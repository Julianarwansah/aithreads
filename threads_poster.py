"""
threads_poster.py -- Auto Poster konten IT/AI original.
Alur: scan timeline → lihat tren IT/AI → buat postingan original yang relate → post.
"""
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

from dotenv import load_dotenv
load_dotenv()

import config_loader
import llm_router
import reply_cache
import stats_tracker
from auto_commenter import _heuristic_score, _score_naturalness
from services.session_manager import SessionManager, SessionStatus

# Import shared timeline utilities dari auto_commenter
# (tidak ada circular import: auto_commenter tidak import threads_poster)
from auto_commenter import (
    load_latest_query_config,
    get_fresh_tokens,
    fetch_timeline,
    extract_posts,
    should_comment,
)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_DIR              = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR         = os.getenv("PROFILE_DIR", _DIR)
_LAST_POST_FILE   = os.path.join(_DATA_DIR, "poster_last_post.json")
_DAILY_COUNT_FILE = os.path.join(_DATA_DIR, "poster_daily_count.json")

# Interval cek ulang saat menunggu jam ramai / kuota. Tidak boleh 0 (busy-loop) dan
# tidak boleh berjam-jam supaya jam ramai berikutnya tidak kelewat.
_SCHEDULE_CHECK_SECONDS = 900    # 15 menit
_QUOTA_RECHECK_SECONDS  = 1800   # 30 menit — dipakai saat kuota harian sudah habis

POST_ENDPOINT = "https://www.threads.com/api/v1/media/configure_text_only_post/"

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
}

_post_history: list[str] = []

DEFAULT_TOPICS = [
    "ngoding dan debugging",
    "AI dan tools terbaru",
    "developer life",
    "tech opinion",
    "startup dan produk digital",
]


# ── scanner ───────────────────────────────────────────────────────────────────
def scan_trending_it_ai(session) -> list[dict]:
    """Ambil post IT/AI yang lagi ramai di timeline (6 jam terakhir)."""
    print("[SCAN] Memindai timeline untuk konten IT/AI yang sedang tren…")
    try:
        query_config = load_latest_query_config()
        tokens       = get_fresh_tokens(session)
        raw          = fetch_timeline(session, tokens, query_config)
    except Exception as e:
        print(f"[SCAN] Gagal fetch timeline: {e}")
        return []

    if not raw:
        print("[SCAN] Timeline kosong atau tidak bisa diambil.")
        return []

    posts = extract_posts(raw)
    now   = time.time()
    result = []

    for p in posts:
        caption  = (p.get("caption") or "").strip()
        taken_at = p.get("taken_at") or 0
        age_hr   = (now - taken_at) / 3600 if taken_at else 99
        if age_hr > 6:
            continue
        if not caption:
            continue
        ok, _ = should_comment(caption)
        if not ok:
            continue
        likes    = p.get("like_count")  or 0
        replies  = p.get("reply_count") or 0
        # reply lebih berat: artinya orang mau bahas
        engagement = likes + replies * 3
        result.append({
            "username":   p.get("username", "?"),
            "caption":    caption,
            "likes":      likes,
            "replies":    replies,
            "engagement": engagement,
        })

    result.sort(key=lambda x: x["engagement"], reverse=True)
    top = result[:6]

    if top:
        print(f"[SCAN] {len(top)} postingan IT/AI tren ditemukan:")
        for i, p in enumerate(top, 1):
            print(f"  {i}. @{p['username']} ({p['likes']}👍 {p['replies']}💬): {p['caption'][:70]}…")
    else:
        print("[SCAN] Tidak ada tren IT/AI terdeteksi — akan pakai topik default.")

    return top


def _build_trend_summary(posts: list[dict]) -> str:
    """Ringkasan teks dari trend posts untuk dimasukkan ke prompt."""
    if not posts:
        return "(tidak ada data tren — buat postingan dari pengalaman sendiri)"
    lines = []
    for p in posts:
        snippet = p["caption"][:120].replace("\n", " ")
        if len(p["caption"]) > 120:
            snippet += "…"
        lines.append(f'- "{snippet}" ({p["likes"]}👍 {p["replies"]}💬)')
    return "\n".join(lines)


