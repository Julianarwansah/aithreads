"""
auto_commenter.py -- Threads Timeline Fetcher (Natural Commenter)
"""
import json
import logging
import os
import re
import sys
import time
import random
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

# WAJIB sebelum import modul lokal, supaya isi .env sudah termuat
# (llm_router & reply_cache membaca environment saat di-import)
from dotenv import load_dotenv
load_dotenv()

import stats_tracker
import reply_cache
import agent_loader
import llm_router
import config_loader
import comment_memory
from services.session_manager import SessionManager, SessionStatus

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

ANTI_AI_PROMPT = ""

_comment_history = []

THREADS_HOME        = "https://www.threads.com/"
GRAPHQL_URL         = "https://www.threads.com/graphql/query"
LIKE_DOC_ID         = "24753372994365040"
LIKE_FRIENDLY_NAME  = "useTHLikeMutationLikeMutation"
THREAD_ITEMS_URL    = "https://www.threads.com/api/v1/text_feed/{}/thread_items/"
GRAPHQL_DUMP  = "graphql_dump.json"
REPLY_ENDPOINT = "https://www.threads.com/api/v1/media/configure_text_only_post/"
AUTO_MODE = True

_TARGET_NAMES = {"BarcelonaFeedDirectQuery", "BarcelonaHomeTimeline", "BarcelonaFeedQuery"}

BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://www.threads.com",
    "referer": "https://www.threads.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "x-ig-app-id": "238260118697367",
    "x-asbd-id": "359341",
    "x-fb-friendly-name": "BarcelonaFeedDirectQuery",
}



def _strip_markdown_structure(text: str) -> str:
    """Cabut teks komentar nyata dari format markdown multi-bagian Qoder.

    Jika Qoder mengembalikan format:
        **Evaluasi:** ...
        **Komentar:**
        > teks komentar di sini
    maka ambil baris setelah "**Komentar:**" atau semua baris "> ...".
    Kalau tidak ada penanda itu, kembalikan teks apa adanya.
    """
    # Cek apakah ada blok "**Komentar:**"
    komentar_marker = re.search(r'\*\*[Kk]omentar[:\s]*\*\*', text)
    if komentar_marker:
        after = text[komentar_marker.end():].strip()
        # Ambil baris blockquote ("> ...") atau teks polos setelah marker
        lines = [ln.lstrip('> ').strip() for ln in after.splitlines() if ln.strip()]
        return ' '.join(lines)

    # Kalau semua baris adalah blockquote ("> "), gabungkan
    lines = text.splitlines()
    if lines and all(ln.startswith('>') or not ln.strip() for ln in lines if ln.strip()):
        return ' '.join(ln.lstrip('> ').strip() for ln in lines if ln.strip())

    # Hapus baris yang hanya berisi label markdown (**Evaluasi:** dst.)
    cleaned_lines = [ln for ln in lines if not re.match(r'^\s*\*\*\w[^*]*\*\*\s*$', ln)]
    return '\n'.join(cleaned_lines).strip()


def parse_ai_reply(reply: str) -> str:
    cleaned_reply = _strip_markdown_structure(reply.strip())

    # 1. Cek SKIP Intent
    if cleaned_reply.upper() == "SKIP":
        return "SKIP"

    # Jika AI tetap menyelipkan link, buang seluruh balasannya.
    # Menghapus link-nya saja menyisakan kalimat menggantung ("cek aja di ...").
    # Pola kedua ikut menangkap domain tanpa protokol ("shope.ee/4vKx9") karena
    # Threads tetap merendernya sebagai link yang bisa diklik.
    # TLD wajib huruf supaya angka kiểu Indonesia ("150.000") tidak ikut kena.
    link_patterns = (
        r'https?://\S+',
        r'\b(?:[\w-]+\.)+[a-zA-Z]{2,}(?:/\S*)?\b',
    )
    if any(re.search(p, cleaned_reply, flags=re.IGNORECASE) for p in link_patterns):
        print(f"[VALIDATION FAILED] Terdeteksi link, balasan dibuang: {cleaned_reply}")
        return "SKIP"

    if not cleaned_reply:
        return "SKIP"

    # 2. Cek Banned Phrases (Post-Generation Validation)
    banned_phrases = ["bener banget nih", "semangat terus ya", "kalau butuh cek ini", "saya rekomendasikan", "coba produk ini"]
    if any(banned in cleaned_reply.lower() for banned in banned_phrases):
        print(f"[VALIDATION FAILED] Terdeteksi frasa dilarang: {cleaned_reply}")
        return "SKIP"

    MAX_REPLY_LENGTH = int(config_loader.get("max_comment_length") or 350)
    if len(cleaned_reply) > MAX_REPLY_LENGTH:
        cleaned_reply = cleaned_reply[:MAX_REPLY_LENGTH - 3] + "..."

    return cleaned_reply

# ── banned phrases untuk quality filter ──────────────────────────────────────
_AI_SOUNDING_PATTERNS = re.compile(
    r'^(wah[,!\s]|haha[,!\s]|hehe[,!\s]?|oh wow|relate banget|keren banget|semangat|'
    r'pasti bisa|luar biasa|sangat\s|betul sekali|memang benar|tentu saja|'
    r'tentu aja|pastinya|yep[,!\s]|yap[,!\s]|iya banget|benar banget)',
    re.IGNORECASE,
)
_AI_FILLER_PHRASES = re.compile(
    r'(semangat terus|pasti bisa|luar biasa|sangat bermanfaat|sangat membantu|'
    r'jangan lupa|tetap semangat|keep it up|you got this|inspiring banget|'
    r'keren banget sih|mantap jiwa|top banget|bagus banget sih)',
    re.IGNORECASE,
)


