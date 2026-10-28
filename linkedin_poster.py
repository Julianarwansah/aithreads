"""
linkedin_poster.py -- LinkedIn Auto Poster (konten IT/AI original)

Alur:
  1. Scan feed LinkedIn untuk tren IT/AI terkini
  2. Generate postingan original via llm_router
  3. Post via Voyager API /ugcPosts
  4. Simpan histori agar tidak mengulang topik
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
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import llm_router
import config_loader
import stats_tracker
from linkedin_commenter import fetch_feed, _should_comment, SessionExpiredError, RateLimitError
from services.linkedin_session_manager import LinkedInAuth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR          = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR     = os.getenv("PROFILE_DIR", _DIR)
_LAST_POST    = os.path.join(_DATA_DIR, "linkedin_last_post.json")
_POST_HISTORY = os.path.join(_DATA_DIR, "linkedin_post_history.json")

UGC_POSTS_URL = "https://www.linkedin.com/voyager/api/ugcPosts"

DEFAULT_TOPICS = [
    "ngoding dan debugging",
    "AI dan tools terbaru untuk developer",
    "developer life dan produktivitas",
    "tech opinion Indonesia",
    "belajar pemrograman pengalaman pribadi",
    "startup dan produk digital Indonesia",
    "open source tools favorit",
]

_post_history: list = []


# ── history helpers ───────────────────────────────────────────────────────────

def _load_history() -> list:
    try:
        if Path(_POST_HISTORY).exists():
            return json.loads(Path(_POST_HISTORY).read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_history(history: list):
    try:
        Path(_POST_HISTORY).write_text(
            json.dumps(history[-20:], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _get_last_post_time() -> float:
    try:
        if Path(_LAST_POST).exists():
            data = json.loads(Path(_LAST_POST).read_text(encoding="utf-8"))
            return float(data.get("ts", 0))
    except Exception:
        pass
    return 0.0


def _save_last_post(text: str):
    try:
        Path(_LAST_POST).write_text(
            json.dumps({"ts": time.time(), "text": text}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── trend scanner ─────────────────────────────────────────────────────────────

def _scan_trending(auth: LinkedInAuth) -> list:
    logger.info("[LI_POSTER] Scan tren IT/AI di feed…")
    try:
        posts = fetch_feed(auth, count=30)
    except Exception as e:
        logger.warning(f"[LI_POSTER] Gagal scan feed: {e}")
        return []

    result = []
    for p in posts:
        ok, _ = _should_comment(p["caption"])
        if not ok:
            continue
        engagement = p.get("likes", 0) + p.get("comments", 0) * 3
        result.append({**p, "engagement": engagement})

    result.sort(key=lambda x: x["engagement"], reverse=True)
    top = result[:5]

    if top:
        logger.info(f"[LI_POSTER] {len(top)} post tren ditemukan:")
        for i, p in enumerate(top, 1):
            logger.info(f"  {i}. {p['caption'][:70]}… ({p['likes']}👍 {p['comments']}💬)")
    else:
        logger.info("[LI_POSTER] Tidak ada tren — pakai topik default")

    return top


def _build_trend_summary(posts: list) -> str:
    if not posts:
        return "(tidak ada data tren — buat dari pengalaman sendiri)"
    lines = []
    for p in posts:
        snippet = p["caption"][:120].replace("\n", " ")
        if len(p["caption"]) > 120:
            snippet += "…"
        lines.append(f'- "{snippet}" ({p.get("likes",0)}👍 {p.get("comments",0)}💬)')
    return "\n".join(lines)


# ── content generator ─────────────────────────────────────────────────────────

def _clean_post(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    lines = []
    skip_labels = re.compile(r'^(postingan|draft|output|hasil|komentar|teks|konten)[:\s]', re.IGNORECASE)
    for ln in text.splitlines():
        ln = ln.lstrip('> ').strip()
        if skip_labels.match(ln):
            parts = ln.split(':', 1)
            if len(parts) == 2 and parts[1].strip():
                lines.append(parts[1].strip())
        else:
            if ln:
                lines.append(ln)
    return '\n'.join(lines).strip()


def _heuristic_score(text: str) -> float:
    if not text:
        return -999.0
    score = 100.0
    score -= len(text) * 0.15
    if "?" in text:
        score += 8
    score -= text.count("!") * 5
    for w in ["luar biasa", "sangat inspiring", "keren banget", "mantap jiwa"]:
        if w in text.lower():
            score -= 8
    return score


def generate_post(auth: LinkedInAuth) -> str:
    global _post_history
    history = _load_history()
    _post_history = history

    trend_posts   = _scan_trending(auth)
    trend_summary = _build_trend_summary(trend_posts)

    cfg    = config_loader.load()
    topics = cfg.get("linkedin_poster_topics") or cfg.get("poster_topics") or DEFAULT_TOPICS
    persona = cfg.get("linkedin_persona") or cfg.get("bot_persona", "")

    if trend_posts:
        topic_hint = trend_posts[0]["caption"][:80]
    else:
        topic_hint = random.choice(topics)

    recent = "\n".join(f"- {p}" for p in _post_history[-5:]) if _post_history else "(belum ada)"

    sys_prompt = f"""lo developer IT Indonesia yang aktif nulis di LinkedIn.
