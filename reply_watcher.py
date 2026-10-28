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


def _normalize_username(username: str) -> str:
    """Samakan format username untuk perbandingan: buang "@"/spasi, huruf kecil.

    Entri di config bisa tersimpan apa adanya (jalur _auto_blacklist yang lama menulis
    username mentah, mis. "Budi_Santoso"), jadi kedua sisi harus dinormalisasi.
    """
    return (username or "").strip().lstrip("@").strip().lower()


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


# ── klasifikasi balasan masuk ─────────────────────────────────────────────────
_CLASSIFY_TOKENS = ("jelas", "tidak_jelas", "tidak_sopan")

# Urutan alternasi penting: "jelas" adalah substring dari "tidak_jelas", jadi
# token yang lebih panjang harus dicoba duluan supaya tidak salah potong.
_CLASSIFY_TOKEN_RE = _re.compile(r"tidak_jelas|tidak_sopan|jelas")

# Kalau penilainya sendiri bermasalah → anggap jelas. Penilai yang rusak tidak
# boleh bikin bot berhenti membalas orang sungguhan.
_CLASSIFY_DEFAULT = "jelas"


def _parse_classify_response(resp: str) -> str:
    """
    Ubah jawaban LLM jadi salah satu token klasifikasi. Return "" kalau tidak dikenali.

    Dicari 2 tingkat: baris yang isinya persis satu token (kasus LLM nurut),
    lalu token pertama yang muncul di mana pun (kasus LLM ngeyel jelasin).
    Spasi/strip disamakan jadi "_" supaya "tidak jelas" dan "tidak-jelas" ikut kebaca.
    """
    if not resp:
        return ""

    lines = []
    for raw_line in resp.splitlines():
        t = _re.sub(r"[*`\"'>]", " ", raw_line.lower())
        t = _re.sub(r"[\s\-]+", "_", t).strip("._!?,;:")
        if t:
            lines.append(t)

    for line in lines:
        if line in _CLASSIFY_TOKENS:
            return line

    for line in lines:
        m = _CLASSIFY_TOKEN_RE.search(line)
        if m:
            return m.group(0)
    return ""


