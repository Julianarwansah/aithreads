"""
job_hunter.py -- Auto-lamar lowongan kerja IT di Threads

Alur:
  1. Scan home timeline untuk post lowongan IT
  2. Filter berdasarkan keyword job + IT
  3. Generate komentar lamaran via LLM
  4. Kirim komentar
  5. Kalau post secara eksplisit minta DM → kirim DM juga
  6. Track semua lamaran di job_applications.json
"""

import json
import logging
import os
import re
import sys
import time
import random
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from dotenv import load_dotenv
load_dotenv(override=True)

import llm_router
import config_loader
import stats_tracker
from auto_commenter import (
    process_timeline, get_fresh_tokens, fetch_timeline,
    load_latest_query_config, BASE_HEADERS, REPLY_ENDPOINT,
    SessionExpiredError, RateLimitError, _rate_backoff,
)
from services.session_manager import SessionManager, SessionStatus

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR      = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("PROFILE_DIR", _DIR)

APPLIED_FILE = os.path.join(_DATA_DIR, "job_applications.json")
DM_BASE_URL  = "https://www.instagram.com/api/v1/direct_v2/threads/"

# ── keyword lists ──────────────────────────────────────────────────────────────

JOB_KEYWORDS = [
    "lowongan", "hiring", "dibutuhkan", "butuh", "cari ", "kami cari",
    "open position", "open role", "open rekrutmen", "rekrut",
    "join tim", "join team", "bergabung", "kita buka",
    "freelance project", "project freelance", "jasa", "butuh jasa",
    "we're hiring", "were hiring", "now hiring",
]

IT_KEYWORDS = [
    "developer", "programmer", "engineer", "programming",
    "frontend", "front end", "front-end",
    "backend", "back end", "back-end",
    "fullstack", "full stack", "full-stack",
    "mobile dev", "android dev", "ios dev", "flutter dev", "react native",
    "web dev", "web developer",
    "python", "javascript", "typescript", "react", "next.js", "nextjs",
    "vue", "angular", "laravel", "django", "fastapi", "node.js", "nodejs",
    "flutter", "kotlin", "swift", "java dev",
    "devops", "cloud engineer", "data engineer", "data scientist",
    "machine learning", "ml engineer", "ai engineer", "llm", "nlp",
    "software engineer", "software dev",
    "it staff", "it support", "ui/ux", "product engineer",
    "tech", "coding", "ngoding",
]

DM_TRIGGER_WORDS = [
    "dm ", "dm.", "dm,", "(dm)", "via dm", "lewat dm", "kirim dm",
    "send dm", "direct message", "silakan dm", "silahkan dm",
    "hubungi dm", "contact dm", "dm aku", "dm kami", "dm saya",
    "inbox ", "dm langsung", "reach out",
]

NEGATIVE_KEYWORDS = [
    "sudah terisi", "already filled", "closed", "ditutup", "posisi penuh",
    "tidak lowong", "maaf sudah", "posisi sudah",
]


# ── cache helpers ──────────────────────────────────────────────────────────────

