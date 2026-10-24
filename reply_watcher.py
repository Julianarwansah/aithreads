"""
reply_watcher.py -- Pantau balasan ke komentar kita & auto-reply natural.

Alur:
  1. Baca reply_cache → ambil post_id yang baru-baru kita komentari
  2. Untuk tiap post, fetch thread items (root + semua reply)
  3. Temukan reply yang ditujukan ke username kita
  4. Cek apakah sudah pernah dibalas (replied_cache.json)
  5. Generate balasan natural via Qoder
  6. Kirim balasan & catat ke replied_cache.json
"""
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import config_loader
import llm_router
import stats_tracker
import reply_cache as rc
from auto_commenter import post_reply, get_fresh_tokens
from services.session_manager import SessionManager, SessionStatus

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
REPLIED_CACHE_FILE = os.path.join(_DIR, "replied_cache.json")

# Endpoint thread items — GET request, butuh cookie auth
THREAD_ITEMS_URL = "https://www.threads.com/api/v1/text_feed/{media_id}/thread_items/"

THREAD_HEADERS = {
    "accept":          "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin":          "https://www.threads.com",
    "referer":         "https://www.threads.com/",
    "user-agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "x-ig-app-id":     "238260118697367",
    "x-asbd-id":       "359341",
}


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
    # bersihkan entry > 7 hari
    cutoff = time.time() - 7 * 86400
    data = {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
    _save_replied(data)


# ── fetch thread items ────────────────────────────────────────────────────────
def fetch_thread_items(session, media_id: str) -> list:
    """Fetch semua item dalam sebuah thread (root post + semua reply)."""
    headers = THREAD_HEADERS.copy()
    csrf = session.cookies.get("csrftoken")
    if csrf:
        headers["x-csrftoken"] = csrf

    url = THREAD_ITEMS_URL.format(media_id=media_id)
    try:
        resp = session.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("thread_items") or data.get("items") or []
            print(f"  [THREAD] {len(items)} item di thread {media_id}")
            return items
        else:
            print(f"  [THREAD] HTTP {resp.status_code} untuk {media_id}")
    except Exception as e:
        print(f"  [THREAD] Error fetch: {e}")
    return []


# ── parse replies to us ───────────────────────────────────────────────────────
def find_replies_to_us(thread_items: list, our_username: str) -> list:
    """
    Dari daftar thread items, kembalikan list reply yang ditujukan ke username kita.
    Struktur item Threads: item["media"] → post object.
    """
    replies = []
    for item in thread_items:
        media = item.get("media") or item  # bisa langsung atau nested
        user  = (media.get("user") or {})
        commenter = user.get("username", "")

        # Lewati kalau ini post kita sendiri
        if commenter.lower() == our_username.lower():
            continue

        text_info   = media.get("text_post_app_info") or {}
        reply_author = (text_info.get("reply_to_author") or {})
        replied_to   = reply_author.get("username", "")

        if replied_to.lower() != our_username.lower():
            continue

        caption_text = (media.get("caption") or {}).get("text", "").strip()
        media_pk     = str(media.get("pk") or media.get("id") or "")

        if not caption_text or not media_pk:
            continue

        replies.append({
            "reply_id":   media_pk,
            "commenter":  commenter,
            "text":       caption_text,
            "taken_at":   media.get("taken_at") or 0,
        })
    return replies


# ── generate balasan ──────────────────────────────────────────────────────────
def generate_watcher_reply(
    their_reply: str,
    our_comment: str,
    original_caption: str,
) -> str:
    """Buat balasan natural ke orang yang reply ke komentar kita."""
    sys_prompt = """lo lagi ngobrol di Threads, ada orang yang balas komentar lo.
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
"haha sama gue juga"
"serius? gimana hasilnya?"
"ini gue skip dulu, kayaknya ribet"
"iya emg gitu sih kenyataannya\""""

    user_prompt = (
        f"Konteks postingan asli: {original_caption[:150]}\n"
        f"Komentar gue sebelumnya: {our_comment[:100]}\n"
        f"Mereka balas: {their_reply}\n\n"
        f"Bales singkat:"
    )

    try:
        resp = llm_router.generate(user_prompt, system_prompt=sys_prompt)
        # Strip markdown kalau ada
        import re
        resp = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', resp).strip()
        resp = resp.lstrip('"> ').strip()
        # Buang kalau ada link
        if re.search(r'https?://\S+', resp):
            return ""
        return resp[:200] if resp else ""
    except Exception as e:
        print(f"  [WATCHER] LLM error: {e}")
        return ""


# ── satu siklus cek ───────────────────────────────────────────────────────────
def check_and_reply(session, our_username: str):
    recent = rc.get_recent_post_ids(max_age_hours=24)
    if not recent:
        print("[WATCHER] Belum ada post yang dikomentari dalam 24 jam terakhir.")
        return

    print(f"[WATCHER] Mengecek {len(recent)} post yang baru-baru dikomentari…")
    replied_count = 0

    for entry in recent:
        post_id     = entry["post_id"]
        our_comment = entry["our_reply"]
        orig_cap    = entry["caption"]

        items = fetch_thread_items(session, post_id)
        if not items:
            continue

        replies_to_us = find_replies_to_us(items, our_username)
        if not replies_to_us:
            print(f"  [WATCHER] @{post_id[:8]}… — belum ada yang balas komentar kita")
            continue

        for r in replies_to_us:
            rid = r["reply_id"]
            if _already_replied(rid):
                continue

            print(f"  [WATCHER] @{r['commenter']} balas: {r['text'][:60]}…")

            resp_text = generate_watcher_reply(r["text"], our_comment, orig_cap)
            if not resp_text:
                print("  [WATCHER] Gagal generate balasan, skip.")
                continue

            print(f"  [WATCHER] DRAF BALAS: {resp_text}")

            # Jeda natural sebelum balas
            time.sleep(random.randint(3, 8))

            ok = post_reply(session, rid, resp_text)
            if ok:
                print("  [WATCHER] Berhasil membalas!")
                _mark_replied(rid, resp_text)
                stats_tracker.increment("comments_sent")
                replied_count += 1
            else:
                print("  [WATCHER] Gagal membalas.")

            # Jeda antar balasan supaya tidak spam
            time.sleep(random.randint(30, 90))

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

    while True:
        cfg = config_loader.load()

        if not cfg.get("auto_reply_enabled", False):
            print("[WATCHER] auto_reply_enabled=False, tidur 5 menit…")
            time.sleep(300)
            continue

        # Cek jam aktif
        h     = datetime.now().hour
        start = int(cfg.get("active_start_hour", 7))
        end   = int(cfg.get("active_end_hour",   23))
        if not (start <= h < end):
            print(f"[SCHEDULE] Di luar jam aktif, tidur 30 menit…")
            time.sleep(1800)
            continue

        try:
            check_and_reply(session, our_username)
        except Exception as e:
            logger.error("Watcher error: %s", e)

        interval_min = int(cfg.get("auto_reply_check_minutes", 30))
        print(f"[WATCHER] Tidur {interval_min} menit sebelum cek berikutnya…")
        time.sleep(interval_min * 60)


if __name__ == "__main__":
    main()
