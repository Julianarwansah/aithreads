"""
linkedin_commenter.py -- LinkedIn Auto Commenter (IT/AI Indonesia)

Alur:
  1. Fetch feed via Voyager API /feed/updatesV2
  2. Filter postingan IT/AI dengan keyword yang sama seperti Threads
  3. Generate komentar natural via llm_router
  4. Kirim komentar via /socialActions/{activityUrn}/comments
  5. Simpan ke reply cache
"""

import json
import logging
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from dotenv import load_dotenv
load_dotenv()

import stats_tracker
import llm_router
import config_loader
import comment_memory
from services.linkedin_session_manager import LinkedInAuth, SessionStatus

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR       = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.getenv("PROFILE_DIR", _DIR)

FEED_URL      = "https://www.linkedin.com/voyager/api/feed/updatesV2"
COMMENT_URL   = "https://www.linkedin.com/voyager/api/socialActions/{urn}/comments"
LIKES_URL     = "https://www.linkedin.com/voyager/api/socialActions/{urn}/likes"

_REPLIED_FILE = os.path.join(_DATA_DIR, "linkedin_replied.json")
_POST_HISTORY = os.path.join(_DATA_DIR, "linkedin_post_history.json")

IT_AI_KEYWORDS = [
    # programming & dev
    "python", "javascript", "typescript", "golang", "rust", "java", "kotlin", "swift",
    "react", "vue", "angular", "nextjs", "node", "django", "fastapi", "laravel",
    "docker", "kubernetes", "devops", "cicd", "git", "github", "gitlab",
    "backend", "frontend", "fullstack", "mobile", "android", "ios",
    "ngoding", "coding", "programmer", "developer", "software engineer",
    "debugging", "deploy", "production", "staging", "microservice", "api",
    # AI & ML
    "machine learning", "deep learning", "neural network", "llm", "gpt", "claude",
    "chatgpt", "gemini", "copilot", "cursor", "ai tools", "prompt engineering",
    "data science", "nlp", "computer vision", "model", "training", "fine-tuning",
    "generative ai", "gen ai", "artificial intelligence", "kecerdasan buatan",
    # general tech
    "startup", "tech", "teknologi", "saas", "cloud", "aws", "gcp", "azure",
    "database", "sql", "nosql", "redis", "postgresql", "mongodb",
    "security", "cybersecurity", "linux", "open source",
    # Indonesian context
    "software", "it indonesia", "developer indonesia", "tech indonesia",
    "belajar coding", "belajar pemrograman", "career switch", "ganti karir",
]

_NEGATIVE_SIGNALS = [
    "lowongan", "hiring", "vacancy", "loker", "magang", "internship",
    "promo", "diskon", "jual", "beli", "iklan", "sponsor",
    "politik", "agama", "sara", "duka", "meninggal", "bencana",
]


class SessionExpiredError(Exception):
    pass


