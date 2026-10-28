"""
comment_memory.py -- Persistent memory komentar bot.

Menyimpan histori komentar kita sendiri + mendeteksi pola
agar AI terus berkembang dan tidak repetitif.
"""
import os
import re
import threading
import time

import safe_store

_DIR        = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.getenv("PROFILE_DIR", _DIR)
MEMORY_FILE = os.path.join(_DATA_DIR, "comment_memory.json")
MAX_ENTRIES = 100
# Lock in-proses untuk jalur baca. Jalur tulis memakai safe_store.file_lock() yang
# berlaku antar-proses; lock ini tetap dipertahankan karena banyak fungsi analisis
# di bawah membaca lewat _load() dan sebelumnya mengandalkan _lock.
_lock       = threading.Lock()

# Draf yang DIBUANG quality gate disimpan terpisah, bukan di MEMORY_FILE. Memory
# utama hanya berisi komentar yang benar-benar terkirim — kalau draf gagal ikut
# disimpan di sana, ia akan mengotori engagement stats, dedup, dan daftar "komentar
# terakhir" yang dipakai prompt.
REJECTED_FILE = os.path.join(_DATA_DIR, "rejected_comments.json")
MAX_REJECTED  = 50

# Entri lama (ditulis sebelum rubrik kualitas ada) tidak punya field natural/clarity/
# politeness sama sekali, jadi semua pembacaan wajib lewat .get() dan entri tanpa skor
# diperlakukan netral — bukan dianggap buruk.
MIN_SCORE_FALLBACK = 6      # sama dengan DEFAULTS config_loader.quality_min_score

# Jumlah sampel minimum sebelum sebuah topic_bucket boleh dipakai mengambil keputusan.
# Sama dengan ambang "total >= 3" yang sudah dipakai get_engagement_stats() untuk
# best_style/best_sentiment: data engagement di sini tipis, jadi kesimpulan dari 1-2
# titik data jauh lebih sering salah daripada benar.
MIN_TOPIC_SAMPLES = 3


def _cfg_int(key: str, default: int) -> int:
    """Baca config int. Import di dalam fungsi + try/except: module ini dipakai banyak
    proses, jangan sampai crash cuma karena config_loader bermasalah."""
    try:
        import config_loader
        val = config_loader.get(key)
        return int(val) if val is not None else default
    except Exception:
        return default


def _as_score(val):
    """Paksa nilai jadi int skala 1-10; None kalau tidak bisa dipakai."""
    try:
        num = int(val)
    except (TypeError, ValueError):
        return None
    return max(1, min(10, num))


def _entry_scores(entry: dict) -> list:
    """Ambil skor kualitas yang benar-benar ada di sebuah entri (entri lama → kosong)."""
    out = []
    for key in ("natural", "clarity", "politeness"):
        val = entry.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out.append(int(val))
    return out


# ── style detection ───────────────────────────────────────────────────────────
def _detect_style(text: str) -> str:
    t = text.lower().strip()
    if t.endswith("?") or re.search(r'\b(gimana|kenapa|berapa|apa|siapa|kapan|emg beneran|udah solved|pake apa|akhirnya)\b', t):
        return "nanya"
    if re.match(r'^(anjir|wah|eh |nah|serius|beneran|haha|lol|duh|gilak|parah|omg)', t):
        return "react"
    if re.search(r'\b(coba|pake|mending|gunain|cobain|harusnya|bisa|cek|install)\b', t):
        return "saran"
    return "share"


# ── topic bucket detection ────────────────────────────────────────────────────
_TOPIC_BUCKETS = {
    # \bdebug\b tidak pernah cocok dengan "debugging" (word boundary menolak huruf
    # sesudahnya), jadi varian morfologi dipakai lewat \w*. Tanpa ini sebagian besar
    # postingan/komentar jatuh ke "misc" dan tidak pernah dapat sinyal engagement.
    "debugging":  r'\b(error|bug\w*|debug\w*|stuck|crash\w*|exception|traceback|fix\w*|solve\w*)\b',
    "deployment": r'\b(deploy\w*|ci/cd|docker|kubernetes|server|production|staging|pipeline)\b',
    "ai_tools":   r'\b(ai|llm|chatgpt|gpt|claude|copilot|ml|model|prompt|dataset)\b',
    "learning":   r'\b(belajar|tutorial|course|bootcamp|pemula|newbie|ngerti|paham)\b',
    "career":     r'\b(kerja\w*|pekerjaan|karir|salary|gaji|interview|job|freelance|remote|hiring)\b',
    "tools":      r'\b(vscode|vim|git|github|linux|terminal|framework|library|stack|tools?)\b',
    "opinion":    r'\b(menurut\w*|opini|opinion|pendapat|tbh|ngl|sebenarnya|kayaknya|gue rasa|hot take|unpopular)\b',
}

