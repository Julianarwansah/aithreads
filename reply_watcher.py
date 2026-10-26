"""
reply_watcher.py -- Pantau balasan ke komentar kita & auto-reply natural.

Alur:
  1. Baca reply_cache → ambil post_id + comment_id yang baru-baru kita komentari
  2. Untuk tiap comment, scrape thread page (anonymous session) untuk cari balasan
  3. Temukan reply yang ditujukan ke username kita
  4. Cek apakah sudah pernah dibalas (replied_cache.json)
  5. Generate balasan natural via Qoder
  6. Kirim balasan & catat ke replied_cache.json

Metode fetch: HTML scraping via anonymous session (bukan REST API yang sudah tidak jalan).
  - https://www.threads.com/t/{comment_shortcode} → halaman thread comment kita
  - Parse relay store JSON dari HTML untuk menemukan balasan
"""
import json
import logging
import os
import random
import re as _re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

from dotenv import load_dotenv
load_dotenv(override=True)

import config_loader
import comment_memory
import llm_router
import stats_tracker
import reply_cache as rc
from auto_commenter import (
    post_reply, detect_post_sentiment, _heuristic_score, _score_naturalness,
    SessionExpiredError, RateLimitError,
)
from services.session_manager import SessionManager, SessionStatus
from curl_cffi.requests import Session as CurlSession

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR               = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR          = os.getenv("PROFILE_DIR", _DIR)
REPLIED_CACHE_FILE = os.path.join(_DATA_DIR, "replied_cache.json")

_ANON_HEADERS = {
    "accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "sec-fetch-dest":  "document",
    "sec-fetch-mode":  "navigate",
    "sec-fetch-site":  "none",
    "upgrade-insecure-requests": "1",
}

_NEGATIVE_REPLY_SIGNALS = [
    'spam', 'bot', 'lapor', 'report', 'annoying', 'nyebelin', 'ganggu',
    'berhenti', 'stop comment', 'ga minta pendapat', 'siapa lo', 'ngapain',
    'unfollow', 'block', 'kenapa komen', 'lo siapa',
]

def _is_negative_reply(text: str) -> bool:
    low = text.lower()
    return any(sig in low for sig in _NEGATIVE_REPLY_SIGNALS)


_blacklist_lock = threading.Lock()

def _auto_blacklist(username: str):
    if not username:
        return
    with _blacklist_lock:
        cfg = config_loader.load()
        blacklist = cfg.get("blacklisted_accounts", [])
        if username.lower() not in [u.lower() for u in blacklist]:
            blacklist.append(username)
            config_loader.save({"blacklisted_accounts": blacklist})
            print(f"  [AUTO-BLACKLIST] @{username} ditambahkan ke blacklist otomatis")


# ── shortcode converter ───────────────────────────────────────────────────────
def _to_shortcode(num: int) -> str:
    """Konversi media pk (int) ke Threads/Instagram shortcode."""
    ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    result = ""
    while num > 0:
        num, rem = divmod(num, 64)
        result = ALPHABET[rem] + result
    return result