def _quality_check(text: str) -> bool:
    """
    True = komentar lolos, False = terdeteksi pola AI / tidak natural.
    Dicek SEBELUM posting; kalau False, generate ulang sekali.
    """
    if not text or len(text) < 4:
        return False
    if len(text) > 220:
        return False
    if _AI_SOUNDING_PATTERNS.search(text):
        return False
    if _AI_FILLER_PHRASES.search(text):
        return False
    # Terlalu banyak tanda seru = hype berlebihan
    if text.count('!') > 2:
        return False
    # Kalimat terlalu panjang & formal (ada "yang" + "untuk" + "adalah" dalam 1 teks)
    formal_words = sum(1 for w in ['yang', 'untuk', 'adalah', 'merupakan', 'dalam rangka'] if w in text.lower())
    if formal_words >= 3:
        return False
    return True


def _heuristic_score(text: str) -> float:
    """Nilai draft tanpa LLM call — pilih yang paling natural dari 2 kandidat."""
    if not text or text == "SKIP":
        return -999.0
    score = 100.0
    score -= len(text) * 0.3
    if "?" in text:
        score += 8
    score -= text.count("!") * 8
    for w in ["sangat", "keren banget", "luar biasa", "mantap", "semangat", "pasti bisa", "inspiring"]:
        if w in text.lower():
            score -= 6
    for w in ["adalah", "merupakan", "dalam rangka"]:
        if w in text.lower():
            score -= 5
    return score


def _score_naturalness(draft: str, caption: str) -> int:
    """Minta LLM nilai naturalness 1–10. Default 7 (lolos) kalau LLM error."""
    prompt = (
        "Nilai seberapa natural komentar Threads berikut (1=robot banget, 10=manusia beneran).\n"
        "Jawab HANYA angka, tidak ada teks lain.\n\n"
        f"Post: {caption[:80]}\n"
        f"Komentar: {draft}"
    )
    try:
        resp = llm_router.generate(prompt)
        m = re.search(r'\b(10|[1-9])\b', resp)
        return int(m.group()) if m else 7
    except Exception:
        return 7


def _build_engagement_constraint() -> str:
    """Kembalikan instruksi keras berbasis data engagement historis."""
    try:
        stats = comment_memory.get_engagement_stats()
        if not stats:
            return ""
        by_style = stats.get("by_style", {})
        best = stats.get("best_style", "")
        if best and by_style.get(best, {}).get("total", 0) >= 5:
            rate = by_style[best].get("rate", 0)
            if rate >= 0.4:
                return (
                    f"\n🎯 DATA REAL: gaya '{best}' paling sering dibalas orang ({rate:.0%})"
                    f" — WAJIB prioritaskan gaya ini"
                )
    except Exception:
        pass
    return ""


def detect_post_sentiment(caption: str) -> str:
    """
    Deteksi mood postingan: frustrated | win | question | opinion | info
    Dipakai untuk menyesuaikan nada komentar.
    """
    low = caption.lower()

    frustrated_signals = [
        'stuck', 'error terus', 'nyerah', 'cape', 'capek', 'hopeless',
        'mau nangis', 'susah banget', 'ga bisa-bisa', 'ga ngerti', 'frustasi',
        'kesel', 'desperate', 'udah berapa jam', 'udah berapa hari', 'ga jalan',
        'kenapa sih', 'kok ga bisa', 'masih error', 'error lagi',
    ]
    win_signals = [
        'berhasil', 'akhirnya', 'finally', 'solved', 'done', 'selesai',
        'yeay', 'yay', 'alhamdulillah', 'bisa juga', 'gue berhasil',
        'deploy berhasil', 'udah jalan', 'udah bisa',
    ]
    question_signals = [
        'ada yang tau', 'ada yang pernah', 'gimana cara', 'minta saran',
        'tolong bantu', 'recommend', 'rekomendasi', 'ada yang bisa',
        'bantuin dong', 'cara nya gimana', 'kalian gimana',
    ]
    opinion_signals = [
        'menurut gue', 'menurut lo', 'tbh', 'ngl', 'fight me',
        'unpopular opinion', 'hot take', 'gue rasa', 'kayaknya sih',
        'beda sama', 'kontroversial',
    ]

    if any(s in low for s in frustrated_signals):
        return "frustrated"
    if any(s in low for s in win_signals):
        return "win"
    if low.strip().endswith('?') or any(s in low for s in question_signals):
        return "question"
    if any(s in low for s in opinion_signals):
        return "opinion"
    return "info"


def detect_thread_tone(replies: List[str]) -> str:
    """
    Analisis komentar yang sudah ada di thread untuk deteksi tone dominan.
    Return: "nanya" | "sharing" | "debat" | "mixed"
    """
    if not replies:
        return "mixed"

    nanya   = sum(1 for r in replies if r.strip().endswith("?") or
                  re.search(r'\b(gimana|kenapa|berapa|apa|siapa|kapan|blm|solved|pake apa)\b', r.lower()))
    saran   = sum(1 for r in replies if
                  re.search(r'\b(coba|mending|pake|gunain|harusnya|install|bisa)\b', r.lower()))
    debat   = sum(1 for r in replies if
                  re.search(r'\b(ga setuju|beda pendapat|menurutku|sebenarnya|justru|malah|bukan)\b', r.lower()))

    total = len(replies)
    if total == 0:
        return "mixed"
    if nanya / total > 0.5:
        return "nanya"
    if debat / total > 0.4:
        return "debat"
    if saran / total > 0.5:
        return "sharing"  # banyak saran = thread sharing pengalaman/solusi
    return "mixed"