def _detect_topic_bucket(caption: str) -> str:
    low = caption.lower()
    for bucket, pattern in _TOPIC_BUCKETS.items():
        if re.search(pattern, low):
            return bucket
    return "misc"


# ── storage ───────────────────────────────────────────────────────────────────
def _load() -> list:
    data = safe_store.read_json(MEMORY_FILE, [])
    return data if isinstance(data, list) else []


def _save(data: list):
    safe_store.write_json(MEMORY_FILE, data)


def save(caption: str, reply: str, post_sentiment: str = "info",
         natural: int = None, clarity: int = None, politeness: int = None):
    """
    Simpan komentar baru ke memory.

    natural/clarity/politeness: skor kualitas 1-10 dari auto_commenter._score_quality.
    Semua opsional supaya pemanggil lama (mis. linkedin_commenter) tidak berubah —
    kalau tidak diberikan, field skor tidak ditulis sama sekali, persis entri lama.
    Flag unclear/impolite diturunkan dari skor terhadap ambang quality_min_score.
    """
    min_score = _cfg_int("quality_min_score", MIN_SCORE_FALLBACK)

    entry = {
        "ts":             time.time(),
        "caption":        caption[:120],
        "reply":          reply,
        "style":          _detect_style(reply),
        "topic_bucket":   _detect_topic_bucket(caption),
        "post_sentiment": post_sentiment,
    }

    nat = _as_score(natural)
    cla = _as_score(clarity)
    pol = _as_score(politeness)
    if nat is not None:
        entry["natural"] = nat
    if cla is not None:
        entry["clarity"] = cla
        entry["unclear"] = cla < min_score
    if pol is not None:
        entry["politeness"] = pol
        entry["impolite"] = pol < min_score

    with safe_store.file_lock(MEMORY_FILE):
        data = _load()
        data.append(entry)
        if len(data) > MAX_ENTRIES:
            data = data[-MAX_ENTRIES:]
        _save(data)


def _load_rejected() -> list:
    data = safe_store.read_json(REJECTED_FILE, [])
    return data if isinstance(data, list) else []


def save_rejected(caption: str, reply: str, scores: dict = None):
    """
    Simpan draf yang DIBUANG quality gate supaya AI bisa belajar dari kesalahannya.

    Tanpa ini draf buruk lenyap begitu saja: gate memang berhasil menahannya, tapi
    tidak ada bahan untuk `get_negative_examples()` sehingga pola yang sama bisa
    muncul lagi selamanya.
    """
    if not (reply or "").strip():
        return
    scores = scores or {}
    min_score = _cfg_int("quality_min_score", MIN_SCORE_FALLBACK)
    nat = _as_score(scores.get("natural"))
    cla = _as_score(scores.get("clarity"))
    pol = _as_score(scores.get("politeness"))

    entry = {
        "ts":      time.time(),
        "caption": (caption or "")[:120],
        "reply":   reply,
        "style":   _detect_style(reply),
        "reason":  _negative_reason({
            "natural": nat, "clarity": cla, "politeness": pol,
            "unclear": cla is not None and cla < min_score,
            "impolite": pol is not None and pol < min_score,
        }, min_score),
    }
    if nat is not None:
        entry["natural"] = nat
    if cla is not None:
        entry["clarity"] = cla
        entry["unclear"] = cla < min_score
    if pol is not None:
        entry["politeness"] = pol
        entry["impolite"] = pol < min_score

    try:
        with safe_store.file_lock(REJECTED_FILE):
            data = _load_rejected()
            data.append(entry)
            if len(data) > MAX_REJECTED:
                data = data[-MAX_REJECTED:]
            safe_store.write_json(REJECTED_FILE, data)
    except Exception:
        pass


# ── analysis ──────────────────────────────────────────────────────────────────
def get_style_counts(last_n: int = 20) -> dict:
    """Hitung distribusi tipe komentar dari N terakhir."""
    with _lock:
        data = _load()
    counts = {"nanya": 0, "react": 0, "share": 0, "saran": 0}
    for e in data[-last_n:]:
        s = e.get("style", "share")
        counts[s] = counts.get(s, 0) + 1
    return counts


def get_overused_style(last_n: int = 15) -> str:
    """Kembalikan tipe komentar yang paling sering dipakai belakangan ini."""
    counts = get_style_counts(last_n)
    total  = sum(counts.values())
    if total < 5:
        return ""
    # flagged jika >50% dari total
    for style, cnt in counts.items():
        if cnt / total > 0.5:
            return style
    return ""


