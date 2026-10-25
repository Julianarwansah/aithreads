"""
comment_memory.py -- Persistent memory komentar bot.

Menyimpan histori komentar kita sendiri + mendeteksi pola
agar AI terus berkembang dan tidak repetitif.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

_DIR        = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.getenv("PROFILE_DIR", _DIR)
MEMORY_FILE = os.path.join(_DATA_DIR, "comment_memory.json")
MAX_ENTRIES = 100
_lock       = threading.Lock()


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
    "debugging":  r'\b(error|bug|debug|stuck|crash|exception|traceback|fix|solve)\b',
    "deployment": r'\b(deploy|ci/cd|docker|kubernetes|server|production|staging|pipeline)\b',
    "ai_tools":   r'\b(ai|llm|chatgpt|gpt|claude|copilot|ml|model|prompt|dataset)\b',
    "learning":   r'\b(belajar|tutorial|course|bootcamp|pemula|newbie|ngerti|paham)\b',
    "career":     r'\b(kerja|karir|salary|gaji|interview|job|freelance|remote|hiring)\b',
    "tools":      r'\b(vscode|vim|git|github|linux|terminal|framework|library|stack)\b',
    "opinion":    r'\b(menurut|tbh|ngl|sebenarnya|kayaknya|gue rasa|hot take|unpopular)\b',
}

def _detect_topic_bucket(caption: str) -> str:
    low = caption.lower()
    for bucket, pattern in _TOPIC_BUCKETS.items():
        if re.search(pattern, low):
            return bucket
    return "misc"


# ── storage ───────────────────────────────────────────────────────────────────
def _load() -> list:
    try:
        if Path(MEMORY_FILE).exists():
            data = json.loads(Path(MEMORY_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save(data: list):
    Path(MEMORY_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save(caption: str, reply: str, post_sentiment: str = "info"):
    """Simpan komentar baru ke memory."""
    with _lock:
        data = _load()
        data.append({
            "ts":             time.time(),
            "caption":        caption[:120],
            "reply":          reply,
            "style":          _detect_style(reply),
            "topic_bucket":   _detect_topic_bucket(caption),
            "post_sentiment": post_sentiment,
        })
        if len(data) > MAX_ENTRIES:
            data = data[-MAX_ENTRIES:]
        _save(data)


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
    with _lock:
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


def build_memory_prompt(n: int = 8) -> str:
    """
    Buat teks memory untuk prompt:
    - Daftar N komentar terakhir + tipe masing-masing
    - ⭐ pada komentar yang dapat balasan (engaged)
    - Hint untuk variasi kalau ada tipe yang dominan
    """
    with _lock:
        data = _load()

    if not data:
        return "(belum ada histori komentar — ini komentar pertamamu)"

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
        text += f"\n(⭐ = komentar yang dapat balasan — pelajari polanya)"

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