def generate_reply(caption: str, existing_replies: List[str] = None) -> str:
    """
    Generate komentar natural.

    existing_replies: komentar orang lain yang sudah ada di thread ini
    """
    cfg = config_loader.load()

    # ── persona konsisten ─────────────────────────────────────────────────────
    persona = cfg.get("bot_persona", "").strip()

    # ── engagement constraint dari data historis ──────────────────────────────
    engagement_constraint = _build_engagement_constraint()

    # ── deteksi sentiment postingan ───────────────────────────────────────────
    sentiment = detect_post_sentiment(caption)
    sentiment_hints = {
        "frustrated": "postingan ini curhat/frustasi — empati dulu, JANGAN langsung kasih solusi atau saran",
        "win":        "postingan ini cerita berhasil — ikut relate atau share pengalaman serupa, jangan nanya 'udah solved?'",
        "question":   "postingan ini nanya sesuatu — lo bisa jawab singkat atau nanya balik kalau penasaran",
        "opinion":    "postingan ini opini/hot take — lo bisa setuju, nuansi, atau share perspektif lo sendiri",
        "info":       "",
    }
    sentiment_block = sentiment_hints.get(sentiment, "")

    # ── memory komentar kita sendiri ──────────────────────────────────────────
    memory_prompt = comment_memory.build_memory_prompt(n=8)

    # ── deteksi tone thread ───────────────────────────────────────────────────
    thread_tone = detect_thread_tone(existing_replies or [])
    tone_hints = {
        "nanya":   "thread ini banyak yang nanya — lo bisa jawab/relate atau nanya lanjut",
        "sharing": "thread ini banyak yang share solusi — react/nanya juga oke, jangan saran lagi",
        "debat":   "thread ini ada debat kecil — hindari trigger debat, cukup react atau nanya netral",
        "mixed":   "",
    }
    tone_block = tone_hints.get(thread_tone, "")

    # ── konteks komentar orang lain ───────────────────────────────────────────
    if existing_replies:
        others_block = "komentar orang lain yang SUDAH ADA di thread ini (jangan duplikat):\n"
        others_block += "\n".join(f'- "{r[:80]}"' for r in existing_replies[:5])
        hint = tone_block or "kalau orang-orang nanya, lo bisa jawab/relate. kalau udah banyak saran, jangan saran lagi."
        others_block += f"\n→ {hint}"
    else:
        others_block = "(belum ada komentar lain di thread ini)"

    base_prompt = agent_loader.load_agent('threads-natural-commenter-agent')

    # Bangun blok sentiment (hanya tampil kalau ada)
    sentiment_line = f"\n⚡ KONTEKS: {sentiment_block}" if sentiment_block else ""

    if base_prompt:
        sys_prompt = (base_prompt
                      .replace("{persona}", persona)
                      .replace("{sentiment}", sentiment_line)
                      .replace("{recent_comments}", memory_prompt)
                      .replace("{others_comments}", others_block))
        if engagement_constraint:
            sys_prompt += engagement_constraint
        user_prompt = caption
    else:
        persona_line = f"\nLATAR BELAKANG LO: {persona}\n" if persona else ""
        sys_prompt = f"""lo orang IT Indonesia yang sering buka Threads. kalau nemu postingan yang relate, lo nimbrung — tapi lo BUKAN asisten, lo bukan guru, lo cuma orang yang lagi scroll sambil rebahan.
{persona_line}
TIPE KOMENTAR (pilih yang paling pas, bukan selalu kasih saran):

1. NANYA — lo penasaran, mau lanjutin obrolan
   contoh: "ini udah solved blm?"
   contoh: "lo pake apa akhirnya?"
   contoh: "framework-nya apa emg?"

2. REACT / NGOBROL — lo cuma relate, ga perlu kasih apa-apa
   contoh: "ini gue banget kemarin"
   contoh: "serius? gue kira cuma gue doang"
   contoh: "anjir sama persis"

3. SHARE SINGKAT — lo pernah ngalamin, share dikit aja
   contoh: "gue kemarin 3 hari stuck di sini"
   contoh: "sama, akhirnya gue tinggalin aja dulu"

4. SARAN (JARANG — hanya kalau beneran natural, bukan default)
   contoh: "coba cek log-nya dulu sih"

POLA NULIS:
- 1 kalimat pendek, kadang 4–6 kata cukup
- bahasa santai: "gue", "lo", "sih", "aja", "emg", "blm", "udh", "tp", "jg", "yg"
- boleh typo kecil biar keliatan manusia beneran
- JANGAN mulai dengan "Wah", "Haha", "Hehe", "Oh", "Ya", "Iya", "Relate banget"
- JANGAN kasih saran kalau ga diminta
- DILARANG: link, nama produk spesifik, promosi
- kalau postingan duka/politik panas/NSFW: balas SKIP

CONTOH JELEK (keliatan AI):
"Relate banget nih! Emang susah sih, coba mulai dari yang simpel dulu biar ga overwhelming."
"Wah keren banget progress lo! Semangat terus ya, pasti bisa!"{sentiment_line}

{others_block}

komentar lo sebelumnya — jangan ulangi pola/nada yang sama:
{memory_prompt}{engagement_constraint}"""
        user_prompt = caption

    def _do_generate():
        # Minta 2 draft sekaligus; pilih yang skor heuristic-nya lebih tinggi
        two_draft_prompt = (
            f"Tulis 2 versi komentar yang berbeda gaya (1 kalimat masing-masing), "
            f"pisahkan dengan ---\n\n{user_prompt}"
        )
        r = llm_router.generate(two_draft_prompt, system_prompt=sys_prompt)
        print(f"\n[RAW LLM]: {r}")
        parts = [p.strip() for p in r.split("---") if p.strip()]
        if len(parts) >= 2:
            candidates = [parse_ai_reply(p) for p in parts[:2]]
            valid = [(t, _heuristic_score(t)) for t in candidates if t and t != "SKIP"]
            if valid:
                best_text, best_sc = max(valid, key=lambda x: x[1])
                print(f"  [DRAFT] {len(valid)} kandidat valid, pilih skor heuristik {best_sc:.1f}: '{best_text[:50]}'")
                return best_text
            return "SKIP"
        return parse_ai_reply(r)

    try:
        cached_reply = reply_cache.check_cache(caption)
        if cached_reply:
            parsed_cache = parse_ai_reply(cached_reply)
            if parsed_cache == "SKIP":
                return "SKIP"
            return parsed_cache

        reply = _do_generate()
        if reply == "SKIP":
            return "SKIP"

        # ── quality filter — regenerate sekali kalau gagal ────────────────────
        if not _quality_check(reply):
            print(f"  [QC] Gagal quality check: '{reply[:60]}' — regenerate…")
            reply = _do_generate()
            if reply == "SKIP":
                return "SKIP"
            if not _quality_check(reply):
                print(f"  [QC] Masih gagal setelah regenerate — pakai hasil terakhir")

        # ── self-critique gate — LLM nilai naturalness ────────────────────────
        critique_score = _score_naturalness(reply, caption)
        print(f"  [CRITIQUE] Skor naturalness: {critique_score}/10")
        if critique_score < 7:
            print(f"  [CRITIQUE] Skor rendah ({critique_score}/10) — generate ulang…")
            retry = _do_generate()
            if retry and retry != "SKIP" and _quality_check(retry):
                reply = retry

        print(f"[FINAL]: {reply} ({len(reply)} kar)\n")

        comment_memory.save(caption, reply, post_sentiment=sentiment)

        _comment_history.append(reply)
        if len(_comment_history) > 3:
            _comment_history.pop(0)

        return reply
    except Exception as exc:
        stats_tracker.log_error(f"LLM Router Error: {exc}")
        stats_tracker.increment("llm_errors")
        logger.error("Gagal membuat balasan: %s", exc)
        return "SKIP"