lo mau share insight/pengalaman singkat yang relate ke tren yang lagi dibahas orang.
bukan promosi, bukan sales pitch — cuma orang tech yang punya pendapat.

LATAR BELAKANG: {persona}

yang lagi rame di LinkedIn sekarang:
{trend_summary}

cara nulis LinkedIn yang bagus (bukan corporate speak):
- 2–4 kalimat maks, langsung ke inti
- Bahasa Indonesia boleh campur Inggris teknis (natural)
- Tone: casual-profesional (bukan formal kaku, bukan alay)
- Boleh: opini singkat, pengalaman kecil, pertanyaan reflektif
- DILARANG: "Excited to share...", "I am pleased to announce...", CTA lebay
- DILARANG: hashtag berlebihan (maks 2-3 kalau perlu)
- DILARANG: link, mention @orang, promosi produk

contoh JELEK:
"Sebagai seorang software engineer, saya sangat bersyukur bisa berbagi pengalaman berharga ini kepada komunitas yang luar biasa! 🚀🔥"

contoh BAGUS:
"dua tahun pakai AI buat ngoding, sekarang gue lebih sering debug output AI daripada debug kode sendiri. ada yang ngalamin ini juga?"
"ngl deploy hari jumat udah reflex bikin anxiety. padahal cuma push minor fix."
"terlalu sering gue skip dokumentasi pas deadline mepet, nyesel-nya juga nanti pas deadline mepet lagi"

postingan lo sebelumnya (JANGAN ulangi topik/pola):
{recent}