def mark_engagement(reply_text: str, caption: str = ""):
    """
    Tandai komentar sebagai 'berhasil' — dipanggil saat reply_watcher
    menemukan ada orang yang balas komentar kita.
    Cocokkan reply_text + caption (jika diberikan) agar tidak salah tandai
    komentar identik yang dikirim ke dua post berbeda.
    """
    cap_prefix = caption[:80] if caption else ""
    with safe_store.file_lock(MEMORY_FILE):
        data = _load()
        # cari dari belakang: exact text match + caption match
        for entry in reversed(data):
            if entry.get("reply", "").strip() == reply_text.strip():
                if not cap_prefix or entry.get("caption", "")[:80] == cap_prefix:
                    entry["engaged"] = True
                    _save(data)
                    return
        # fallback: substring match dengan caption check
        for entry in reversed(data):
            stored = entry.get("reply", "")
            if reply_text[:40] in stored or stored[:40] in reply_text:
                if not cap_prefix or entry.get("caption", "")[:80] == cap_prefix:
                    entry["engaged"] = True
                    _save(data)
                    return


def _negative_block() -> str:
    """
    Seksi prompt berisi contoh komentar buruk kita sendiri. String kosong kalau
    tidak ada. Dipakai dua tempat (memory kosong dan memory terisi) supaya draf
    yang dibuang gate tetap jadi bahan belajar sejak komentar pertama.
    """
    try:
        negative = get_negative_examples(_cfg_int("quality_negative_examples", 3))
    except Exception:
        return ""
    if not negative:
        return ""
    text = "\n\n🚫 CONTOH BURUK komentar lo sendiri — pola kayak gini yang bikin orang ilfeel:"
    for ex in negative:
        text += f'\nJANGAN kayak gini: "{ex["reply"][:120]}" (alasannya: {ex["reason"]})'
    return text + "\n→ ganti polanya: maksudnya harus langsung kebaca, bahasanya tetap sopan"


def build_memory_prompt(n: int = 8) -> str:
    """
    Buat teks memory untuk prompt:
    - Daftar N komentar terakhir + tipe masing-masing
    - ⭐ pada komentar yang dapat balasan (engaged)
    - Hint untuk variasi kalau ada tipe yang dominan
    - Contoh komentar buruk (tidak jelas / tidak sopan) yang wajib dihindari
    """
    with _lock:
        data = _load()

    if not data:
        return "(belum ada histori komentar — ini komentar pertamamu)" + _negative_block()

    recent   = data[-n:]
    overused = get_overused_style()

    lines = []
    for e in recent:
        star  = " ⭐" if e.get("engaged") else ""
        lines.append(f'- "{e["reply"]}" [{e["style"]}]{star}')
    text = "\n".join(lines)

    # Hint engagement kalau ada
    engaged_count = sum(1 for e in recent if e.get("engaged"))
    if engaged_count:
        text += "\n(⭐ = komentar yang dapat balasan — pelajari polanya)"

    # Engagement analytics — panduan berbasis data
    stats = get_engagement_stats()
    if stats:
        insights = []
        best_style = stats.get("best_style")
        best_sent  = stats.get("best_sentiment")
        by_style   = stats.get("by_style", {})
        by_sent    = stats.get("by_sentiment", {})
        if best_style and by_style.get(best_style, {}).get("rate", 0) > 0:
            rate = by_style[best_style]["rate"]
            insights.append(f"gaya '{best_style}' engagement rate tertinggi ({rate:.0%})")
        if best_sent and by_sent.get(best_sent, {}).get("rate", 0) > 0:
            rate = by_sent[best_sent]["rate"]
            insights.append(f"post '{best_sent}' paling sering dibalas ({rate:.0%})")
        if insights:
            text += f"\n📊 Data: {' · '.join(insights)}"

    # Hint style variation
    if overused:
        style_hints = {
            "saran":  "lo udah sering kasih saran — sekarang coba nanya atau react aja biar ga keliatan pattern",
            "react":  "lo udah sering react pendek — coba nanya lanjut atau share pengalaman sebentar",
            "nanya":  "lo udah sering nanya — coba react atau share sesuatu biar variatif",
            "share":  "lo udah sering cerita — coba nanya balik atau react singkat aja",
        }
        text += f"\n\n⚠ Gaya: {style_hints.get(overused, '')}"

    # Hint topic diversity
    overused_topic = get_overused_topic()
    if overused_topic:
        topic_label = {
            "debugging":  "debugging/error",
            "deployment": "deployment/infra",
            "ai_tools":   "AI tools",
            "learning":   "belajar/tutorial",
            "career":     "karir/job",
            "tools":      "tools/framework",
            "opinion":    "opini",
        }.get(overused_topic, overused_topic)
        text += f"\n⚠ Topik: lo baru-baru ini sering komentar soal {topic_label} — coba variasi ke topik lain"

    text += _negative_block()

    return text


