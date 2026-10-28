"""
linkedin_reply_watcher.py -- Pantau balasan ke komentar kita di LinkedIn & auto-reply.

Alur:
  1. Baca linkedin_replied.json untuk tahu comment_id + activity_urn yang kita komentari
  2. Fetch komentar di tiap activity via Voyager /socialActions/{urn}/comments
  3. Cari reply yang punya parentCommentUrn = comment_id kita
  4. Cek apakah sudah dibalas (linkedin_watcher_cache.json)
  5. Generate balasan natural via llm_router
  6. Kirim via /socialActions/{urn}/comments dengan parentCommentUrn
"""

import json
import logging
import os
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

import llm_router
import config_loader
import stats_tracker
from linkedin_commenter import (
    _parse_reply, _quality_check,
    SessionExpiredError, RateLimitError,
)
from services.linkedin_session_manager import LinkedInAuth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR        = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.getenv("PROFILE_DIR", _DIR)

COMMENTS_URL = "https://www.linkedin.com/voyager/api/socialActions/{urn}/comments"

_REPLIED_FILE       = os.path.join(_DATA_DIR, "linkedin_replied.json")
_WATCHER_CACHE_FILE = os.path.join(_DATA_DIR, "linkedin_watcher_cache.json")

_NEGATIVE_REPLY_SIGNALS = [
    'spam', 'bot', 'lapor', 'report', 'annoying', 'nyebelin', 'ganggu',
    'berhenti', 'stop', 'ga minta', 'siapa lo', 'ngapain', 'block',
    'kenapa komen', 'lo siapa',
]


def _is_negative_reply(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in _NEGATIVE_REPLY_SIGNALS)


# ── watcher cache ─────────────────────────────────────────────────────────────