tema relevan sekarang: {topic_hint}"""

    user_prompt = "Tulis satu postingan LinkedIn pendek yang relate ke tema ini:"

    def _gen():
        two_prompt = (
            f"Tulis 2 versi postingan berbeda (2–4 kalimat masing-masing), "
            f"pisahkan dengan ---\n\n{user_prompt}"
        )
        raw = llm_router.generate(two_prompt, system_prompt=sys_prompt)
        logger.info(f"[LI_POSTER RAW]: {raw}")
        parts = [p.strip() for p in raw.split("---") if p.strip()]
        if len(parts) >= 2:
            candidates = [_clean_post(p) for p in parts[:2]]
            valid = [(t, _heuristic_score(t)) for t in candidates if t]
            if valid:
                best, sc = max(valid, key=lambda x: x[1])
                logger.info(f"  [LI_POSTER DRAFT] pilih skor {sc:.1f}")
                return best
        return _clean_post(raw)

    try:
        content = _gen()
        if not content:
            return ""

        # Self-critique: regenerate kalau kurang natural
        from auto_commenter import _score_naturalness
        score = _score_naturalness(content, topic_hint)
        logger.info(f"  [LI_POSTER CRITIQUE] Skor: {score}/10")
        if score < 7:
            retry = _gen()
            if retry:
                retry_score = _score_naturalness(retry, topic_hint)
                if retry_score > score:
                    content = retry

        if content:
            history.append(content)
            _save_history(history)

        return content

    except Exception as e:
        stats_tracker.log_error(f"[LI_POSTER] LLM error: {e}")
        return ""


# ── post sender ───────────────────────────────────────────────────────────────

def post_to_linkedin(auth: LinkedInAuth, text: str) -> Optional[str]:
    """Kirim postingan baru. Return post URN atau None."""
    session = auth.get_authenticated_session()
    if not session:
        raise SessionExpiredError("No session")

    # Dapatkan profile URN
    profile_id = auth.profile_id or ""
    if not profile_id:
        logger.error("[LI_POSTER] profile_id tidak tersedia — tidak bisa post")
        return None

    # Bersihkan format URN: ambil bagian terakhir setelah ":"
    if "urn:li:person:" in profile_id:
        person_urn = profile_id
    else:
        person_urn = f"urn:li:person:{profile_id.split(':')[-1]}"

    headers = {**auth.get_request_headers(), "content-type": "application/json"}
    body = json.dumps({
        "author":          person_urn,
        "lifecycleState":  "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary":    {"text": text},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    })

    try:
        resp = session.post(UGC_POSTS_URL, headers=headers, content=body, timeout=25)
    except Exception as e:
        raise SessionExpiredError(f"Post request failed: {e}")

    if resp.status_code in (401, 403):
        raise SessionExpiredError(f"Post returned {resp.status_code}")
    if resp.status_code == 429:
        raise RateLimitError(int(resp.headers.get("Retry-After", 90)))
    if resp.status_code in (200, 201):
        try:
            post_urn = resp.json().get("id") or resp.json().get("entityUrn", "")
        except Exception:
            post_urn = "ok"
        logger.info(f"[LI_POSTER] POST BERHASIL: {text[:60]}… urn={post_urn}")
        return post_urn

    logger.warning(f"[LI_POSTER] Post HTTP {resp.status_code}: {resp.text[:300]}")
    return None


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    from dotenv import load_dotenv; load_dotenv()

    auth = LinkedInAuth()
    if not auth.load_session():
        logger.error("[LI_POSTER] Session invalid — set LINKEDIN_LI_AT di .env")
        sys.exit(1)

    cfg            = config_loader.load()
    interval_hours = float(cfg.get("linkedin_poster_interval_hours", cfg.get("poster_interval_hours", 12)))
    active_start   = int(cfg.get("linkedin_active_start_hour", cfg.get("active_start_hour", 7)))
    active_end     = int(cfg.get("linkedin_active_end_hour",   cfg.get("active_end_hour",   23)))

    logger.info(f"[LI_POSTER] Mulai — interval {interval_hours}j, jam aktif {active_start:02d}–{active_end:02d}")

    while True:
        now_h = datetime.now().hour
        if not (active_start <= now_h < active_end):
            logger.info("[LI_POSTER] Di luar jam aktif — tidur 15 menit")
            time.sleep(900)
            continue

        last_post_ago = time.time() - _get_last_post_time()
        if last_post_ago < interval_hours * 3600:
            wait = interval_hours * 3600 - last_post_ago
            logger.info(f"[LI_POSTER] Terlalu cepat — tunggu {wait/3600:.1f}j lagi")
            time.sleep(min(wait, 1800))
            continue

        try:
            content = generate_post(auth)
            if not content:
                logger.warning("[LI_POSTER] Konten kosong — skip siklus ini")
                time.sleep(1800)
                continue

            logger.info(f"[LI_POSTER] DRAF POST: {content}")
            post_urn = post_to_linkedin(auth, content)
            if post_urn:
                _save_last_post(content)
                stats_tracker.increment("li_posts_sent")
            else:
                logger.warning("[LI_POSTER] Post gagal terkirim")
                stats_tracker.increment("li_errors")

        except SessionExpiredError as e:
            logger.error(f"[LI_POSTER] Session expired: {e} — berhenti")
            sys.exit(1)
        except RateLimitError as e:
            logger.warning(f"[LI_POSTER] Rate limited — tunggu {e.retry_after}s")
            time.sleep(e.retry_after)
            continue

        sleep_secs = interval_hours * 3600 + random.randint(-1800, 1800)
        logger.info(f"[LI_POSTER] Tidur {sleep_secs/3600:.1f}j…")
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
