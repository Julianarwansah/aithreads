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
from services.session_manager import SessionManager, SessionStatus

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

ANTI_AI_PROMPT = ""

_comment_history = []

THREADS_HOME       = "https://www.threads.com/"
GRAPHQL_URL        = "https://www.threads.com/graphql/query"
LIKE_DOC_ID        = "24753372994365040"
LIKE_FRIENDLY_NAME = "useTHLikeMutationLikeMutation"
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

def generate_reply(caption: str) -> str:
    global _comment_history

    recent_comments = "\n".join([f"- {c}" for c in _comment_history]) if _comment_history else "(Belum ada komentar sebelumnya)"

    base_prompt = agent_loader.load_agent('threads-natural-commenter-agent')

    if base_prompt:
        sys_prompt = base_prompt.replace("{recent_comments}", recent_comments)
        user_prompt = f"{caption}"
    else:
        sys_prompt = f"""lo orang IT Indonesia yang sering buka Threads. kalau nemu postingan yang relate, lo nimbrung — tapi lo BUKAN asisten, lo bukan guru, lo cuma orang yang lagi scroll sambil rebahan.

TIPE KOMENTAR (pilih yang paling pas, bukan selalu kasih saran):

1. NANYA — lo penasaran, mau lanjutin obrolan
   contoh: "ini udah solved blm?"
   contoh: "lo pake apa akhirnya?"
   contoh: "framework-nya apa emg?"
   contoh: "berapa lama sampe kelar?"
   contoh: "ini production apa dev?"

2. REACT / NGOBROL — lo cuma relate, ga perlu kasih apa-apa
   contoh: "ini gue banget kemarin"
   contoh: "serius? gue kira cuma gue doang"
   contoh: "nah ini dia"
   contoh: "anjir sama persis"
   contoh: "haha duh familiar banget"
   contoh: "ini kenapa relate parah sih"

3. SHARE SINGKAT — lo pernah ngalamin, share dikit aja
   contoh: "gue kemarin 3 hari stuck di sini"
   contoh: "blm kelar juga dari minggu lalu ini"
   contoh: "sama, akhirnya gue tinggalin aja dulu"

4. SARAN (JARANG — hanya kalau beneran natural, bukan default)
   contoh: "coba cek log-nya dulu sih"
   contoh: "pernah coba pakai x dulu?"

POLA NULIS:
- 1 kalimat pendek. kadang cuma 4–6 kata aja udah cukup
- bahasa santai: "gue", "lo", "sih", "aja", "emg", "blm", "udh", "tp", "jg", "yg"
- boleh typo kecil biar keliatan manusia beneran
- JANGAN mulai dengan "Wah", "Haha", "Hehe", "Oh", "Ya", "Iya", "Relate banget"
- JANGAN kasih saran kalau ga diminta — itu bikin keliatan bot/AI
- DILARANG: link, nama produk spesifik, promosi
- kalau postingan duka/politik panas/NSFW: balas SKIP

CONTOH JELEK (keliatan AI — jangan begini):
"Relate banget nih! Emang susah sih, coba mulai dari yang simpel dulu biar ga overwhelming."
"Wah keren banget progress lo! Semangat terus ya, pasti bisa!"
"Mungkin bisa dicoba pakai Docker aja biar lebih konsisten environment-nya."

komentar terakhir lo (jangan ulangi pola/nada yang sama):
{recent_comments}"""
        user_prompt = caption

    try:
        cached_reply = reply_cache.check_cache(caption)
        if cached_reply:
            parsed_cache = parse_ai_reply(cached_reply)
            if parsed_cache == "SKIP":
                return "SKIP"
            return parsed_cache

        reply = llm_router.generate(user_prompt, system_prompt=sys_prompt)
        
        print("\n========== ROUTER DEBUG ==========")
        print(f"[RAW LLM RESPONSE]: {reply}")
        print("==================================\n")

        reply = parse_ai_reply(reply)
        if reply == "SKIP":
            return "SKIP"

        print("\n========== FINAL DEBUG ==========")
        print(f"[FINAL REPLY]: {reply}")
        print(f"[FINAL LENGTH]: {len(reply)}")
        print("=================================\n")
        
        reply_cache.save_to_cache(caption, reply)
        _comment_history.append(reply)
        if len(_comment_history) > 3:
            _comment_history.pop(0)
            
        return reply
    except Exception as exc:
        # Jangan pernah mengembalikan teks error: process_timeline hanya menyaring
        # "SKIP", jadi string lain akan benar-benar ter-post ke Threads.
        stats_tracker.log_error(f"LLM Router Error: {exc}")
        stats_tracker.increment("llm_errors")
        logger.error("Gagal membuat balasan: %s", exc)
        return "SKIP"

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