def _classify_reply(reply_text: str, our_comment: str = "") -> str:
    """
    Klasifikasikan balasan orang ke: "jelas" / "tidak_jelas" / "tidak_sopan".

    SATU panggilan llm_router.generate saja (tiap panggilan ~10 detik, jadi
    tidak boleh dipecah jadi beberapa penilaian). Selalu fail-open ke "jelas":
    kalau LLM exception atau jawabannya tidak dikenali, balasan diperlakukan
    normal supaya watcher tetap membalas orang sungguhan.
    """
    if not (reply_text or "").strip():
        return _CLASSIFY_DEFAULT

    sys_prompt = (
        "lo classifier balasan Threads. "
        "jawab HANYA satu token, tanpa penjelasan, tanpa tanda baca."
    )
    prompt = (
        "Tentukan kategori balasan berikut:\n"
        "- tidak_sopan: kasar, menghina, merendahkan, atau nyerang orangnya\n"
        "- tidak_jelas: kata-katanya acak-acakan/ngelantur sampai maksudnya nggak kebaca\n"
        "- jelas: selain dua di atas (termasuk yang singkat, beda pendapat, atau nanya)\n\n"
        "Jawab HANYA salah satu token ini: jelas / tidak_jelas / tidak_sopan\n"
        "Contoh jawaban yang benar: tidak_sopan\n"
        "Jangan tulis penjelasan atau teks lain.\n\n"
        f"Komentar gue sebelumnya: {(our_comment or '')[:100]}\n"
        f"Balasan mereka: {reply_text[:300]}"
    )

    try:
        resp = llm_router.generate(prompt, system_prompt=sys_prompt)
        label = _parse_classify_response(resp)
        if label in _CLASSIFY_TOKENS:
            return label
        print(f"  [WATCHER KLASIFIKASI] Jawaban penilai tidak dikenali ('{(resp or '')[:40]}') — anggap jelas")
    except Exception as e:
        print(f"  [WATCHER KLASIFIKASI] LLM error saat menilai ({e}) — anggap jelas")
    return _CLASSIFY_DEFAULT


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

    Threads menaruh teks post di ``text_fragments[].plaintext`` dan field
    ``pk`` / ``user`` berada jauh dari ``reply_to_author``, jadi parsing
    di-anchor pada ``reply_to_author`` yang menunjuk ke akun kita lalu
    field sisanya diambil dari window sekitarnya.
    """
    results = []
    seen_pks = set()

    anchor = _re.compile(
        r'"reply_to_author"\s*:\s*\{[^}]*?"username"\s*:\s*"([^"]+)"'
    )
    for m in anchor.finditer(html):
        if m.group(1).lower() != our_username.lower():
            continue
        pos = m.start()

        before = html[max(0, pos - 2500):pos]
        after  = html[pos:pos + 4000]

        # Teks balasan: field plaintext di text_fragments node ini
        txt_m = None
        for tm in _re.finditer(r'"plaintext"\s*:\s*"((?:[^"\\]|\\.)*)"', before):
            txt_m = tm
        if not txt_m:
            txt_m = _re.search(
                r'"caption"\s*:\s*\{[^{}]*"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
                before, _re.DOTALL,
            )
        if not txt_m:
            continue
        raw = txt_m.group(1)
        try:
            text = json.loads('"' + raw + '"')
        except Exception:
            text = raw.replace("\\n", "\n").replace('\\"', '"')
        text = text.strip()
        if not text:
            continue

        # pk balasan: media pk terakhir sebelum anchor
        pk_m = None
        for pm in _re.finditer(r'"pk"\s*:\s*"(\d{15,})"', before):
            pk_m = pm
        if not pk_m:
            continue
        reply_id = pk_m.group(1)
        if reply_id == comment_id or reply_id in seen_pks:
            continue

        # Penulis balasan: username di dalam objek "user" sesudah anchor
        commenter = ""
        um = _re.search(r'"user"\s*:\s*\{', after)
        if um:
            au = _re.search(
                r'"username"\s*:\s*"([^"]+)"', after[um.start():um.start() + 3000]
            )
            if au:
                commenter = au.group(1)
        if not commenter or commenter.lower() == our_username.lower():
            continue

        ta_m = _re.search(r'"taken_at"\s*:\s*(\d+)', before + after[:500])
        seen_pks.add(reply_id)
        results.append({
            "reply_id":  reply_id,
            "commenter": commenter,
            "text":      text,
            "taken_at":  int(ta_m.group(1)) if ta_m else 0,
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


# Balasan klarifikasi cadangan kalau LLM gagal — tetap nanya balik, tidak sok paham.
_CLARIFY_FALLBACKS = [
    "hah? gue kurang nangkep maksud lo apa",
    "sorry, ini maksudnya gimana ya?",
    "belum kebayang maksud lo — jelasin dikit dong",
]


def generate_clarification_reply(their_reply: str, our_comment: str = "") -> str:
    """
    Balasan untuk reply yang kata-katanya tidak jelas: tanya balik singkat & ramah.

    Satu panggilan LLM; kalau gagal atau hasilnya kosong/berisi link, pakai
    fallback tetap supaya balasan klarifikasi selalu ada.
    """
    sys_prompt = """ada orang yang balas komentar lo di Threads, tapi kata-katanya nggak kebaca maksudnya.
lo mau nanya balik biar jelas — bukan sok paham, bukan ngeledek.