def get_recent_topics(n: int = 10) -> list:
    """Kembalikan list topic_bucket dari N komentar terakhir."""
    with _lock:
        data = _load()
    return [e.get("topic_bucket", "misc") for e in data[-n:]]


def get_overused_topic(last_n: int = 6) -> str:
    """Kembalikan topik yang muncul >50% dari N komentar terakhir."""
    topics = get_recent_topics(last_n)
    if len(topics) < 3:
        return ""
    from collections import Counter
    counts = Counter(topics)
    most_common, count = counts.most_common(1)[0]
    if count / len(topics) > 0.5 and most_common != "misc":
        return most_common
    return ""


def get_engagement_stats() -> dict:
    """
    Hitung engagement rate per style dan per sentiment context.
    Return: {
      "by_style":    {"nanya": {"total": 5, "engaged": 3, "rate": 0.6}, ...},
      "by_sentiment": {"frustrated": {"total": 2, "engaged": 2, "rate": 1.0}, ...},
      "best_style":   "nanya",
      "best_sentiment": "frustrated",
    }
    """
    with _lock:
        data = _load()

    if not data:
        return {}

    styles    = ["nanya", "react", "share", "saran"]
    sentiments = ["frustrated", "win", "question", "opinion", "info"]

    by_style     = {s: {"total": 0, "engaged": 0} for s in styles}
    by_sentiment = {s: {"total": 0, "engaged": 0} for s in sentiments}

    for e in data:
        style = e.get("style", "share")
        sent  = e.get("post_sentiment", "info")
        eng   = bool(e.get("engaged"))

        if style in by_style:
            by_style[style]["total"]   += 1
            if eng: by_style[style]["engaged"] += 1

        if sent in by_sentiment:
            by_sentiment[sent]["total"]   += 1
            if eng: by_sentiment[sent]["engaged"] += 1

    def _add_rate(d):
        for v in d.values():
            v["rate"] = round(v["engaged"] / v["total"], 2) if v["total"] > 0 else 0.0

    _add_rate(by_style)
    _add_rate(by_sentiment)

    # Best performers (minimal 3 total untuk dianggap valid)
    best_style = max(
        (s for s in styles if by_style[s]["total"] >= 3),
        key=lambda s: by_style[s]["rate"],
        default="",
    )
    best_sent = max(
        (s for s in sentiments if by_sentiment[s]["total"] >= 3),
        key=lambda s: by_sentiment[s]["rate"],
        default="",
    )

    return {
        "by_style":      by_style,
        "by_sentiment":  by_sentiment,
        "best_style":    best_style,
        "best_sentiment": best_sent,
    }


def get_topic_engagement_stats() -> dict:
    """
    Hitung engagement rate per topic_bucket — versi topik dari get_engagement_stats().
    Return: {
      "by_topic":   {"ai_tools": {"total": 54, "engaged": 13, "rate": 0.24}, ...},
      "best_topic": "ai_tools",
    }

    Entri lama yang tidak punya field topic_bucket dihitung sebagai "misc" (default
    _detect_topic_bucket) supaya tidak ada data yang terbuang. Bucket tak dikenal
    (mis. pola _TOPIC_BUCKETS berubah) tetap dilaporkan apa adanya.

    "misc" ikut ada di by_topic, tapi tidak pernah jadi best_topic: itu bucket
    penampung default yang isinya campur aduk, bukan label topik — alasan yang sama
    dengan get_overused_topic() mengecualikannya.
    """
    with _lock:
        data = _load()

    if not data:
        return {}

    by_topic = {b: {"total": 0, "engaged": 0} for b in list(_TOPIC_BUCKETS) + ["misc"]}

    for e in data:
        topic = e.get("topic_bucket") or "misc"
        if topic not in by_topic:
            by_topic[topic] = {"total": 0, "engaged": 0}
        by_topic[topic]["total"] += 1
        if bool(e.get("engaged")):
            by_topic[topic]["engaged"] += 1

    for v in by_topic.values():
        v["rate"] = round(v["engaged"] / v["total"], 2) if v["total"] > 0 else 0.0

    # Best performer (minimal MIN_TOPIC_SAMPLES total untuk dianggap valid)
    best_topic = max(
        (b for b in by_topic
         if b != "misc" and by_topic[b]["total"] >= MIN_TOPIC_SAMPLES),
        key=lambda b: by_topic[b]["rate"],
        default="",
    )

    return {
        "by_topic":   by_topic,
        "best_topic": best_topic,
    }