# ── generator ─────────────────────────────────────────────────────────────────
def generate_thread_content(session) -> str:
    global _post_history
    cfg    = config_loader.load()
    topics = cfg.get("poster_topics") or DEFAULT_TOPICS

    # 1. Scan tren dulu
    trend_posts   = scan_trending_it_ai(session)
    trend_summary = _build_trend_summary(trend_posts)
    # Simpan hasil scan ke cache agar UI bisa baca
    try:
        from pathlib import Path
        tc = os.path.join(_DATA_DIR, "trending_cache.json")
        Path(tc).write_text(
            json.dumps({"ts": time.time(), "posts": trend_posts[:6]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    # 2. Pilih topik referensi: dari tren kalau ada, atau random dari list
    if trend_posts:
        # Ambil kata kunci kasar dari caption postingan paling ramai
        top_caption = trend_posts[0]["caption"]
        topic_hint  = top_caption[:80]
    else:
        topic_hint = random.choice(topics)

    recent = "\n".join(f"- {p}" for p in _post_history) if _post_history else "(belum ada postingan sebelumnya)"

    sys_prompt = f"""lo adalah orang IT Indonesia yang aktif banget di Threads.
lo baru buka timeline dan ngeliat beberapa postingan IT/AI yang lagi rame dibahas orang.
sekarang lo mau nimbrung — bukan reply ke orang, tapi nulis postingan sendiri yang relate ke tema itu.

yang lagi rame di timeline sekarang:
{trend_summary}

cara lo nulis di Threads:
- SANGAT pendek, 1–3 kalimat max. kebanyakan orang cuma baca 2 detik
- pakai bahasa lu gue sehari-hari: "gue", "lo", "sih", "aja", "bgt", "emg", "ngl", "tbh", "yg", "tp"
- boleh potong kata kayak orang ngetik beneran: "blm", "udh", "lg", "jg", "klo"
- boleh typo kecil biar keliatan manusia
- tone bisa: relate/frustrasi/opini singkat/iseng/fakta kecil yang bikin orang mikir
- DILARANG: promosi, link, sebut nama orang/username, copas caption orang lain
- DILARANG: kata-kata lebay ("luar biasa", "sangat inspiring", "keren banget sih")
- JANGAN mulai dengan "Gue mau cerita…" atau "Jadi begini…" — langsung ke intinya

contoh JELEK (terlalu AI / terstruktur):
"Sebagai developer Indonesia, gue sangat merasakan betapa pentingnya mengikuti perkembangan AI. Tools seperti ChatGPT dan Claude benar-benar mengubah cara kerja kita!"

contoh BAGUS (natural, pendek, relate):
"ngl gue udh ga bisa ngoding tanpa AI assist sekarang, otak gue males mikir sendiri"
"tiap update library pasti ada aja yang break, kenapa sih"
"deploy hari jumat sore adalah kejahatan"
"AI makin pinter eh developer makin mager, kira-kira nanti kita masih dipake ga"
"somehow debug 3 jam lebih nyaman daripada bikin dokumentasi 10 menit"

postingan lo sebelumnya (JANGAN ulangi pola/tema yang sama):
{recent}

tema yang relevan sekarang: {topic_hint}"""

    user_prompt = "Tulis satu postingan Threads pendek yang relate ke tema yang lagi rame:"

    def _gen_best_post() -> str:
        two_prompt = (
            "Tulis 2 versi postingan berbeda (1–3 kalimat masing-masing), "
            f"pisahkan dengan ---\n\n{user_prompt}"
        )
        r = llm_router.generate(two_prompt, system_prompt=sys_prompt)
        print(f"\n[POSTER RAW LLM]: {r}")
        parts = [p.strip() for p in r.split("---") if p.strip()]
        if len(parts) >= 2:
            candidates = [_clean_post(p) for p in parts[:2]]
            valid = [(t, _heuristic_score(t)) for t in candidates if t]
            if valid:
                best, sc = max(valid, key=lambda x: x[1])
                print(f"  [POSTER DRAFT] {len(valid)} kandidat, pilih skor {sc:.1f}")
                return best
        return _clean_post(r)

    try:
        content = _gen_best_post()

        # Self-critique gate
        if content:
            score = _score_naturalness(content, topic_hint)
            print(f"  [POSTER CRITIQUE] Skor naturalness: {score}/10")
            if score < 7:
                print(f"  [POSTER CRITIQUE] Skor rendah — generate ulang…")
                retry = _gen_best_post()
                if retry:
                    retry_score = _score_naturalness(retry, topic_hint)
                    if retry_score > score:
                        content = retry

        if content:
            _post_history.append(content)
            if len(_post_history) > 5:
                _post_history.pop(0)
        return content
    except Exception as e:
        stats_tracker.log_error(f"Poster LLM error: {e}")
        stats_tracker.increment("llm_errors")
        return ""


def _clean_post(text: str) -> str:
    """Bersihkan output Qoder dari format markdown, kutip, atau label."""
    if not text:
        return ""
    # Hapus bold/italic markdown
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # Hapus baris label ("Postingan:", "Draft:", dst)
    lines = []
    skip_labels = re.compile(r'^(postingan|draft|output|hasil|komentar|teks)[:\s]', re.IGNORECASE)
    for ln in text.splitlines():
        ln = ln.lstrip('> ').strip()
        if skip_labels.match(ln):
            # Ambil teks setelah titik dua kalau ada
            parts = ln.split(':', 1)
            if len(parts) == 2 and parts[1].strip():
                lines.append(parts[1].strip())
        elif ln:
            lines.append(ln)
    result = ' '.join(lines).strip()
    # Potong di 400 karakter
    if len(result) > 400:
        result = result[:397] + "…"
    return result


# ── poster ────────────────────────────────────────────────────────────────────
def post_thread(session, text: str) -> bool:
    csrf = os.getenv("THREADS_CSRF_TOKEN") or session.cookies.get("csrftoken") or ""
    hdrs = BASE_HEADERS.copy()
    hdrs["x-csrftoken"] = csrf
    data = urlencode({
        "caption":            text,
        "publish_mode":       "text_post",
        "upload_id":          str(int(time.time() * 1000)),
        "text_post_app_info": json.dumps({
            "text_with_entities": {"entities": [], "text": text}
        }),
    })
    try:
        resp = session.post(POST_ENDPOINT, data=data, headers=hdrs, timeout=30)
        return resp.status_code == 200
    except Exception as exc:
        logger.error("post_thread error: %s", exc)
        return False


def _read_last_post_time() -> float:
    """Baca timestamp postingan terakhir dari file (0 kalau belum pernah)."""
    try:
        from pathlib import Path
        if Path(_LAST_POST_FILE).exists():
            data = json.loads(Path(_LAST_POST_FILE).read_text(encoding="utf-8"))
            return float(data.get("ts", 0))
    except Exception:
        pass
    return 0.0


def _write_last_post_time():
    try:
        from pathlib import Path
        Path(_LAST_POST_FILE).write_text(
            json.dumps({"ts": time.time()}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        # Gagal tulis tidak boleh membatalkan siklus: postingan sudah terkirim
        logger.warning("Gagal menyimpan waktu post terakhir: %s", e)


# ── jadwal & kuota harian ─────────────────────────────────────────────────────
def _cfg_number(cfg: dict, key: str, default: float) -> float:
    """Baca angka dari config tanpa melempar exception (nilai rusak → default)."""
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        logger.warning("Config '%s' bukan angka — pakai default %s", key, default)
        return float(default)


def _today_str(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _read_daily_count(now: datetime | None = None) -> int:
    """
    Jumlah postingan pada hari kalender berjalan.

    Reset harian terjadi di sini: kalau "date" di file bukan hari ini (atau filenya
    hilang/rusak), dihitung 0 — jadi file baru ditulis ulang saat post sukses berikutnya.
    """
    try:
        from pathlib import Path
        p = Path(_DAILY_COUNT_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if str(data.get("date", "")) == _today_str(now):
                return max(0, int(data.get("count", 0)))
    except Exception as e:
        logger.warning("Gagal baca kuota harian (%s) — dianggap 0.", e)
    return 0


def _write_daily_count(count: int, now: datetime | None = None) -> bool:
    try:
        from pathlib import Path
        Path(_DAILY_COUNT_FILE).write_text(
            json.dumps({"date": _today_str(now), "count": max(0, int(count))}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.warning("Gagal simpan kuota harian: %s", e)
        return False


def _increment_daily_count(now: datetime | None = None) -> int:
    """Naikkan counter kuota harian. Hanya dipanggil setelah posting benar-benar sukses."""
    used = _read_daily_count(now) + 1
    _write_daily_count(used, now)
    return used


def get_peak_hours_safe() -> set:
    """Jam ramai dari data balasan 7 hari terakhir; set kosong kalau belum ada data/gagal."""
    try:
        return {int(h) for h in reply_cache.get_peak_hours(top_n=5)}
    except Exception as e:
        logger.warning("Gagal baca jam ramai: %s", e)
        return set()


def _format_hours(hours) -> str:
    return ", ".join(f"{int(h):02d}:00" for h in sorted(hours))


def _next_peak_hour(peaks: set, now: datetime) -> int | None:
    """Jam ramai terdekat sesudah sekarang; kalau sudah lewat semua → jam pertama besok."""
    if not peaks:
        return None
    ahead = sorted(h for h in peaks if h > now.hour)
    return ahead[0] if ahead else min(peaks)


def _seconds_until_midnight(now: datetime | None = None) -> float:
    now      = now or datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (midnight - now).total_seconds())


def is_active_hours(now: datetime | None = None) -> bool:
    cfg   = config_loader.load()
    h     = (now or datetime.now()).hour
    start = int(_cfg_number(cfg, "active_start_hour", 7))
    end   = int(_cfg_number(cfg, "active_end_hour",   23))
    return start <= h < end


def is_post_window(now: datetime | None = None) -> tuple[bool, str]:
    """
    Boleh posting sekarang atau tidak, plus alasan siap-log.

    Gate: jam aktif selalu dihormati, lalu jam ramai kalau poster_peak_only aktif.
    Kalau data jam ramai belum ada (peak set kosong), fallback ke jam aktif supaya
    poster tidak macet selamanya menunggu data yang belum pernah terkumpul.
    """
    cfg = config_loader.load()
    now = now or datetime.now()
    h   = now.hour

    if not is_active_hours(now):
        start = int(_cfg_number(cfg, "active_start_hour", 7))
        end   = int(_cfg_number(cfg, "active_end_hour",   23))
        return False, f"Di luar jam aktif ({start:02d}:00–{end:02d}:00)"

    if not bool(cfg.get("poster_peak_only", True)):
        return True, f"Jam aktif {h:02d}:xx (gate jam ramai nonaktif)"

    peaks = get_peak_hours_safe()
    if not peaks:
        return True, (f"Jam aktif {h:02d}:xx — data jam ramai masih kosong, "
                      "fallback ke jam aktif (active_start_hour/active_end_hour)")
    if h in peaks:
        return True, f"Jam ramai {h:02d}:xx"

    nxt     = _next_peak_hour(peaks, now)
    nxt_txt = f"{nxt:02d}:00" if nxt is not None else "besok"
    return False, (f"Sekarang {h:02d}:xx bukan jam ramai — menunggu {nxt_txt} "
                   f"(jam ramai: {_format_hours(peaks)})")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    if not llm_router.is_available():
        print(f"[ERROR] Qoder CLI tidak ditemukan.")
        sys.exit(1)
    print(f"[INFO] Poster aktif — backend: Qoder CLI")
    print("[INFO] Mengecek koneksi Qoder CLI…")
    if not llm_router.ping():
        print("[ERROR] Qoder CLI tidak merespons saat startup. Cek login & koneksi internet.")
        sys.exit(1)
    print("[INFO] Qoder CLI OK.")
    print(f"[INFO] Alur: scan tren → generate post natural → kirim")

    cfg0    = config_loader.load()
    hours0  = _cfg_number(cfg0, "poster_interval_hours", 48)
    quota0  = int(_cfg_number(cfg0, "poster_daily_quota", 5))
    peak0   = bool(cfg0.get("poster_peak_only", True))
    gap0    = int(_cfg_number(cfg0, "poster_min_gap_minutes", 120))
    print(f"[INFO] Jadwal: {'interval nonaktif' if hours0 <= 0 else f'interval {hours0:.0f} jam'} | "
          f"kuota harian {quota0 if quota0 > 0 else 'tanpa batas'} | "
          f"jarak minimum {gap0 if gap0 > 0 else 'nonaktif'} menit | "
          f"{'hanya jam ramai' if peak0 else 'jam aktif saja'}")

    sm = SessionManager()
    if not sm.load_session() or sm.validate_session().status != SessionStatus.VALID:
        print("[ERROR] Session invalid.")
        sys.exit(1)

    session = sm.inject_session()

    while True:
        try:
            cfg   = config_loader.load()
            hours = _cfg_number(cfg, "poster_interval_hours", 48)
            quota = int(_cfg_number(cfg, "poster_daily_quota", 5))

            # Interval 0 (atau negatif) = nonaktif: seluruh cek elapsed_hr dilewati,
            # cadence diserahkan ke kuota harian + jam ramai di bawah.
            if hours > 0:
                # Cek interval sejak postingan terakhir — tahan double-post saat restart
                last_ts    = _read_last_post_time()
                elapsed_hr = (time.time() - last_ts) / 3600
                if last_ts > 0 and elapsed_hr < hours:
                    remaining_hr = hours - elapsed_hr
                    print(f"[INFO] Belum saatnya posting (sisa {remaining_hr:.1f} jam dari interval {hours:.0f} jam).")
                    time.sleep(min(3600, max(60, int(remaining_hr * 3600))))
                    continue

            # Gate jam ramai (fallback ke jam aktif kalau datanya belum ada)
            allowed, reason = is_post_window()
            if not allowed:
                print(f"[SCHEDULE] {reason} — cek lagi dalam {_SCHEDULE_CHECK_SECONDS // 60} menit…")
                time.sleep(_SCHEDULE_CHECK_SECONDS)
                continue

            # Gate kuota harian (0 = tanpa batas)
            posted_today = _read_daily_count()
            if quota > 0 and posted_today >= quota:
                wait = min(_seconds_until_midnight(), _QUOTA_RECHECK_SECONDS)
                sisa = max(0, quota - posted_today)
                print(f"[QUOTA] Kuota harian habis ({posted_today}/{quota}, sisa {sisa}) hari "
                      f"{_today_str()} — posting lanjut setelah tengah malam, cek lagi ±{wait / 60:.0f} menit.")
                time.sleep(wait)
                continue

            # Gate jarak minimum antar-post (0 = nonaktif). Sengaja di luar blok
            # `hours > 0` supaya tetap berlaku saat interval dimatikan — inilah rem
            # yang mencegah beberapa postingan keluar berdekatan dalam satu jam ramai.
            min_gap = int(_cfg_number(cfg, "poster_min_gap_minutes", 120))
            if min_gap > 0:
                gap_ts = _read_last_post_time()
                elapsed_min = (time.time() - gap_ts) / 60
                if gap_ts > 0 and elapsed_min < min_gap:
                    wait = min(_SCHEDULE_CHECK_SECONDS, max(60, int((min_gap - elapsed_min) * 60)))
                    print(f"[GAP] Jarak minimum {min_gap} menit belum terpenuhi "
                          f"(baru {elapsed_min:.0f} menit sejak post terakhir) — cek lagi ±{wait // 60} menit.")
                    time.sleep(wait)
                    continue

            print("\n" + "="*50)
            quota_txt = f"{posted_today}/{quota}" if quota > 0 else f"{posted_today} (tanpa batas)"
            print(f"[INFO] Memulai siklus posting… ({reason} | kuota hari ini {quota_txt})")

            # Scan + generate
            content = generate_thread_content(session)

            if not content:
                print("[INFO] Tidak ada konten yang bisa dibuat, coba lagi dalam 1 jam.")
                time.sleep(3600)
                continue

            print(f"\n  DRAF POST: {content}")
            print(f"  Panjang: {len(content)} karakter")

            ok = post_thread(session, content)
            if ok:
                print("  >> Berhasil memposting!")
                stats_tracker.increment("posts_created")
                _write_last_post_time()
                # Counter kuota hanya naik kalau posting benar-benar sukses
                used = _increment_daily_count()
                if quota > 0:
                    print(f"  [QUOTA] {used}/{quota} postingan hari ini.")
            else:
                print("  >> Gagal posting.")
                stats_tracker.increment("post_failures")

            # Tidur akhir siklus: interval lama kalau masih dipakai, kalau tidak
            # cukup interval cek ulang pendek agar jam ramai berikutnya tidak kelewat.
            if hours > 0:
                nap = max(60, int(hours * 3600))
                print(f"\n[INFO] Poster tidur {nap / 3600:.1f} jam hingga postingan berikutnya…")
            else:
                nap = _SCHEDULE_CHECK_SECONDS
                print(f"\n[INFO] Interval nonaktif — cek jam ramai & kuota lagi dalam {nap // 60} menit…")
            time.sleep(nap)

        except Exception as e:
            stats_tracker.log_error(str(e))
            logger.error("Poster error: %s", e)
            time.sleep(3600)


if __name__ == "__main__":
    main()