def fetch_post_replies(session, post_id: str) -> List[str]:
    """
    Ambil komentar yang sudah ada di thread.
    Dipakai sebagai konteks prompt agar bot tidak duplikat & sesuai tone obrolan.
    """
    headers = dict(BASE_HEADERS)
    del headers["x-fb-friendly-name"]  # tidak perlu untuk REST GET
    csrf = session.cookies.get("csrftoken")
    if csrf:
        headers["x-csrftoken"] = csrf

    try:
        resp = session.get(THREAD_ITEMS_URL.format(post_id), headers=headers, timeout=10)
        if resp.status_code != 200:
            return []

        data  = resp.json()
        items = data.get("thread_items") or data.get("items") or []
        replies = []
        for item in items:
            media    = item.get("media") or item
            text_app = media.get("text_post_app_info") or {}
            # hanya ambil yang merupakan reply (ada reply_to_author), skip root post
            if not text_app.get("reply_to_author"):
                continue
            cap = (media.get("caption") or {}).get("text", "").strip()
            if cap and 5 < len(cap) < 200:
                replies.append(cap)

        return replies[:6]   # max 6 reply untuk prompt
    except Exception:
        return []


def like_post(session, tokens: Dict[str, str], post_id: str) -> bool:
    """Like sebuah post sebelum komentar agar lebih natural."""
    headers = dict(BASE_HEADERS)
    headers["x-fb-friendly-name"] = LIKE_FRIENDLY_NAME
    csrf = session.cookies.get("csrftoken")
    if csrf:
        headers["x-csrftoken"] = csrf

    variables = {
        "mediaID":     post_id,
        "requestData": {"container_module": "ig_text_feed_timeline"},
    }
    payload = urlencode({
        "lsd":                      tokens.get("lsd", ""),
        "fb_dtsg":                  tokens.get("fb_dtsg", ""),
        "doc_id":                   LIKE_DOC_ID,
        "variables":                json.dumps(variables),
        "server_timestamps":        "true",
        "fb_api_req_friendly_name": LIKE_FRIENDLY_NAME,
    })
    try:
        resp = session.post(GRAPHQL_URL, data=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return "errors" not in data and data.get("data") is not None
        return False
    except Exception as e:
        print(f"[LIKE] Error: {e}")
        return False


def post_reply(session, post_id: str, text: str) -> Optional[str]:
    """Kirim reply ke post_id. Return media_pk komentar baru jika berhasil, None jika gagal."""
    csrf = (
        session.cookies.get("csrftoken", domain=".threads.com")
        or session.cookies.get("csrftoken")
        or ""
    )

    headers = {
        "accept":           "*/*",
        "accept-language":  "en-US,en;q=0.9",
        "content-type":     "application/x-www-form-urlencoded",
        "origin":           "https://www.threads.com",
        "referer":          "https://www.threads.com/",
        "sec-fetch-dest":   "empty",
        "sec-fetch-mode":   "cors",
        "sec-fetch-site":   "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-ig-app-id":   "238260118697367",
        "x-csrftoken":   csrf,
    }

    payload = urlencode({
        "caption": text,
        "barcelona_source_reply_id": post_id,
        "publish_mode": "text_post",
        "upload_id": str(int(time.time() * 1000)),
        "text_post_app_info": json.dumps({"reply_id": post_id, "entry_point": "create_reply", "text_with_entities": {"entities": [], "text": text}}),
    })

    try:
        resp = session.post(REPLY_ENDPOINT, data=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            media = resp.json().get("media") or {}
            result = media.get("pk") or media.get("id")
            return str(result) if result else None
        return None
    except Exception as e:
        print(f"[REPLY_ERROR] {e}")
        return None

def human_delay():
    cfg = config_loader.load()
    lo  = int(cfg["delay_min_minutes"]) * 60
    hi  = int(cfg["delay_max_minutes"]) * 60

    # Smart peak-hour: sesuaikan delay berdasarkan pola aktivitas nyata
    multiplier = 1.0
    try:
        peak_hours   = reply_cache.get_peak_hours(top_n=5)
        current_hour = datetime.now().hour
        if peak_hours and current_hour in peak_hours:
            multiplier = 0.75
            print(f"  [SMART_DELAY] Jam peak ({current_hour:02d}:xx) — delay ×0.75")
        elif 1 <= current_hour <= 5:
            multiplier = 2.0
            print(f"  [SMART_DELAY] Dini hari ({current_hour:02d}:xx) — delay ×2.0")
    except Exception:
        pass

    lo_adj = max(5 * 60, int(lo * multiplier))
    hi_adj = max(lo_adj, int(hi * multiplier))
    delay  = random.randint(lo_adj, hi_adj)
    print(f"\n[INFO] Bot beristirahat {int(delay/60)} menit agar natural…")
    time.sleep(delay)

def load_latest_query_config() -> Dict[str, str]:
    # Default fallback dari capture yang sudah ada (hardcoded bypass)
    DEFAULT_DOC_ID = "37542980015300902"
    DEFAULT_VARIABLES = '{"data":{"pagination_source":"text_post_feed_threads","reason":"cold_start_fetch"},"variant":"for_you","__relay_internal__pv__BarcelonaIsLoggedInrelayprovider":true,"__relay_internal__pv__BarcelonaHasDearAlgoConsumptionrelayprovider":true,"__relay_internal__pv__BarcelonaHasEventBadgerelayprovider":false,"__relay_internal__pv__BarcelonaGenAIRepliesEnabledrelayprovider":false,"__relay_internal__pv__BarcelonaIsSearchDiscoveryEnabledrelayprovider":false,"__relay_internal__pv__BarcelonaHasCommunitiesrelayprovider":true,"__relay_internal__pv__BarcelonaHasGameScoreSharerelayprovider":false,"__relay_internal__pv__BarcelonaHasPublicViewCountCardrelayprovider":true,"__relay_internal__pv__BarcelonaHasCommunityEntityCardrelayprovider":true,"__relay_internal__pv__BarcelonaHasScorecardCommunityrelayprovider":true,"__relay_internal__pv__BarcelonaHasSportTeamAllegianceCardrelayprovider":false,"__relay_internal__pv__BarcelonaHasMusicrelayprovider":true,"__relay_internal__pv__BarcelonaHasNewspaperLinkStylerelayprovider":false,"__relay_internal__pv__BarcelonaMessagingHasGroupChatsrelayprovider":false,"__relay_internal__pv__BarcelonaHasMessagingrelayprovider":false,"__relay_internal__pv__BarcelonaHasViewerRepliedrelayprovider":false,"__relay_internal__pv__BarcelonaHasGhostPostEmojiActivationrelayprovider":false,"__relay_internal__pv__BarcelonaOptionalCookiesEnabledrelayprovider":true,"__relay_internal__pv__BarcelonaHasDearAlgoWebProductionrelayprovider":false,"__relay_internal__pv__BarcelonaHasWebFaviconsrelayprovider":false,"__relay_internal__pv__BarcelonaIsCrawlerrelayprovider":false,"__relay_internal__pv__BarcelonaHasCommunityTopContributorsrelayprovider":false,"__relay_internal__pv__BarcelonaCanSeeSponsoredContentrelayprovider":false,"__relay_internal__pv__BarcelonaShouldShowFediverseM075Featuresrelayprovider":true,"__relay_internal__pv__BarcelonaIsInternalUserrelayprovider":false}'
    
    dumps = []
    if os.path.exists(GRAPHQL_DUMP):
        with open(GRAPHQL_DUMP, "r", encoding="utf-8") as f:
            try:
                dumps = json.load(f)
            except json.JSONDecodeError:
                dumps = []
                
        if not isinstance(dumps, list):
            dumps = [dumps] if dumps else []

    for entry in dumps:
        if not isinstance(entry, dict):
            continue
            
        payload    = entry.get("request_payload") or {}
        doc_id     = entry.get("doc_id") or payload.get("doc_id", "")
        friendly   = entry.get("friendly_name") or payload.get("fb_api_req_friendly_name", "")
        variables  = payload.get("variables", "")
        
        # Ekstrak dari list jika hasil parsing berupa list (ex: dari parse_qs)
        if isinstance(doc_id, list): doc_id = doc_id[0] if doc_id else ""
        if isinstance(friendly, list): friendly = friendly[0] if friendly else ""
        if isinstance(variables, list): variables = variables[0] if variables else ""
        
        # Pastikan variables menjadi string untuk payload GraphQL
        variables_str = variables if isinstance(variables, str) else json.dumps(variables)
        
        if friendly and friendly in _TARGET_NAMES and doc_id:
            return {"doc_id": str(doc_id), "variables": variables_str}
        if "pagination_source" in variables_str and "text_post_feed_threads" in variables_str and doc_id:
            return {"doc_id": str(doc_id), "variables": variables_str}
            
    # Jika tidak ketemu atau file tidak ada, gunakan default fallback yang sudah di-capture
    print("\n[INFO] Menggunakan fallback GraphQL config bawaan (Default).")
    return {"doc_id": DEFAULT_DOC_ID, "variables": DEFAULT_VARIABLES}

def get_fresh_tokens(session) -> Dict[str, str]:
    tokens: Dict[str, str] = {"lsd": "", "fb_dtsg": ""}
    try:
        resp = session.get(THREADS_HOME, timeout=20)
        for pat in [r'"LSD",\[\],\{"token":"([^"]+)"\}', r'"lsd":"([^"]+)"']:
            m = re.search(pat, resp.text)
            if m: tokens["lsd"] = m.group(1); break
        for pat in [r'"DTSGInitialData",\[\],\{"token":"([^"]+)"\}', r'"fb_dtsg":"([^"]+)"']:
            m = re.search(pat, resp.text)
            if m: tokens["fb_dtsg"] = m.group(1); break
    except Exception:
        pass
    return tokens

def fetch_timeline(session, tokens: Dict[str, str], query_config: Dict[str, str]) -> Optional[Dict[str, Any]]:
    headers = dict(BASE_HEADERS)
    friendly  = query_config.get("friendly_name", "BarcelonaFeedDirectQuery")
    headers["x-fb-friendly-name"] = friendly
    csrf = session.cookies.get("csrftoken")
    if csrf: headers["x-csrftoken"] = csrf

    payload = urlencode({
        "lsd":       tokens.get("lsd", ""),
        "fb_dtsg":   tokens.get("fb_dtsg", ""),
        "doc_id":    query_config["doc_id"],
        "variables": query_config["variables"],
        "server_timestamps":        "true",
        "fb_api_req_friendly_name": friendly,
    })

    try:
        resp = session.post(GRAPHQL_URL, data=payload, headers=headers, timeout=30)
        print(f"[DEBUG] fetch_timeline status code: {resp.status_code}")
        if resp.status_code != 200:
            print(f"[DEBUG] fetch_timeline error response: {resp.text[:500]}")
            return None
        return json.loads(resp.text)
    except Exception as e:
        print(f"[DEBUG] fetch_timeline exception: {e}")
        return None

def extract_posts(json_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    posts: List[Dict[str, Any]] = []
    data = json_data.get("data") or {}
    feed_data = data.get("feedData") or (data.get("viewer") or {}).get("home_feed_units")
    if not feed_data:
        print(f"[DEBUG] extract_posts: feedData atau home_feed_units tidak ditemukan. Keys di data: {list(data.keys()) if data else 'data is None'}")
        return posts

    edges = feed_data.get("edges", []) if isinstance(feed_data, dict) else []
    for edge in edges:
        items = (edge.get("node") or {}).get("text_post_app_thread", {}).get("thread_items", [])
        if not items: continue
        post = items[0].get("post") or {}
        

        
        # --- METRICS DISCOVERY LOGGING ---
        like_count = post.get("like_count")
        text_post_app_info = post.get("text_post_app_info", {})
        triage_comments = post.get("triage_comments")
        
        reply_count = None
        if isinstance(text_post_app_info, dict):
            reply_count = text_post_app_info.get("direct_reply_count") or text_post_app_info.get("reply_count")
            
        print(f"[POST_METRICS] like_count={like_count} | reply_count={reply_count}")
        print(f"[TEXT_POST_APP_INFO] {text_post_app_info}")
        print(f"[TRIAGE_COMMENTS] {triage_comments}")
        print(f"[TREND_METRICS] likes={like_count} replies={reply_count}")
        # ---------------------
        
        user = post.get("user") or {}
        posts.append({
            "username":   user.get("username") or "(unknown)",
            "post_id":    post.get("pk") or post.get("id") or "?",
            "caption":    (post.get("caption") or {}).get("text", ""),
            "like_count": like_count,
            "reply_count": reply_count,
            "taken_at":   post.get("taken_at") or 0,
            "raw_post":   post,
        })
    return posts

# Konten sensitif — selalu SKIP, tidak perlu check keyword IT/AI
_SENSITIVE_PATTERNS = [
    # Duka / kematian
    r'\b(innalillahi|inna lillahi|semoga husnul khotimah|almarhum|almarhumah|'
    r'meninggal|telah tiada|berpulang|duka cita|turut berduka|rip\b|rest in peace)\b',
    # SARA / agama keras
    r'\b(kafir|murtad|sesat|neraka kalian|azab|haram banget lo|dasar munafik)\b',
    # Politik panas
    r'\b(coblos|golput|pilkada|pilpres|calon presiden|caleg|partai [a-z]+|'
    r'rezim|turunkan presiden|people power|#reformasi)\b',
    # Promosi / iklan / MLM
    r'\b(harga promo|diskon [0-9]+%|hubungi wa|wa kami|link bio|order sekarang|'
    r'limited stock|stok terbatas|garansi uang kembali|passive income|cuan jutaan)\b',
    # Hoax signals
    r'\b(viralkan|sebarkan|jangan sampai|ini nyata|ini fakta|bukti sudah ada|'
    r'share sebelum dihapus|tersebar|bocoran)\b',
]
_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in _SENSITIVE_PATTERNS]


def is_sensitive_content(caption: str) -> str:
    """Return kategori kalau konten sensitif, kosong kalau aman."""
    low = caption.lower()
    categories = ["duka/kematian", "SARA", "politik", "promosi/iklan", "hoax"]
    for pattern, category in zip(_SENSITIVE_RE, categories):
        if pattern.search(low):
            return category
    return ""


_LOKER_KEYWORDS = [
    "lowongan", "loker", "hiring", "we are hiring", "open rekrutmen",
    "open recruitment", "open position", "open vacancy", "dicari karyawan",
    "dibutuhkan segera", "bergabung bersama kami", "kirim cv", "kirim lamaran",
    "apply now", "daftarkan diri", "job desc", "job description",
    "kualifikasi:", "persyaratan:", "benefit:", "🚨 hiring", "📢 hiring",
    "walk in interview", "walk-in interview",
]

# Keyword yang aman untuk substring match (panjang/spesifik, tidak ada risiko false positive)
_IT_AI_KEYWORDS = [
    # Umum coding & dev
    "ngoding", "coding", "programmer", "developer", "programming",
    "debug", "deploy", "repository", "commit",
    "pull request", "merge", "refactor", "framework", "library",
    # Bahasa & stack
    "python", "javascript", "typescript", "golang", "flutter", "nextjs",
    "angular", "laravel", "django", "fastapi", "nodejs", "node.js",
    "nosql", "mongodb", "postgresql", "mysql", "redis",
    # Infra & tools
    "linux", "ubuntu", "docker", "kubernetes", "k8s", "ci/cd", "devops",
    "microservice", "nginx", "apache", "github", "gitlab",
    "xampp", "laragon", "wsl",
    # Perangkat & IT umum
    "komputer", "hardware", "cybersecurity", "hacking",
    "pentesting", "vulnerability", "firewall",
    # AI & ML
    "artificial intelligence", "machine learning",
    "deep learning", "neural network", "large language model",
    "chatgpt", "copilot", "qoder",
    "midjourney", "stable diffusion", "generative ai", "prompt engineer",
    "fine-tuning", "huggingface", "langchain", "vector db", "embedding",
    # Startup & tech
    "startup", "saas", "software engineer", "tech stack",
    "product manager", "ui/ux", "figma", "wireframe",
]

# Keyword pendek/ambigu — harus cocok sebagai KATA UTUH (word boundary)
# Contoh bug: "api" ↔ "tapi", "git" ↔ "gitu", "ram" ↔ "program"
_IT_AI_WORD_KEYWORDS = [
    "bug", "error", "server", "cloud", "api", "git", "repo", "sql",
    "php", "java", "rust", "vue", "ruby", "dart", "react", "swift", "kotlin",
    "aws", "gcp", "azure", "vps", "gpu", "ram", "vpn",
    "llm", "mvp", "rag",
    "laptop", "pc", "spek", "network", "jaringan",
    "terminal", "coding", "ngoding",
]


# Konteks non-IT yang bisa memicu false positive pada keyword umum
_CONTEXT_EXCLUSIONS = {
    # "developer" bisa = developer properti/perumahan di Indonesia
    "developer": ["perumahan", "properti", "apartemen", "kpr", "rumah", "kavling", "tanah", "ruko"],
    # "network" bisa = network marketing / MLM
    "network":   ["marketing", "mlm", "bisnis", "member", "downline", "upline", "reseller"],
}


def should_comment(caption: str) -> tuple[bool, str]:
    """Kembalikan (True, alasan) jika layak dikomentari, (False, alasan) jika tidak."""
    low  = caption.lower()
    cfg  = config_loader.load()

    loker_kws      = _LOKER_KEYWORDS       + [k.lower() for k in cfg.get("loker_keywords_extra",  [])]
    it_ai_kws      = _IT_AI_KEYWORDS       + [k.lower() for k in cfg.get("it_ai_keywords_extra", [])]
    it_ai_word_kws = _IT_AI_WORD_KEYWORDS

    # Cek konten sensitif paling awal (duka/SARA/politik/promosi/hoax)
    sensitive = is_sensitive_content(caption)
    if sensitive:
        return False, f"konten sensitif ({sensitive})"

    # Cek loker dulu
    for kw in loker_kws:
        if kw in low:
            return False, f"lowongan kerja ({kw!r})"

    def _context_ok(kw: str) -> bool:
        """False jika keyword match tapi konteksnya non-IT."""
        exclusions = _CONTEXT_EXCLUSIONS.get(kw, [])
        return not any(exc in low for exc in exclusions)

    # Substring match (keyword panjang/spesifik — aman)
    for kw in it_ai_kws:
        if kw in low:
            if not _context_ok(kw):
                continue
            return True, f"topik IT/AI ({kw!r})"

    # Word-boundary match (keyword pendek — hindari false positive seperti "api"→"tapi")
    for kw in it_ai_word_kws:
        if re.search(r'\b' + re.escape(kw) + r'\b', low):
            if not _context_ok(kw):
                continue
            return True, f"topik IT/AI word ({kw!r})"

    # ai/ml: hindari match "email", "trail", "gmail", dll
    if re.search(r'(?<!\w)ai(?!\w)', low) or re.search(r'(?<!\w)ml(?!\w)', low):
        return True, "topik IT/AI (ai/ml)"

    return False, "tidak ada kata kunci IT/AI"


def calculate_trend_score(likes: int, replies: int) -> int:
    like_score = 0
    if likes >= 300:
        like_score = 30
    elif likes >= 100:
        like_score = 20
    elif likes >= 30:
        like_score = 10
        
    reply_score = 0
    if replies >= 30:
        reply_score = 30
    elif replies >= 10:
        reply_score = 20
    elif replies >= 3:
        reply_score = 10
        
    return like_score + reply_score

def process_timeline(session):
    query_config = load_latest_query_config()
    tokens = get_fresh_tokens(session)
    print(f"[DEBUG] process_timeline: tokens fetched -> lsd={bool(tokens.get('lsd'))}, fb_dtsg={bool(tokens.get('fb_dtsg'))}")
    
    raw = fetch_timeline(session, tokens, query_config)
    
    if not raw:
        print("[DEBUG] process_timeline: raw data dari fetch_timeline bernilai None atau kosong!")
        return False
        
    posts = extract_posts(raw)
    if not posts:
        print("[DEBUG] process_timeline: Tidak ada postingan yang berhasil diekstrak!")
        return False

    # Filter: hanya IT/AI, skip lowongan kerja, max N jam lalu, skip blacklist
    cfg = config_loader.load()
    MAX_AGE_SECONDS = float(cfg["max_post_age_hours"]) * 3600
    blacklist = [u.lower().strip() for u in cfg.get("blacklisted_accounts", [])]
    now = time.time()
    filtered = []
    for p in posts:
        uname = (p.get("username") or "").lower()
        if uname in blacklist:
            print(f"[FILTER] @{p['username']} dilewati — akun di-blacklist")
            continue
        taken_at = p.get("taken_at") or 0
        age_seconds = now - taken_at if taken_at else MAX_AGE_SECONDS + 1
        age_hours = age_seconds / 3600
        if age_seconds > MAX_AGE_SECONDS:
            print(f"[FILTER] @{p['username']} dilewati — post terlalu lama ({age_hours:.1f} jam)")
            continue
        ok, reason = should_comment(p.get("caption", ""))
        if ok:
            filtered.append(p)
        else:
            print(f"[FILTER] @{p['username']} dilewati — {reason}")
    posts = filtered
    if not posts:
        print("[FILTER] Tidak ada post IT/AI ≤5 jam di timeline, coba lagi di siklus berikutnya.")
        return False

    app_mode = os.getenv("APP_MODE", "normal").lower()

    if app_mode == "trend":
        stats_tracker.increment("trend_posts_found")
        trend_candidates = [
            p for p in posts
            if (p.get("like_count") or 0) >= 30 and (p.get("reply_count") or 0) >= 3
        ]

        if trend_candidates:
            top = max(
                trend_candidates,
                key=lambda p: calculate_trend_score(p.get("like_count") or 0, p.get("reply_count") or 0),
            )
            stats_tracker.increment("trend_posts_selected")

            print(f"\n[TREND_SCORE] {calculate_trend_score(top.get('like_count') or 0, top.get('reply_count') or 0)}")
            print(f"[TREND_LIKE_COUNT] {top.get('like_count', 0)}")
            print(f"[TREND_REPLY_COUNT] {top.get('reply_count', 0)}")

            posts = [top]
        else:
            stats_tracker.increment("trend_fallbacks")
            print("[TREND_FALLBACK] Tidak ada postingan yang lolos kriteria, pakai urutan acak.")
            random.shuffle(posts)
    else:
        random.shuffle(posts)

    # Kumpulkan post_id yang sudah pernah dikomentari (24 jam terakhir)
    commented_ids = {e["post_id"] for e in reply_cache.get_recent_post_ids(max_age_hours=24)}

    for p in posts:
        username = p.get("username", "?")
        post_id  = p.get("post_id",  "?")
        caption  = p.get("caption",  "")

        # Skip kalau sudah pernah komentar di post ini, coba post berikutnya
        if str(post_id) in commented_ids:
            print(f"  [SKIP] Sudah pernah komentar di post {post_id}, coba post lain…")
            continue

        print(f"\n[INFO] Menemukan target: @{username}")

        # Baca komentar orang lain yang sudah ada di thread ini
        existing = fetch_post_replies(session, str(post_id))
        if existing:
            print(f"  [MEMORY] {len(existing)} komentar orang lain ditemukan di thread")

        reply_text = generate_reply(caption, existing_replies=existing)

        if reply_text == "SKIP":
            print("  >> AI memutuskan SKIP (tidak relevan / terdeteksi link / banned phrase).")
            time.sleep(15)
            continue

        print("--------------------------------------------------")
        print(f"  DRAF AI: {reply_text}")
        print("--------------------------------------------------")

        if AUTO_MODE:
            # 1. Like dulu sebelum komentar (lebih natural)
            if config_loader.get("like_before_comment"):
                jeda_like = random.randint(2, 6)
                print(f"  [LIKE] Like dulu… (jeda {jeda_like}d)")
                time.sleep(jeda_like)
                liked = like_post(session, tokens, str(post_id))
                print(f"  [LIKE] {'Berhasil 👍' if liked else 'Gagal, lanjut komentar'}")
                time.sleep(random.randint(1, 3))

            # 2. Kirim komentar
            our_comment_id = post_reply(session, str(post_id), reply_text)
            if our_comment_id is not None:
                print(f"  >> Status Pengiriman: Berhasil! (comment_id={our_comment_id})")
                stats_tracker.increment("comments_sent")
                reply_cache.save_to_cache(
                    caption, reply_text,
                    post_id=str(post_id),
                    comment_id=our_comment_id,
                )
                human_delay()
                break  # satu komentar per siklus — berhasil
            else:
                print("  >> Status Pengiriman: Gagal, coba post berikutnya…")
                stats_tracker.increment("comment_failures")
                time.sleep(15)
    else:
        # Semua post sudah dikomentari — jeda pendek agar tidak hammering API
        delay = random.randint(5 * 60, 10 * 60)
        print(f"\n[INFO] Semua post dalam timeline sudah dikomentari. Jeda {delay // 60} menit…")
        time.sleep(delay)
    return True

def is_active_hours() -> bool:
    cfg   = config_loader.load()
    h     = datetime.now().hour
    start = int(cfg.get("active_start_hour", 7))
    end   = int(cfg.get("active_end_hour",   23))
    return start <= h < end


def main():
    if not llm_router.is_available():
        print(f"[ERROR] Qoder CLI tidak ditemukan di '{llm_router.QODER_BIN}'.")
        print("        Pastikan sudah install & login Qoder, atau set QODER_BIN di .env")
        sys.exit(1)
    print(f"[INFO] Backend LLM: Qoder CLI ({llm_router.QODER_BIN})")

    sm = SessionManager()
    if not sm.load_session() or sm.validate_session().status != SessionStatus.VALID:
        print("[ERROR] Session invalid. Cek THREADS_SESSION_ID dll di .env")
        sys.exit(1)

    session    = sm.inject_session()
    fail_streak = 0   # session-health counter

    while True:
        try:
            cfg = config_loader.load()
            start = int(cfg.get("active_start_hour", 7))
            end   = int(cfg.get("active_end_hour",   23))

            if not is_active_hours():
                print(f"[SCHEDULE] Di luar jam aktif ({start:02d}:00–{end:02d}:00), tidur 30 menit…")
                time.sleep(1800)
                continue

            ok = process_timeline(session)
            if ok:
                fail_streak = 0
            else:
                fail_streak += 1
                if fail_streak >= 5:
                    print(f"[HEALTH] {fail_streak}× gagal berturut-turut — session mungkin expired. Tidur 30 menit.")
                    stats_tracker.log_error("Possible session expired (5x consecutive fetch failures)")
                    time.sleep(1800)
                    fail_streak = 0
                else:
                    time.sleep(60)

        except Exception as e:
            stats_tracker.log_error(str(e))
            logger.error("Error: %s", e)
            time.sleep(60)

if __name__ == "__main__":
    main()