def get_recent_replies(n: int = 5) -> list:
    """Kembalikan list teks komentar terbaru (untuk pengecekan duplikat)."""
    with _lock:
        data = _load()
    return [e["reply"] for e in data[-n:]]


def get_engaged_comments(n: int = 10) -> list:
    """Kembalikan N komentar terbaru yang dapat balasan (engaged=True)."""
    with _lock:
        data = _load()
    engaged = [e for e in reversed(data) if e.get("engaged")]
    return engaged[:n]


def _negative_reason(entry: dict, min_score: int) -> str:
    """Alasan singkat kenapa sebuah komentar dipakai sebagai contoh buruk."""
    reasons = []
    clarity    = entry.get("clarity")
    politeness = entry.get("politeness")
    natural    = entry.get("natural")

    if entry.get("unclear") or (isinstance(clarity, (int, float)) and clarity < min_score):
        reasons.append("kurang jelas")
    if entry.get("impolite") or (isinstance(politeness, (int, float)) and politeness < min_score):
        reasons.append("kurang sopan")
    if isinstance(natural, (int, float)) and natural < min_score:
        reasons.append("kaku kayak robot")
    return " & ".join(reasons) if reasons else "skor kualitasnya rendah"


def get_negative_examples(n: int = 3) -> list:
    """
    Kembalikan contoh komentar buruk kita sendiri supaya AI tidak mengulang polanya.

    Hanya entri yang benar-benar jelek yang diambil (ber-flag unclear/impolite atau
    skor terendahnya di bawah quality_min_score) — n yang lebih besar dari jumlah
    komentar buruk tidak boleh menyeret komentar bagus ke dalam daftar "jangan".
    Urutan: yang ber-flag dulu (ini keluhan utama user), lalu skor terendah, lalu terbaru.
    Entri lama tanpa field skor dilewati — tidak ada bukti kalau itu buruk.

    Return: [{"reply", "reason", "natural", "clarity", "politeness", "worst"}, ...]
    """
    if n <= 0:
        return []

    min_score = _cfg_int("quality_min_score", MIN_SCORE_FALLBACK)
    with _lock:
        data     = _load()
        rejected = _load_rejected()

    candidates = []

    # Draf yang dibuang quality gate: sudah pasti contoh buruk, tidak difilter ulang.
    # Inilah sumber utama — memory utama hanya berisi komentar yang lolos gate.
    for e in rejected:
        reply = (e.get("reply") or "").strip()
        if not reply:
            continue
        scores = _entry_scores(e)
        candidates.append({
            "flagged":    True,
            "worst":      min(scores) if scores else 0,
            "ts":         e.get("ts", 0) or 0,
            "reply":      reply,
            "reason":     e.get("reason") or _negative_reason(e, min_score),
            "natural":    e.get("natural"),
            "clarity":    e.get("clarity"),
            "politeness": e.get("politeness"),
        })

    for e in data:
        reply = (e.get("reply") or "").strip()
        if not reply:
            continue
        scores = _entry_scores(e)
        if not scores:
            continue        # entri lama: belum pernah dinilai
        worst   = min(scores)
        flagged = bool(e.get("unclear")) or bool(e.get("impolite"))
        if not flagged and worst >= min_score:
            continue        # komentar ini baik-baik saja, jangan dijadikan contoh buruk
        candidates.append({
            "flagged":    flagged,
            "worst":      worst,
            "ts":         e.get("ts", 0) or 0,
            "reply":      reply,
            "reason":     _negative_reason(e, min_score),
            "natural":    e.get("natural"),
            "clarity":    e.get("clarity"),
            "politeness": e.get("politeness"),
        })

    candidates.sort(key=lambda c: (not c["flagged"], c["worst"], -c["ts"]))
    return [
        {
            "reply":      c["reply"],
            "reason":     c["reason"],
            "natural":    c["natural"],
            "clarity":    c["clarity"],
            "politeness": c["politeness"],
            "worst":      c["worst"],
        }
        for c in candidates[:n]
    ]


def get_summary() -> dict:
    """Ringkasan statistik: total, engaged, overall engagement rate."""
    with _lock:
        data = _load()
    total_count = len(data)
    engaged_count = sum(1 for e in data if e.get("engaged"))
    return {
        "total":   total_count,
        "engaged": engaged_count,
        "rate":    round(engaged_count / total_count, 2) if total_count else 0.0,
    }


def total() -> int:
    with _lock:
        return len(_load())