cara lo bales:
- sangat singkat, 1 kalimat cukup
- nada ramah dan penasaran, jangan nyalahin orangnya
- pakai bahasa lo: "gue", "lo", "sih", "aja"
- JANGAN pura-pura ngerti isi balasan mereka
- JANGAN ngeledek atau nyindir
- JANGAN sebut nama orang"""

    user_prompt = (
        f"Komentar gue sebelumnya: {(our_comment or '')[:100]}\n"
        f"Mereka balas (nggak jelas): {their_reply[:200]}\n\n"
        f"Tanya balik singkat biar paham maksudnya:"
    )

    try:
        raw = llm_router.generate(user_prompt, system_prompt=sys_prompt)
        text = _re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', raw).strip().lstrip('"> ').strip()
        text = text.splitlines()[0].strip() if text else ""
        if text and not _re.search(r'https?://\S+', text):
            print(f"  [WATCHER DRAFT] klarifikasi: '{text[:50]}'")
            return text[:200]
        print("  [WATCHER] Klarifikasi dari LLM kosong/berisi link — pakai fallback")
    except Exception as e:
        print(f"  [WATCHER] LLM error saat bikin klarifikasi: {e} — pakai fallback")
    return random.choice(_CLARIFY_FALLBACKS)


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

    # Dibaca sekali per siklus: klasifikasi balasan & blacklist otomatis bersifat opsional
    cfg              = config_loader.load()
    classify_enabled = bool(cfg.get("reply_classify_enabled", True))
    blacklist_impolite = bool(cfg.get("auto_blacklist_impolite", False))
    # Entri kosong / "@" ternormalisasi jadi "" — dibuang supaya tidak ikut match
    blacklisted = {_normalize_username(u) for u in cfg.get("blacklisted_accounts", [])} - {""}

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

            # Dicek paling awal: akun yang diblokir user tidak boleh dibalas sama sekali,
            # jadi tidak perlu lewat guard keyword maupun klasifikasi LLM (~10 detik).
            if _normalize_username(commenter) in blacklisted:
                print(f"  [WATCHER] @{commenter} di-blacklist — skip")
                _mark_replied(rid, "BLACKLISTED")
                continue

            if _is_negative_reply(reply_text):
                print(f"  [WATCHER] ⚠ Reply negatif dari @{commenter} — blacklist & skip")
                _auto_blacklist(commenter)
                _mark_replied(rid, "")
                continue

            # Klasifikasi balasan — di-gate config. Kalau mati, tidak ada panggilan
            # LLM tambahan dan alurnya persis sama seperti sebelumnya.
            reply_kind = _CLASSIFY_DEFAULT
            if classify_enabled:
                reply_kind = _classify_reply(reply_text, our_comment)
                aksi = {
                    "tidak_sopan":  "skip",
                    "tidak_jelas":  "balas klarifikasi",
                }.get(reply_kind, "balas normal")
                print(f"  [WATCHER KLASIFIKASI] @{commenter}: {reply_kind} → {aksi}")

            if reply_kind == "tidak_sopan":
                print(f"  [WATCHER] ⚠ Balasan kasar/tidak sopan dari @{commenter}: "
                      f"'{reply_text[:60]}…' — tidak dibalas")
                if blacklist_impolite:
                    # Format disamakan dengan yang dibaca filter blacklist di
                    # auto_commenter (lowercase, tanpa "@" di depan)
                    _auto_blacklist(commenter.strip().lstrip("@").lower())
                else:
                    print("  [WATCHER] auto_blacklist_impolite=False — skip tanpa blacklist")
                _mark_replied(rid, "SKIP_TIDAK_SOPAN")
                continue

            if reply_kind == "tidak_jelas":
                resp_text = generate_clarification_reply(reply_text, our_comment)
            else:
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
                print("  [WATCHER] Posting dihentikan. Perbarui cookies di .env:")
                print("  [WATCHER]   1. Buka threads.com di browser")
                print("  [WATCHER]   2. F12 → Application → Cookies → threads.com")
                print("  [WATCHER]   3. Salin sessionid ke THREADS_SESSION_ID di .env")
                print("  [WATCHER]   4. Restart watcher")
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
    print("[INFO] Mengecek koneksi Qoder CLI…")
    if not llm_router.ping():
        print("[ERROR] Qoder CLI tidak merespons saat startup. Cek login & koneksi internet.")
        sys.exit(1)
    print("[INFO] Qoder CLI OK.")

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