def post_reply(session, post_id: str, text: str) -> bool:
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

    data = urlencode({
        "caption": text,
        "barcelona_source_reply_id": post_id,
        "publish_mode": "text_post",
        "upload_id": str(int(time.time() * 1000)),
        "text_post_app_info": json.dumps({"reply_id": post_id, "entry_point": "create_reply", "text_with_entities": {"entities": [], "text": text}}),
    })
    
    try:
        resp = session.post(REPLY_ENDPOINT, data=data, headers=headers, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        print(f"[REPLY_ERROR] {e}")
        return False

def human_delay():
    cfg   = config_loader.load()
    lo    = int(cfg["delay_min_minutes"]) * 60
    hi    = int(cfg["delay_max_minutes"]) * 60
    delay = random.randint(lo, max(lo, hi))
    print(f"\n[INFO] Bot beristirahat {int(delay/60)} menit agar natural...")
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

_LOKER_KEYWORDS = [
    "lowongan", "loker", "hiring", "we are hiring", "open rekrutmen",
    "open recruitment", "open position", "open vacancy", "dicari karyawan",
    "dibutuhkan segera", "bergabung bersama kami", "kirim cv", "kirim lamaran",
    "apply now", "daftarkan diri", "job desc", "job description",
    "kualifikasi:", "persyaratan:", "benefit:", "🚨 hiring", "📢 hiring",
    "walk in interview", "walk-in interview",
]

_IT_AI_KEYWORDS = [
    # Umum coding & dev
    "ngoding", "coding", "programmer", "developer", "programming",
    "bug", "debug", "error", "deploy", "repository", "repo", "commit",
    "pull request", "merge", "refactor", "framework", "library",
    # Bahasa & stack
    "python", "javascript", "typescript", "php", "java", "golang", "rust",
    "c++", "kotlin", "swift", "ruby", "dart", "flutter", "react", "nextjs",
    "vue", "angular", "laravel", "django", "fastapi", "nodejs", "node.js",
    "sql", "nosql", "mongodb", "postgresql", "mysql", "redis",
    # Infra & tools
    "linux", "ubuntu", "docker", "kubernetes", "k8s", "ci/cd", "devops",
    "cloud", "aws", "gcp", "azure", "server", "api", "microservice",
    "vps", "nginx", "apache", "git", "github", "gitlab", "terminal",
    "xampp", "laragon", "wsl",
    # Perangkat & IT umum
    "laptop", "pc build", "komputer", "hardware", "spek", "ram", "gpu",
    "network", "jaringan", "vpn", "firewall", "cybersecurity", "hacking",
    "pentesting", "vulnerability",
    # AI & ML
    " ai ", "artificial intelligence", "machine learning", " ml ",
    "deep learning", "neural network", "llm", "large language model",
    "chatgpt", "gpt-", "claude", "gemini", "copilot", "qoder",
    "midjourney", "stable diffusion", "generative ai", "prompt engineer",
    "dataset", "fine-tuning", "inference", "huggingface", "langchain",
    "rag ", "vector db", "embedding",
    # Startup & tech
    "startup", "saas", "software engineer", "tech stack", "mvp",
    "product manager", "ui/ux", "figma", "wireframe",
]


def should_comment(caption: str) -> tuple[bool, str]:
    """Kembalikan (True, alasan) jika layak dikomentari, (False, alasan) jika tidak."""
    low  = caption.lower()
    cfg  = config_loader.load()

    loker_kws  = _LOKER_KEYWORDS  + [k.lower() for k in cfg.get("loker_keywords_extra",  [])]
    it_ai_kws  = _IT_AI_KEYWORDS  + [k.lower() for k in cfg.get("it_ai_keywords_extra", [])]

    for kw in loker_kws:
        if kw in low:
            return False, f"lowongan kerja ({kw!r})"

    for kw in it_ai_kws:
        if kw in low:
            return True, f"topik IT/AI ({kw!r})"

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

    # Filter: hanya IT/AI, skip lowongan kerja, max N jam lalu
    cfg = config_loader.load()
    MAX_AGE_SECONDS = float(cfg["max_post_age_hours"]) * 3600
    now = time.time()
    filtered = []
    for p in posts:
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

    for p in posts[:1]:
        username = p.get("username", "?")
        post_id  = p.get("post_id",  "?")
        caption  = p.get("caption",  "")

        print(f"\n[INFO] Menemukan target: @{username}")

        reply_text = generate_reply(caption)

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
            success = post_reply(session, str(post_id), reply_text)
            if success:
                print("  >> Status Pengiriman: Berhasil!")
                stats_tracker.increment("comments_sent")
                reply_cache.save_to_cache(caption, reply_text, post_id=str(post_id))
            else:
                print("  >> Status Pengiriman: Gagal.")
                stats_tracker.increment("comment_failures")

        human_delay()
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