def _load_applications() -> Dict:
    try:
        if Path(APPLIED_FILE).exists():
            return json.loads(Path(APPLIED_FILE).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_applications(data: Dict):
    try:
        Path(APPLIED_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"[JH] Save error: {e}")


def _already_applied(post_id: str) -> bool:
    return str(post_id) in _load_applications()


def _mark_applied(post_id: str, entry: Dict):
    data = _load_applications()
    data[str(post_id)] = entry
    _save_applications(data)


def _update_application(post_id: str, updates: Dict):
    data = _load_applications()
    if str(post_id) in data:
        data[str(post_id)].update(updates)
        _save_applications(data)


# ── detectors ─────────────────────────────────────────────────────────────────

def _is_job_post(text: str) -> Tuple[bool, str]:
    """
    Return (True, job_type) kalau post adalah lowongan IT.
    job_type: 'fullstack'|'frontend'|'backend'|'mobile'|'ai'|'devops'|'general'
    """
    low = text.lower()

    # Pastikan ada keyword lowongan
    has_job = any(kw in low for kw in JOB_KEYWORDS)
    if not has_job:
        return False, ""

    # Pastikan ada keyword IT
    has_it = any(kw in low for kw in IT_KEYWORDS)
    if not has_it:
        return False, ""

    # Skip kalau sudah ditutup
    if any(kw in low for kw in NEGATIVE_KEYWORDS):
        return False, ""

    # Tentukan tipe posisi
    if any(kw in low for kw in ["machine learning", "ml ", "ai engineer", "llm", "nlp", "data scientist"]):
        return True, "ai"
    if any(kw in low for kw in ["devops", "cloud engineer", "infrastructure"]):
        return True, "devops"
    if any(kw in low for kw in ["mobile", "android", "ios", "flutter", "react native", "kotlin", "swift"]):
        return True, "mobile"
    if any(kw in low for kw in ["frontend", "front end", "front-end", "react", "vue", "angular", "next"]):
        return True, "frontend"
    if any(kw in low for kw in ["backend", "back end", "back-end", "python", "django", "fastapi", "laravel", "node"]):
        return True, "backend"
    if any(kw in low for kw in ["fullstack", "full stack", "full-stack"]):
        return True, "fullstack"
    return True, "general"


def _wants_dm(text: str) -> bool:
    """Apakah post secara eksplisit minta pelamar DM?"""
    low = text.lower()
    return any(kw in low for kw in DM_TRIGGER_WORDS)


def _extract_cv_instruction(text: str) -> Optional[str]:
    """Cek apakah post minta kirim CV/portfolio (untuk disebut di komentar)."""
    low = text.lower()
    if any(kw in low for kw in ["kirim cv", "send cv", "attach cv", "sertakan cv", "lampirkan cv", "portfolio"]):
        return "cv"
    return None


# ── generator ─────────────────────────────────────────────────────────────────

def generate_application_comment(job_text: str, job_type: str, wants_dm: bool, cv_req: Optional[str]) -> str:
    cfg        = config_loader.load()
    persona    = cfg.get("bot_persona", "developer Indonesia 3 tahun, python + js, ngulik AI")
    cv_summary = cfg.get("job_hunter_cv_summary", "")
    if not cv_summary:
        cv_summary = "dev ~3 tahun (python + js + react), lagi aktif ngulik AI tools"

    dm_hint = "Kamu HARUS sebut 'boleh DM?' atau 'bisa DM?' di komentar." if wants_dm else ""
    cv_hint = "Sebut singkat bahwa kamu siap kirim CV/portfolio kalau diminta." if cv_req else ""

    sys_prompt = f"""lo developer IT Indonesia yang lagi cari kerja/project.
lo baru lihat lowongan yang cocok dan mau komen untuk melamar.

LATAR BELAKANG LO: {persona}
RINGKASAN CV/SKILL: {cv_summary}

ATURAN KOMENTAR LAMARAN:
- 1-2 kalimat, maks 220 karakter
- Natural, casual-profesional, bahasa Indonesia (boleh campur Inggris teknis)
- Sebutkan 1-2 skill yang paling relevan dengan lowongannya
- Tunjukkan antusias tapi tidak lebay
- JANGAN: kalimat template ("Saya tertarik dengan posisi ini"), JANGAN formil banget
- JANGAN: link, hashtag, emoji berlebihan (maks 1)
- JANGAN: "Halo kak", "permisi", basa-basi panjang
{dm_hint}
{cv_hint}

contoh BAGUS:
"tertarik nih, gue dev 3 tahun mainly python + fastapi, lagi ngulik AI juga — boleh DM?"
"interested! stack gue react + node, punya beberapa project SaaS. siap share portfolio"
"wah pas banget, gue flutter dev 2 tahun, punya apps yang udah live di playstore"
"gue bisa, fullstack (laravel + vue), biasanya solo project jadi ngerti dari BE sampe deploy"

contoh JELEK:
"Halo kak, saya sangat tertarik dengan posisi ini. Saya memiliki pengalaman 3 tahun di bidang..."
"Interested! I am a passionate developer with expertise in..."
"""

    user_prompt = f"""Lowongan ini:\n{job_text[:400]}\n\nTulis komentar lamaran (1-2 kalimat):"""

    try:
        raw   = llm_router.generate(user_prompt, system_prompt=sys_prompt)
        lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
        # Ambil baris terpanjang yang tidak seperti label/instruksi
        candidates = [l for l in lines if len(l) > 20 and not l.startswith(("#", "-", "•", "*"))]
        comment    = candidates[0] if candidates else lines[0] if lines else ""
        # Strip tanda kutip kalau ada
        comment = comment.strip('"').strip("'").strip()
        if comment and len(comment) <= 280:
            return comment
    except Exception as e:
        stats_tracker.log_error(f"[JH] LLM error: {e}")

    return ""


def generate_dm_message(job_text: str, job_type: str) -> str:
    cfg        = config_loader.load()
    persona    = cfg.get("bot_persona", "developer Indonesia 3 tahun")
    cv_summary = cfg.get("job_hunter_cv_summary", "dev ~3 tahun (python + js + react), ngulik AI")

    sys_prompt = f"""lo developer IT Indonesia yang DM recruiter soal lowongan yang mereka post.
lo sudah komen di postnya, sekarang mau DM intro singkat.

LATAR BELAKANG: {persona}
SKILL SUMMARY: {cv_summary}

ATURAN DM:
- 2-3 kalimat, natural, santai tapi sopan
- Sebutkan kamu lihat dari Threads
- Highlight 1-2 skill paling relevan
- Tutup dengan "available buat ngobrol lebih lanjut" atau semacamnya
- JANGAN: terlalu formal, terlalu panjang, template copy-paste
- JANGAN: link, hashtag"""

    user_prompt = f"Lowongan ini:\n{job_text[:300]}\n\nTulis DM singkat intro:"

    try:
        raw = llm_router.generate(user_prompt, system_prompt=sys_prompt)
        msg = raw.strip().strip('"').strip("'")
        if msg and len(msg) <= 500:
            return msg
    except Exception as e:
        stats_tracker.log_error(f"[JH] DM LLM error: {e}")
    return ""


# ── DM sender ─────────────────────────────────────────────────────────────────

def send_dm(session, poster_user_id: str, text: str) -> Optional[str]:
    """
    Kirim DM ke poster_user_id via Instagram direct_v2 API.
    Return item_id jika berhasil, None jika gagal.
    """
    ds_user_id = (
        session.cookies.get("ds_user_id", domain=".threads.com")
        or session.cookies.get("ds_user_id", domain=".instagram.com")
        or os.getenv("THREADS_DS_USER_ID", "")
    )
    csrf = (
        session.cookies.get("csrftoken", domain=".threads.com")
        or session.cookies.get("csrftoken", domain=".instagram.com")
        or os.getenv("THREADS_CSRF_TOKEN", "")
    )

    headers = {
        "accept":          "*/*",
        "content-type":    "application/x-www-form-urlencoded",
        "origin":          "https://www.instagram.com",
        "referer":         "https://www.instagram.com/direct/",
        "user-agent":      (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-ig-app-id":     "238260118697367",
        "x-csrftoken":     csrf,
    }

    # Step 1: broadcast text message langsung ke recipient
    payload = urlencode({
        "recipient_users": json.dumps([[str(poster_user_id)]]),
        "text":            text,
        "client_context":  str(uuid.uuid4()).replace("-", ""),
        "_uid":            str(ds_user_id),
        "_uuid":           str(uuid.uuid4()),
    })

    try:
        resp = session.post(
            "https://www.instagram.com/api/v1/direct_v2/threads/broadcast/text/",
            data=payload,
            headers=headers,
            timeout=20,
        )
        logger.info(f"[JH] DM status: {resp.status_code}")
        if resp.status_code == 200:
            data    = resp.json()
            item_id = (data.get("payload") or {}).get("item_id") or \
                      (data.get("thread") or {}).get("items", [{}])[0].get("item_id", "ok")
            return str(item_id)
        if resp.status_code in (400, 403, 401):
            logger.warning(f"[JH] DM gagal {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"[JH] DM error: {e}")
    return None


# ── comment sender (reuse auto_commenter logic) ────────────────────────────────

def send_comment(session, post_id: str, text: str) -> Optional[str]:
    """Kirim komentar ke post. Return comment_id atau None."""
    csrf = (
        session.cookies.get("csrftoken", domain=".threads.com")
        or session.cookies.get("csrftoken")
        or ""
    )
    headers = {
        **BASE_HEADERS,
        "x-csrftoken": csrf,
    }
    payload = urlencode({
        "caption":          text,
        "barcelona_source_reply_id": post_id,
        "publish_mode":     "text_post",
        "upload_id":        str(int(time.time() * 1000)),
        "text_post_app_info": json.dumps({
            "reply_id":   post_id,
            "entry_point": "create_reply",
            "text_with_entities": {"entities": [], "text": text},
        }),
    })
    try:
        resp = session.post(REPLY_ENDPOINT, data=payload, headers=headers, timeout=30, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if "login" in loc:
                raise SessionExpiredError("comment: redirect to login")
            return None
        if resp.status_code in (401, 403):
            raise SessionExpiredError(f"comment HTTP {resp.status_code}")
        if resp.status_code == 429:
            raise RateLimitError(int(resp.headers.get("Retry-After", 60)))
        if resp.status_code == 200:
            if "text/html" in resp.headers.get("content-type", ""):
                raise SessionExpiredError("comment: HTML response")
            media = resp.json().get("media") or {}
            return str(media.get("pk") or media.get("id") or "ok")
    except (SessionExpiredError, RateLimitError):
        raise
    except Exception as e:
        logger.warning(f"[JH] Comment error: {e}")
    return None


# ── main cycle ─────────────────────────────────────────────────────────────────

def run_once(session) -> int:
    """
    Satu siklus: fetch timeline → temukan lowongan IT → lamar.
    Return jumlah lamaran yang dikirim.
    """
    query_config = load_latest_query_config()
    tokens       = get_fresh_tokens(session)
    if not tokens.get("lsd"):
        logger.warning("[JH] Gagal dapat tokens, skip siklus ini")
        return 0

    raw = fetch_timeline(session, tokens, query_config)
    if not raw:
        logger.warning("[JH] Timeline kosong, skip")
        return 0

    # Parse posts dari raw GraphQL response
    from auto_commenter import extract_posts
    posts = extract_posts(raw)
    if not posts:
        logger.info("[JH] Tidak ada post di timeline")
        return 0

    cfg          = config_loader.load()
    blacklist    = [u.lower() for u in (cfg.get("blacklisted_accounts") or [])]
    applied_count = 0

    logger.info(f"[JH] {len(posts)} post di timeline, scan lowongan…")

    for post in posts:
        caption  = post.get("caption", "").strip()
        post_id  = str(post.get("post_id") or post.get("id", ""))
        username = (post.get("username") or "").lower()

        if not caption or not post_id:
            continue
        if username in blacklist:
            continue
        if _already_applied(post_id):
            continue

        ok, job_type = _is_job_post(caption)
        if not ok:
            continue

        wants_dm_flag = _wants_dm(caption)
        cv_req        = _extract_cv_instruction(caption)
        poster_id     = str(post.get("user_id") or post.get("owner_id") or "")

        logger.info(f"\n[JH] LOWONGAN DITEMUKAN dari @{username} ({job_type})")
        logger.info(f"  Post: {caption[:100]}…")
        logger.info(f"  wants_dm={wants_dm_flag}, cv_req={cv_req}")

        # Generate komentar
        comment_text = generate_application_comment(caption, job_type, wants_dm_flag, cv_req)
        if not comment_text:
            logger.warning(f"  [JH] LLM gagal generate komentar, skip")
            continue

        logger.info(f"  [JH] KOMENTAR: {comment_text}")

        # Kirim komentar
        time.sleep(random.uniform(3, 8))
        try:
            comment_id = send_comment(session, post_id, comment_text)
        except RateLimitError as e:
            logger.warning(f"  [JH] Rate limit saat komen — tunggu {e.retry_after}s")
            time.sleep(e.retry_after)
            continue
        except SessionExpiredError:
            raise

        if not comment_id:
            logger.warning(f"  [JH] Komentar gagal terkirim")
            stats_tracker.increment("job_comment_failures")
            continue

        logger.info(f"  [JH] Komentar terkirim ✓ (id={comment_id})")
        stats_tracker.increment("job_comments_sent")

        entry = {
            "ts":         time.time(),
            "date":       datetime.now().strftime("%Y-%m-%d %H:%M"),
            "username":   username,
            "user_id":    poster_id,
            "caption":    caption[:300],
            "job_type":   job_type,
            "comment_id": comment_id,
            "comment":    comment_text,
            "dm_sent":    False,
            "dm_id":      None,
            "status":     "applied",
        }

        # Kirim DM hanya kalau post minta DM
        if wants_dm_flag and poster_id:
            dm_text = generate_dm_message(caption, job_type)
            if dm_text:
                time.sleep(random.uniform(5, 12))
                dm_id = send_dm(session, poster_id, dm_text)
                if dm_id:
                    logger.info(f"  [JH] DM terkirim ✓ (id={dm_id})")
                    stats_tracker.increment("job_dms_sent")
                    entry["dm_sent"] = True
                    entry["dm_id"]   = dm_id
                    entry["dm_text"] = dm_text
                else:
                    logger.warning(f"  [JH] DM gagal")
        elif wants_dm_flag:
            logger.info(f"  [JH] Post minta DM tapi user_id tidak tersedia, skip DM")

        _mark_applied(post_id, entry)
        applied_count += 1

        # Jeda antar lamaran
        delay = random.uniform(45, 90)
        logger.info(f"  [JH] Jeda {delay:.0f}d sebelum post berikutnya…")
        time.sleep(delay)

    return applied_count


def main():
    from dotenv import load_dotenv; load_dotenv(override=True)

    sm = SessionManager()
    if not sm.load_session() or sm.validate_session().status != SessionStatus.VALID:
        logger.error("[JH] Session invalid — cek .env")
        sys.exit(1)
    session = sm.inject_session()

    cfg           = config_loader.load()
    scan_interval = int(cfg.get("job_hunter_scan_interval_minutes", 45))
    active_start  = int(cfg.get("job_hunter_active_start_hour", cfg.get("active_start_hour", 8)))
    active_end    = int(cfg.get("job_hunter_active_end_hour",   cfg.get("active_end_hour",   21)))

    logger.info(f"[JH] Job Hunter mulai — scan setiap {scan_interval} menit, jam {active_start:02d}–{active_end:02d}")

    while True:
        now_h = datetime.now().hour
        if not (active_start <= now_h < active_end):
            logger.info("[JH] Di luar jam aktif — tidur 15 menit")
            time.sleep(900)
            continue

        try:
            n = run_once(session)
            if n:
                logger.info(f"[JH] Siklus selesai — {n} lamaran dikirim")
            else:
                logger.info("[JH] Siklus selesai — tidak ada lowongan baru")
        except SessionExpiredError as e:
            logger.error(f"[JH] Session expired: {e}")
            sys.exit(1)
        except RateLimitError as e:
            logger.warning(f"[JH] Rate limited — tunggu {e.retry_after}s")
            time.sleep(e.retry_after)
            continue
        except Exception as e:
            logger.error(f"[JH] Error: {e}", exc_info=True)
            stats_tracker.log_error(f"[JH] {e}")

        wait = scan_interval * 60 + random.randint(-300, 300)
        logger.info(f"[JH] Tidur {wait//60} menit…")
        time.sleep(wait)


if __name__ == "__main__":
    main()