# ── replied cache ─────────────────────────────────────────────────────────────
def _load_replied() -> dict:
    try:
        if Path(REPLIED_CACHE_FILE).exists():
            return json.loads(Path(REPLIED_CACHE_FILE).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_replied(data: dict):
    Path(REPLIED_CACHE_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _already_replied(reply_id: str) -> bool:
    return reply_id in _load_replied()


def _mark_replied(reply_id: str, text: str):
    data = _load_replied()
    data[reply_id] = {"ts": time.time(), "text": text}
    cutoff = time.time() - 7 * 86400
    data = {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
    _save_replied(data)


# ── HTML scraping (anonymous session) ────────────────────────────────────────
def _parse_relay_replies(html: str, comment_id: str, our_username: str) -> list:
    """
    Parse relay store JSON dari halaman thread untuk menemukan balasan
    ke komentar kita (comment_id).

    Mencari post node yang memenuhi salah satu kondisi:
      - reply_to_author.username == our_username  (Threads field langsung)
      - pk != comment_id && is_reply:true && (reply_level >= 2 atau rta matches)
    """
    results = []
    seen_pks = set()

    # Cari semua blok JSON yang mengandung "pk": "..." + caption
    # Pattern: cari "pk":"<angka>" lalu ambil context sekitarnya
    for m in _re.finditer(r'"pk"\s*:\s*"(\d+)"', html):
        pk = m.group(1)
        if pk == comment_id or pk in seen_pks:
            continue
        # Hanya ambil pk yang berukuran besar (media pk, bukan user_id)
        if len(pk) < 15:
            continue

        pos = m.start()
        # Ambil window context untuk parsing fields
        ctx_start = max(0, pos - 500)
        ctx_end   = min(len(html), pos + 4000)
        ctx       = html[ctx_start:ctx_end]

        # Wajib ada is_reply:true
        if '"is_reply":true' not in ctx and '"is_reply": true' not in ctx:
            continue

        # Ambil reply_level (opsional, tapi lebih presisi)
        rl_m = _re.search(r'"reply_level"\s*:\s*(\d+)', ctx)
        reply_level = int(rl_m.group(1)) if rl_m else 1

        # Ambil reply_to_author username
        rta_m = _re.search(
            r'"reply_to_author"\s*:\s*\{[^}]*"username"\s*:\s*"([^"]+)"', ctx
        )
        rta_username = rta_m.group(1) if rta_m else ""

        # Filter: harus reply ke kita atau reply_level >= 2 (sub-reply) atau
        # comment_id kita ada di context-nya
        is_reply_to_us = (
            rta_username.lower() == our_username.lower()
            or (reply_level >= 2 and comment_id in ctx)
        )
        if not is_reply_to_us:
            continue

        # Ambil user yang reply
        user_m = _re.search(r'"username"\s*:\s*"([^"]+)"', ctx)
        commenter = user_m.group(1) if user_m else ""
        if commenter.lower() == our_username.lower():
            continue  # skip komentar kita sendiri

        # Ambil caption/text
        cap_m = _re.search(
            r'"caption"\s*:\s*\{[^{}]*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]',
            ctx, _re.DOTALL
        )
        if not cap_m:
            # fallback: cari "text" dekat pk
            cap_m = _re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.){5,200})"', ctx)
        if not cap_m:
            continue
        text = cap_m.group(1).replace("\\n", "\n").replace('\\"', '"').strip()
        if not text:
            continue

        # Ambil taken_at kalau ada
        ta_m  = _re.search(r'"taken_at"\s*:\s*(\d+)', ctx)
        taken = int(ta_m.group(1)) if ta_m else 0

        seen_pks.add(pk)
        results.append({
            "reply_id":  pk,
            "commenter": commenter,
            "text":      text,
            "taken_at":  taken,
        })

    return results


def fetch_comment_replies(comment_id: str, our_username: str, max_retries: int = 2) -> list:
    """
    Fetch balasan ke komentar kita menggunakan HTML scraping (anonymous session).

    Jauh lebih stabil dari REST API /api/v1/media/{id}/comments/ yang
    sudah tidak berjalan (mengembalikan HTML bukan JSON).
    """
    shortcode = _to_shortcode(int(comment_id))
    url       = f"https://www.threads.com/t/{shortcode}"

    for attempt in range(1, max_retries + 1):
        anon = CurlSession(impersonate="chrome124")
        try:
            resp = anon.get(url, headers=_ANON_HEADERS, timeout=25)
            if resp.status_code == 429:
                wait = 60 + random.randint(10, 30)
                print(f"  [WATCHER] Rate limit t/{shortcode} — tunggu {wait}d...")
                time.sleep(wait)
                continue
            if resp.status_code != 200 or len(resp.text) < 2000:
                print(f"  [WATCHER] HTTP {resp.status_code} untuk comment {comment_id[:10]}…")
                return []

            items = _parse_relay_replies(resp.text, comment_id, our_username)
            print(f"  [WATCHER] {len(items)} balasan ditemukan untuk comment {comment_id[:10]}…")
            return items

        except Exception as e:
            print(f"  [WATCHER] Fetch error (attempt {attempt}): {e}")
            if attempt < max_retries:
                time.sleep(5 * attempt)

    return []


# ── generate balasan ──────────────────────────────────────────────────────────
def generate_watcher_reply(
    their_reply: str,
    our_comment: str,
    original_caption: str,
) -> str:
    """Buat balasan natural ke orang yang reply ke komentar kita."""
    sentiment = detect_post_sentiment(their_reply)
    sentiment_guidance = {
        "frustrated": "mereka kayaknya frustrasi — empati dulu, jangan langsung kasih solusi",
        "win":        "mereka share hal positif — ikut relate atau react seneng",
        "question":   "mereka nanya ke lo — jawab singkat atau nanya balik",
        "opinion":    "mereka share opini — lo bisa setuju, nuansi, atau tanya lebih lanjut",
        "info":       "",
    }.get(sentiment, "")

    sentiment_line = f"\n\nKonteks: {sentiment_guidance}" if sentiment_guidance else ""

    sys_prompt = f"""lo lagi ngobrol di Threads, ada orang yang balas komentar lo.
lo mau nimbrung balik dengan santai — kayak orang nyambung ngobrol, bukan asisten.

cara lo bales:
- sangat singkat, 1 kalimat cukup
- kadang cukup setuju, kadang nanya balik, kadang iseng
- pakai bahasa lo: "gue", "lo", "sih", "aja", "emg", "bgt"
- JANGAN berlebihan ("wah keren!", "setuju banget!")
- JANGAN kasih saran panjang
- JANGAN sebut nama orang

contoh yang bagus:
"nah bener tuh"
"serius? gimana hasilnya?"
"ini gue skip dulu, kayaknya ribet"
"iya emg gitu sih kenyataannya"{sentiment_line}"""

    user_prompt = (
        f"Konteks postingan asli: {original_caption[:150]}\n"
        f"Komentar gue sebelumnya: {our_comment[:100]}\n"
        f"Mereka balas: {their_reply}\n\n"
        f"Bales singkat:"
    )

    def _clean(raw: str) -> str:
        t = _re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', raw).strip().lstrip('"> ').strip()
        return "" if _re.search(r'https?://\S+', t) else t

    def _gen_best() -> str:
        two_prompt = (
            f"Tulis 2 versi balasan berbeda (1 kalimat masing-masing), "
            f"pisahkan dengan ---\n\n{user_prompt}"
        )
        r = llm_router.generate(two_prompt, system_prompt=sys_prompt)
        print(f"\n[WATCHER RAW LLM]: {r}")
        parts = [_clean(p) for p in r.split("---") if p.strip()]
        valid = [(t, _heuristic_score(t)) for t in parts if t]
        if len(valid) >= 2:
            best, sc = max(valid, key=lambda x: x[1])
            print(f"  [WATCHER DRAFT] {len(valid)} kandidat, pilih skor {sc:.1f}: '{best[:50]}'")
            return best
        return _clean(r)

    try:
        resp = _gen_best()
        if not resp:
            return ""

        score = _score_naturalness(resp, their_reply)
        print(f"  [WATCHER CRITIQUE] Skor naturalness: {score}/10")
        if score < 7:
            print("  [WATCHER CRITIQUE] Skor rendah — generate ulang…")
            retry = _gen_best()
            if retry and _score_naturalness(retry, their_reply) > score:
                resp = retry

        return resp[:200]
    except Exception as e:
        print(f"  [WATCHER] LLM error: {e}")
        return ""


# ── satu siklus cek ───────────────────────────────────────────────────────────
def check_and_reply(session, our_username: str):
    recent = rc.get_recent_post_ids(max_age_hours=48)
    if not recent:
        print("[WATCHER] Belum ada post yang dikomentari dalam 48 jam terakhir.")
        return

    trackable = [e for e in recent if e.get("comment_id")]
    if not trackable:
        print("[WATCHER] Belum ada comment_id tersimpan — tunggu bot komentar berikutnya.")
        return

    print(f"[WATCHER] Mengecek {len(trackable)} komentar yang bisa di-track…")
    replied_count = 0

    for entry in trackable:
        comment_id  = entry["comment_id"]
        our_comment = entry["our_reply"]
        orig_cap    = entry["caption"]

        # Gunakan HTML scraping (anonymous session, tidak butuh auth)
        items = fetch_comment_replies(comment_id, our_username)

        if not items:
            continue

        for r in items:
            rid        = r["reply_id"]
            commenter  = r["commenter"]
            reply_text = r["text"]

            if _already_replied(rid):
                continue

            print(f"  [WATCHER] @{commenter} balas: {reply_text[:60]}…")

            if _is_negative_reply(reply_text):
                print(f"  [WATCHER] ⚠ Reply negatif dari @{commenter} — blacklist & skip")
                _auto_blacklist(commenter)
                _mark_replied(rid, "")
                continue

            resp_text = generate_watcher_reply(reply_text, our_comment, orig_cap)
            if not resp_text:
                print("  [WATCHER] Gagal generate balasan, skip.")
                continue

            print(f"  [WATCHER] DRAF BALAS: {resp_text}")
            time.sleep(random.randint(3, 8))

            try:
                new_id = post_reply(session, rid, resp_text)
            except SessionExpiredError as e:
                print(f"\n{'='*60}")
                print(f"  [WATCHER] SESSION EXPIRED: {e}")
                print(f"  [WATCHER] Posting dihentikan. Perbarui cookies di .env:")
                print(f"  [WATCHER]   1. Buka threads.com di browser")
                print(f"  [WATCHER]   2. F12 → Application → Cookies → threads.com")
                print(f"  [WATCHER]   3. Salin sessionid ke THREADS_SESSION_ID di .env")
                print(f"  [WATCHER]   4. Restart watcher")
                print(f"{'='*60}\n")
                raise  # bubble up ke main() agar loop berhenti
            except RateLimitError as e:
                wait = e.args[0] if e.args else 60
                print(f"  [WATCHER] Rate limit — tunggu {wait}d sebelum lanjut…")
                time.sleep(wait)
                continue

            if new_id is not None:
                print("  [WATCHER] ✓ Berhasil membalas!")
                _mark_replied(rid, resp_text)
                stats_tracker.increment("comments_sent")
                replied_count += 1
                comment_memory.mark_engagement(our_comment, caption=orig_cap)
                print(f"  [WATCHER] ⭐ '{our_comment[:40]}…' ditandai engaged")
            else:
                print("  [WATCHER] Gagal membalas (no ID returned).")

            time.sleep(random.randint(30, 90))

        # Jeda antar komentar agar tidak hammering
        time.sleep(random.randint(8, 20))

    print(f"[WATCHER] Selesai — {replied_count} balasan dikirim.")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    if not llm_router.is_available():
        print("[ERROR] Qoder CLI tidak ditemukan.")
        sys.exit(1)

    our_username = os.getenv("THREADS_USERNAME", "").strip()
    if not our_username:
        print("[ERROR] Set THREADS_USERNAME di .env (username Threads lo, tanpa @).")
        sys.exit(1)

    sm = SessionManager()
    if not sm.load_session() or sm.validate_session().status != SessionStatus.VALID:
        print("[ERROR] Session invalid.")
        sys.exit(1)

    session = sm.inject_session()
    print(f"[INFO] Reply Watcher aktif — memantau balasan ke @{our_username}")
    print("[INFO] Menggunakan HTML scraping (anonymous session) untuk deteksi balasan.")

    while True:
        cfg = config_loader.load()

        if not cfg.get("auto_reply_enabled", False):
            print("[WATCHER] auto_reply_enabled=False, tidur 5 menit…")
            time.sleep(300)
            continue

        h     = datetime.now().hour
        start = int(cfg.get("active_start_hour", 7))
        end   = int(cfg.get("active_end_hour",   23))
        if not (start <= h < end):
            print(f"[SCHEDULE] Di luar jam aktif ({h:02d}:xx), tidur 30 menit…")
            time.sleep(1800)
            continue

        try:
            check_and_reply(session, our_username)
        except SessionExpiredError:
            print("[WATCHER] Session expired — watcher pause 1 jam. Update .env lalu restart.")
            time.sleep(3600)
            continue
        except Exception as e:
            logger.error("Watcher error: %s", e, exc_info=True)

        interval_min = int(cfg.get("auto_reply_check_minutes", 20))
        print(f"[WATCHER] Tidur {interval_min} menit sebelum cek berikutnya…")
        time.sleep(interval_min * 60)


if __name__ == "__main__":
    main()