def _load_watcher_cache() -> Dict:
    try:
        if Path(_WATCHER_CACHE_FILE).exists():
            return json.loads(Path(_WATCHER_CACHE_FILE).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_watcher_cache(data: Dict):
    try:
        # Prune > 14 hari
        cutoff = time.time() - 14 * 86400
        data   = {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
        Path(_WATCHER_CACHE_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"[LI_WATCHER] Cache save error: {e}")


def _already_replied_watcher(reply_id: str) -> bool:
    return reply_id in _load_watcher_cache()


def _mark_replied_watcher(reply_id: str, text: str):
    data = _load_watcher_cache()
    data[reply_id] = {"ts": time.time(), "text": text}
    _save_watcher_cache(data)


# ── comment fetcher ───────────────────────────────────────────────────────────

def fetch_comments_on_activity(auth: LinkedInAuth, urn: str) -> List[Dict]:
    """Fetch semua komentar di sebuah activity."""
    session = auth.get_authenticated_session()
    if not session:
        raise SessionExpiredError("No session")

    encoded = urn.replace(":", "%3A")
    url     = COMMENTS_URL.format(urn=encoded)
    headers = auth.get_request_headers()

    try:
        resp = session.get(url, headers=headers, timeout=20)
    except Exception as e:
        logger.warning(f"[LI_WATCHER] Fetch comments error: {e}")
        return []

    if resp.status_code in (401, 403):
        raise SessionExpiredError(f"Comments returned {resp.status_code}")
    if resp.status_code == 429:
        raise RateLimitError(int(resp.headers.get("Retry-After", 90)))
    if resp.status_code != 200:
        logger.warning(f"[LI_WATCHER] Comments HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    # Voyager comments response: data.elements = list of comment objects
    elements = (data.get("data") or {}).get("elements", [])
    if not elements:
        elements = data.get("elements", [])
    if not elements:
        included = data.get("included", [])
        elements = [i for i in included if "Comment" in i.get("$type", "")]

    comments = []
    for c in elements:
        cid   = c.get("id") or c.get("entityUrn") or c.get("$id", "")
        text  = ""
        msg   = c.get("message") or {}
        if isinstance(msg, dict):
            text = msg.get("text", "").strip()
        if not text:
            continue
        parent_urn = c.get("parentCommentUrn") or c.get("parentComment") or ""
        author_obj = c.get("commenter") or {}
        author     = ""
        if isinstance(author_obj, dict):
            mp = author_obj.get("miniProfile") or {}
            if isinstance(mp, dict):
                author = mp.get("publicIdentifier", "")
        comments.append({
            "id":         cid,
            "text":       text,
            "author":     author,
            "parent_urn": parent_urn,
        })

    return comments


# ── reply generator ───────────────────────────────────────────────────────────

def generate_watcher_reply(their_text: str, our_comment: str, original_caption: str) -> str:
    cfg     = config_loader.load()
    persona = cfg.get("linkedin_persona") or cfg.get("bot_persona", "")

    sys_prompt = f"""lo developer IT Indonesia yang lagi balas komentar di LinkedIn.
seseorang membalas komentar lo — lo perlu balas singkat dan natural.

LATAR BELAKANG: {persona}

ATURAN:
- 1 kalimat, maks 150 karakter
- Natural, bukan corporate speak
- Kalau mereka nanya: jawab singkat atau nanya balik
- Kalau mereka relate: acknowledge singkat
- Kalau terasa spam/negatif/tidak relevan: balas SKIP
- JANGAN: link, mention @orang, hashtag"""

    user_prompt = (
        f"Post asli: {original_caption[:100]}\n"
        f"Komentar lo: {our_comment[:100]}\n"
        f"Balasan mereka: {their_text[:150]}\n\n"
        "Balas komentar mereka:"
    )

    try:
        raw = llm_router.generate(user_prompt, system_prompt=sys_prompt)
        reply = _parse_reply(raw)
        if reply == "SKIP":
            return "SKIP"
        if not _quality_check(reply):
            return "SKIP"
        return reply
    except Exception as e:
        stats_tracker.log_error(f"[LI_WATCHER] LLM error: {e}")
        return "SKIP"


# ── reply sender ──────────────────────────────────────────────────────────────

def send_reply(auth: LinkedInAuth, activity_urn: str, parent_comment_urn: str, text: str) -> Optional[str]:
    """Kirim balasan ke komentar. Return comment_id atau None."""
    session = auth.get_authenticated_session()
    if not session:
        raise SessionExpiredError("No session")

    encoded = activity_urn.replace(":", "%3A")
    url     = COMMENTS_URL.format(urn=encoded)
    headers = {**auth.get_request_headers(), "content-type": "application/json"}
    body    = json.dumps({
        "message":           {"text": text},
        "parentCommentUrn":  parent_comment_urn,
    })

    try:
        resp = session.post(url, headers=headers, content=body, timeout=20)
    except Exception as e:
        raise SessionExpiredError(f"Reply request failed: {e}")

    if resp.status_code in (401, 403):
        raise SessionExpiredError(f"Reply returned {resp.status_code}")
    if resp.status_code == 429:
        raise RateLimitError(int(resp.headers.get("Retry-After", 90)))
    if resp.status_code in (200, 201):
        try:
            cid = resp.json().get("id") or resp.json().get("entityUrn", "ok")
        except Exception:
            cid = "ok"
        logger.info(f"[LI_WATCHER] Reply terkirim: {text[:50]}…")
        return cid

    logger.warning(f"[LI_WATCHER] Reply HTTP {resp.status_code}: {resp.text[:200]}")
    return None


# ── main cycle ────────────────────────────────────────────────────────────────

def run_once(auth: LinkedInAuth):
    """Satu siklus: cek semua komentar yang kita kirim → balas yang ada balasannya."""
    # Baca history komentar kita
    try:
        replied_data = json.loads(Path(_REPLIED_FILE).read_text(encoding="utf-8")) \
            if Path(_REPLIED_FILE).exists() else {}
    except Exception:
        replied_data = {}

    if not replied_data:
        logger.info("[LI_WATCHER] Belum ada komentar yang bisa dipantau")
        return

    cfg       = config_loader.load()
    blacklist = [u.lower() for u in (cfg.get("blacklisted_accounts") or [])]

    our_username = auth.username or ""
    replied_count = 0

    for activity_urn, entry in list(replied_data.items()):
        our_comment_id   = entry.get("comment_id", "")
        our_comment_text = entry.get("text", "")

        if not our_comment_id:
            continue

        # Jangan cek post yang sudah lebih dari 7 hari
        post_age = time.time() - entry.get("ts", 0)
        if post_age > 7 * 86400:
            continue

        logger.info(f"  [LI_WATCHER] Cek activity {activity_urn[:50]}…")

        try:
            comments = fetch_comments_on_activity(auth, activity_urn)
        except (SessionExpiredError, RateLimitError):
            raise
        except Exception as e:
            logger.warning(f"  [LI_WATCHER] Error fetch comments: {e}")
            continue

        # Cari reply yang parent-nya adalah komentar kita
        for c in comments:
            parent = c.get("parent_urn", "")
            if not parent:
                continue
            # Parent bisa berupa full URN atau hanya ID
            if our_comment_id not in parent and our_comment_id != c.get("id"):
                continue
            if c.get("author", "").lower() == our_username.lower():
                continue  # komentar kita sendiri

            reply_id = c["id"]
            if _already_replied_watcher(reply_id):
                continue

            their_text = c["text"]
            author     = c.get("author", "unknown")

            if author.lower() in blacklist:
                logger.info(f"  [LI_WATCHER] Skip @{author} — blacklist")
                _mark_replied_watcher(reply_id, "BLACKLISTED")
                continue

            if _is_negative_reply(their_text):
                logger.info(f"  [LI_WATCHER] Negative reply dari @{author} — skip & blacklist")
                if author and author != "unknown":
                    bl = list(cfg.get("blacklisted_accounts") or [])
                    if author.lower() not in [u.lower() for u in bl]:
                        bl.append(author)
                        config_loader.save({"blacklisted_accounts": bl})
                _mark_replied_watcher(reply_id, "NEGATIVE")
                continue

            logger.info(f"  [LI_WATCHER] Balas @{author}: {their_text[:60]}…")

            # Cari original caption (tidak simpan di cache, gunakan teks komentar sebagai proxy)
            reply_text = generate_watcher_reply(
                their_text=their_text,
                our_comment=our_comment_text,
                original_caption=our_comment_text,
            )

            if reply_text == "SKIP":
                logger.info("  [LI_WATCHER] LLM SKIP reply ini")
                _mark_replied_watcher(reply_id, "SKIPPED")
                continue

            time.sleep(random.uniform(5, 15))

            try:
                cid = send_reply(auth, activity_urn, reply_id, reply_text)
                if cid:
                    _mark_replied_watcher(reply_id, reply_text)
                    stats_tracker.increment("li_replies_sent")
                    replied_count += 1
                    logger.info(f"  [LI_WATCHER] balas: {reply_text}")
            except (SessionExpiredError, RateLimitError):
                raise
            except Exception as e:
                logger.warning(f"  [LI_WATCHER] Send reply error: {e}")

        time.sleep(random.uniform(3, 8))

    if replied_count:
        logger.info(f"[LI_WATCHER] Siklus selesai — {replied_count} balasan dikirim")
    else:
        logger.info("[LI_WATCHER] Tidak ada reply baru yang perlu dibalas")


def main():
    from dotenv import load_dotenv; load_dotenv()

    auth = LinkedInAuth()
    if not auth.load_session():
        logger.error("[LI_WATCHER] Session invalid — set LINKEDIN_LI_AT di .env")
        sys.exit(1)

    cfg            = config_loader.load()
    check_minutes  = int(cfg.get("linkedin_reply_check_minutes", cfg.get("auto_reply_check_minutes", 20)))
    active_start   = int(cfg.get("linkedin_active_start_hour", cfg.get("active_start_hour", 7)))
    active_end     = int(cfg.get("linkedin_active_end_hour",   cfg.get("active_end_hour",   23)))

    logger.info(f"[LI_WATCHER] Mulai — cek setiap {check_minutes} menit")

    while True:
        now_h = datetime.now().hour
        if not (active_start <= now_h < active_end):
            logger.info("[LI_WATCHER] Di luar jam aktif — tidur 15 menit")
            time.sleep(900)
            continue

        try:
            run_once(auth)
        except SessionExpiredError as e:
            logger.error(f"[LI_WATCHER] Session expired: {e} — berhenti")
            sys.exit(1)
        except RateLimitError as e:
            logger.warning(f"[LI_WATCHER] Rate limited — tunggu {e.retry_after}s")
            time.sleep(e.retry_after)
            continue

        wait = check_minutes * 60 + random.randint(-120, 120)
        logger.info(f"[LI_WATCHER] Tidur {wait//60} menit {wait%60} detik…")
        time.sleep(wait)


if __name__ == "__main__":
    main()