class RateLimitError(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited {retry_after}s")


class _RateBackoff:
    def __init__(self):
        self._events: list = []

    def record(self):
        now = time.time()
        self._events = [t for t in self._events if now - t < 3600]
        self._events.append(now)
        stats_tracker.log_error(f"[LI] Rate limited (event #{len(self._events)} dalam 1 jam)")

    def sleep_seconds(self, base: int = 90) -> int:
        return min(base * (2 ** max(0, len(self._events) - 1)), 3600)


_rate_backoff = _RateBackoff()
_comment_history: list = []


# ── keyword filter ────────────────────────────────────────────────────────────

def _should_comment(text: str) -> tuple[bool, str]:
    """True + alasan jika postingan layak dikomentari."""
    low = text.lower()

    for neg in _NEGATIVE_SIGNALS:
        if neg in low:
            return False, f"negative signal: {neg}"

    cfg = config_loader.load()
    extra = [k.lower() for k in (cfg.get("it_ai_keywords_extra") or [])]
    all_keywords = IT_AI_KEYWORDS + extra

    matched = [kw for kw in all_keywords if kw in low]
    if matched:
        return True, f"matched: {matched[0]}"
    return False, "no IT/AI keyword"


# ── feed fetcher ──────────────────────────────────────────────────────────────

def fetch_feed(auth: LinkedInAuth, count: int = 20) -> List[Dict]:
    """Fetch LinkedIn feed dan return list post yang relevan."""
    session = auth.get_authenticated_session()
    if not session:
        raise SessionExpiredError("No authenticated session")

    headers = auth.get_request_headers()
    params  = {"count": count, "start": 0, "q": "chronological"}

    try:
        resp = session.get(FEED_URL, headers=headers, params=params, timeout=20)
    except Exception as e:
        raise SessionExpiredError(f"Feed request failed: {e}")

    if resp.status_code == 401 or resp.status_code == 403:
        raise SessionExpiredError(f"Feed returned {resp.status_code}")
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 90))
        raise RateLimitError(retry_after)
    if resp.status_code != 200:
        logger.warning(f"[LI_COMMENTER] Feed HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    return _extract_posts(data)


def _extract_posts(data: Dict) -> List[Dict]:
    """Parse Voyager feed response jadi list post sederhana."""
    posts = []
    included = data.get("included", [])

    # Build lookup map dari included elements
    lookup: Dict[str, Any] = {}
    for item in included:
        urn = item.get("entityUrn") or item.get("$id", "")
        if urn:
            lookup[urn] = item

    # Elements di data.elements adalah list activity URNs
    elements = (data.get("data") or {}).get("elements", [])
    if not elements:
        # Beberapa versi Voyager langsung di root
        elements = data.get("elements", [])

    for urn in elements:
        if not isinstance(urn, str):
            continue
        activity = lookup.get(urn, {})
        if not activity:
            # Coba cari di included by activityUrn
            activity = next(
                (i for i in included if i.get("updateMetadata", {}).get("urn") == urn),
                {}
            )
        if not activity:
            continue

        post = _parse_activity(activity, urn, lookup)
        if post:
            posts.append(post)

    # Fallback: scan included langsung untuk UpdateV2
    if not posts:
        for item in included:
            t = item.get("$type", "")
            if "UpdateV2" in t or "NormFeedUpdate" in t:
                urn = item.get("entityUrn") or item.get("updateMetadata", {}).get("urn", "")
                if not urn:
                    continue
                post = _parse_activity(item, urn, lookup)
                if post:
                    posts.append(post)

    return posts


def _parse_activity(activity: Dict, urn: str, lookup: Dict) -> Optional[Dict]:
    """Extract caption, author, like/comment counts dari activity object."""
    # Caption bisa di beberapa tempat tergantung versi Voyager
    caption = ""

    # Path 1: commentary.text.text
    commentary = activity.get("commentary") or {}
    if isinstance(commentary, dict):
        text_obj = commentary.get("text") or {}
        caption = (text_obj.get("text") or "").strip() if isinstance(text_obj, dict) else ""

    # Path 2: actor share content
    if not caption:
        share = activity.get("content") or {}
        if isinstance(share, dict):
            caption = (share.get("description", {}) or {}).get("text", "").strip()

    if not caption or len(caption) < 10:
        return None

    # Author
    actor = activity.get("actor") or {}
    author = ""
    if isinstance(actor, dict):
        name_obj = actor.get("name") or {}
        author = name_obj.get("text", "") if isinstance(name_obj, dict) else ""

    # Engagement counts
    social = activity.get("socialDetail") or {}
    counts = (social.get("totalSocialActivityCounts") or {}) if isinstance(social, dict) else {}
    likes    = counts.get("numLikes", 0) or 0
    comments = counts.get("numComments", 0) or 0

    # Age check
    created_at = activity.get("actor", {}).get("subDescription", {})
    # Voyager encodes time as relative string, skip age filter for now

    return {
        "urn":      urn,
        "caption":  caption,
        "author":   author,
        "likes":    likes,
        "comments": comments,
    }


# ── replied cache ─────────────────────────────────────────────────────────────

def _load_replied() -> Dict:
    try:
        if Path(_REPLIED_FILE).exists():
            return json.loads(Path(_REPLIED_FILE).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_replied(data: Dict):
    try:
        Path(_REPLIED_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"[LI] Failed to save replied cache: {e}")


def _already_commented(urn: str) -> bool:
    return urn in _load_replied()


def _mark_commented(urn: str, comment_id: str, text: str):
    data = _load_replied()
    data[urn] = {"ts": time.time(), "comment_id": comment_id, "text": text}
    # Prune entries > 7 hari
    cutoff = time.time() - 7 * 86400
    data = {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
    _save_replied(data)


# ── comment generator ─────────────────────────────────────────────────────────

def _quality_check(text: str) -> bool:
    if not text or len(text) < 4:
        return False
    if len(text) > 280:
        return False
    # LinkedIn lebih formal: kurangi ekspresi terlalu santai
    ai_starters = re.compile(
        r'^(wah[,!\s]|haha|hehe|oh wow|keren banget|sangat\s|luar biasa|'
        r'betul sekali|tentu saja|pastinya|yep[,!\s]|iya banget)',
        re.IGNORECASE,
    )
    if ai_starters.search(text):
        return False
    if text.count('!') > 2:
        return False
    return True


def _heuristic_score(text: str) -> float:
    if not text or text == "SKIP":
        return -999.0
    score = 100.0
    score -= len(text) * 0.2
    if "?" in text:
        score += 8
    score -= text.count("!") * 6
    for w in ["keren banget", "luar biasa", "mantap", "sangat inspiring", "semangat"]:
        if w in text.lower():
            score -= 6
    return score


def _parse_reply(raw: str) -> str:
    raw = raw.strip()
    if raw.upper() == "SKIP" or not raw:
        return "SKIP"
    if re.search(r'https?://\S+', raw, re.IGNORECASE):
        return "SKIP"
    cfg = config_loader.load()
    max_len = int(cfg.get("max_comment_length") or 350)
    if len(raw) > max_len:
        raw = raw[:max_len - 3] + "..."
    return raw


def generate_comment(caption: str) -> str:
    cfg     = config_loader.load()
    persona = cfg.get("linkedin_persona") or cfg.get("bot_persona", "").strip()
    memory  = comment_memory.build_memory_prompt(n=5)

    sys_prompt = f"""lo orang IT Indonesia yang aktif di LinkedIn. kalau nemu postingan tech yang menarik, lo nulis komentar singkat — bukan copywriter, bukan recruiter, bukan sales. lo developer yang relate.

LATAR BELAKANG: {persona}

ATURAN KOMENTAR LINKEDIN:
- 1–2 kalimat, maks 200 karakter
- Bahasa Indonesia, boleh campur Inggris natural (bukan formal)
- Boleh pakai gaya casual tapi tetap profesional (bukan alay)
- JANGAN: "Sangat inspiring!", "Luar biasa!", "Semangat terus!", "Setuju banget!"
- JANGAN: link, mention @orang, hashtag
- BOLEH: nanya singkat, relate, share perspektif dev kecil
- Kalau postingan lowongan/promo/politik: balas SKIP

Komentar lo sebelumnya (jangan ulangi pola):
{memory}"""

    user_prompt = f"Tulis komentar LinkedIn untuk postingan ini:\n{caption[:200]}"

    def _do_gen() -> str:
        two_prompt = (
            f"Tulis 2 versi komentar berbeda (1–2 kalimat masing-masing), "
            f"pisahkan dengan ---\n\n{user_prompt}"
        )
        raw = llm_router.generate(two_prompt, system_prompt=sys_prompt)
        logger.info(f"[LI_RAW LLM]: {raw}")
        parts = [p.strip() for p in raw.split("---") if p.strip()]
        if len(parts) >= 2:
            candidates = [_parse_reply(p) for p in parts[:2]]
            valid = [(t, _heuristic_score(t)) for t in candidates if t and t != "SKIP"]
            if valid:
                best, sc = max(valid, key=lambda x: x[1])
                logger.info(f"  [LI_DRAFT] pilih skor {sc:.1f}: '{best[:50]}'")
                return best
            return "SKIP"
        return _parse_reply(raw)

    try:
        reply = _do_gen()
        if reply == "SKIP":
            return "SKIP"
        if not _quality_check(reply):
            logger.info(f"  [LI_QC] Gagal — regenerate…")
            reply = _do_gen()
            if reply == "SKIP":
                return "SKIP"
        logger.info(f"[LI_FINAL]: {reply} ({len(reply)} kar)")
        comment_memory.save(caption, reply)
        _comment_history.append(reply)
        if len(_comment_history) > 3:
            _comment_history.pop(0)
        return reply
    except Exception as e:
        stats_tracker.log_error(f"[LI] LLM error: {e}")
        return "SKIP"


# ── like + comment sender ──────────────────────────────────────────────────────

def like_post(auth: LinkedInAuth, urn: str) -> bool:
    session = auth.get_authenticated_session()
    headers = {**auth.get_request_headers(), "content-type": "application/json"}
    encoded = urn.replace(":", "%3A")
    url     = LIKES_URL.format(urn=encoded)
    body    = json.dumps({"actor": f"urn:li:person:{auth.session_manager.session_data.li_at[:8]}"})
    try:
        resp = session.post(url, headers=headers, content=body, timeout=15)
        return resp.status_code in (200, 201)
    except Exception:
        return False


def send_comment(auth: LinkedInAuth, urn: str, text: str) -> Optional[str]:
    """Kirim komentar. Return comment_id atau None kalau gagal."""
    session = auth.get_authenticated_session()
    if not session:
        raise SessionExpiredError("No session")

    encoded = urn.replace(":", "%3A")
    url     = COMMENT_URL.format(urn=encoded)
    headers = {**auth.get_request_headers(), "content-type": "application/json"}
    body    = json.dumps({
        "message":           {"text": text},
        "parentCommentUrn":  None,
    })

    try:
        resp = session.post(url, headers=headers, content=body, timeout=20)
    except Exception as e:
        raise SessionExpiredError(f"Comment request failed: {e}")

    if resp.status_code == 401 or resp.status_code == 403:
        raise SessionExpiredError(f"Comment returned {resp.status_code}")
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 90))
        raise RateLimitError(retry_after)
    if resp.status_code in (200, 201):
        try:
            cid = resp.json().get("id") or resp.json().get("entityUrn", "")
        except Exception:
            cid = ""
        logger.info(f"[LI_COMMENTER] Komentar terkirim ke {urn[:40]}… id={cid}")
        return cid or "ok"

    logger.warning(f"[LI_COMMENTER] Comment HTTP {resp.status_code}: {resp.text[:200]}")
    return None


# ── main loop ─────────────────────────────────────────────────────────────────

def run_once(auth: LinkedInAuth):
    """Satu siklus: fetch feed → filter → comment pada 1 post."""
    logger.info("[LI_COMMENTER] Fetch feed…")
    posts = fetch_feed(auth)
    logger.info(f"[LI_COMMENTER] {len(posts)} post dari feed")

    cfg           = config_loader.load()
    blacklist     = [u.lower() for u in (cfg.get("blacklisted_accounts") or [])]
    like_first    = cfg.get("like_before_comment", True)

    candidates = []
    for p in posts:
        urn     = p["urn"]
        caption = p["caption"]
        author  = p.get("author", "").lower()

        if _already_commented(urn):
            continue
        if author in blacklist:
            continue

        ok, reason = _should_comment(caption)
        if not ok:
            logger.info(f"  [SKIP] {caption[:50]}… — {reason}")
            continue

        candidates.append(p)

    if not candidates:
        logger.info("[LI_COMMENTER] Tidak ada post yang cocok di siklus ini")
        stats_tracker.increment("li_posts_skipped")
        return

    # Pilih 1 post terbaik (engagement tertinggi)
    candidates.sort(key=lambda x: x.get("likes", 0) + x.get("comments", 0) * 2, reverse=True)
    post = candidates[0]

    urn     = post["urn"]
    caption = post["caption"]
    logger.info(f"[LI_COMMENTER] Target: {caption[:80]}…")

    comment = generate_comment(caption)
    if comment == "SKIP":
        logger.info("[LI_COMMENTER] Komentar di-SKIP oleh LLM")
        stats_tracker.increment("li_comments_skipped")
        return

    # Like dulu biar lebih natural
    if like_first:
        liked = like_post(auth, urn)
        logger.info(f"  [LI_LIKE] {'OK' if liked else 'SKIP'}")
        time.sleep(random.uniform(3, 8))

    cid = send_comment(auth, urn, comment)
    if cid:
        _mark_commented(urn, cid, comment)
        stats_tracker.increment("li_comments_sent")
        stats_tracker.log_comment(comment)
        logger.info(f"[LI_COMMENTER] SENT: {comment}")
    else:
        stats_tracker.increment("li_errors")


def main():
    from dotenv import load_dotenv; load_dotenv()

    auth = LinkedInAuth()
    if not auth.load_session():
        logger.error("[LI_COMMENTER] Session invalid — set LINKEDIN_LI_AT di .env")
        sys.exit(1)

    cfg = config_loader.load()
    delay_min = int(cfg.get("linkedin_delay_min_minutes", 15)) * 60
    delay_max = int(cfg.get("linkedin_delay_max_minutes", 25)) * 60
    active_start = int(cfg.get("linkedin_active_start_hour", cfg.get("active_start_hour", 7)))
    active_end   = int(cfg.get("linkedin_active_end_hour",   cfg.get("active_end_hour",   23)))

    logger.info(f"[LI_COMMENTER] Mulai — delay {delay_min//60}–{delay_max//60} menit, jam aktif {active_start:02d}–{active_end:02d}")

    while True:
        now_h = datetime.now().hour
        if not (active_start <= now_h < active_end):
            wait = 900
            logger.info(f"[LI_COMMENTER] Di luar jam aktif — tidur {wait//60} menit")
            time.sleep(wait)
            continue

        try:
            run_once(auth)
        except SessionExpiredError as e:
            logger.error(f"[LI_COMMENTER] Session expired: {e} — berhenti")
            sys.exit(1)
        except RateLimitError as e:
            _rate_backoff.record()
            wait = _rate_backoff.sleep_seconds(e.retry_after)
            logger.warning(f"[LI_COMMENTER] Rate limited — tunggu {wait}s")
            time.sleep(wait)
            continue

        delay = random.randint(delay_min, delay_max)
        logger.info(f"[LI_COMMENTER] Tidur {delay//60} menit {delay%60} detik…")
        time.sleep(delay)


if __name__ == "__main__":
    main()
