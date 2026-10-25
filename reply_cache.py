"""
reply_cache.py -- Cache balasan per caption (TTL 24 jam).

Mencegah bot mengirim balasan yang sama persis untuk postingan yang muncul lagi.
"""
import json
import os
import threading
import time

_DIR      = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("PROFILE_DIR", _DIR)
CACHE_FILE = os.getenv("REPLY_CACHE_FILE", os.path.join(_DATA_DIR, "reply_cache.json"))
TTL_SECONDS = 86400
_lock = threading.Lock()


def _load():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def check_cache(prompt: str):
    with _lock:
        cache = _load()
        now = time.time()
        expired = [k for k, v in cache.items() if now - v.get("timestamp", 0) > TTL_SECONDS]
        for key in expired:
            del cache[key]
        if expired:
            _save(cache)
        entry = cache.get(prompt)
        return entry.get("reply") if entry else None


def save_to_cache(prompt: str, reply: str, post_id: str = "", comment_id: str = ""):
    with _lock:
        cache = _load()
        cache[prompt] = {
            "reply":      reply,
            "timestamp":  time.time(),
            "post_id":    post_id,
            "comment_id": comment_id,
        }
        _save(cache)


def get_activity_by_hour() -> dict:
    """Hitung berapa komentar per jam dari 7 hari terakhir (hanya entry dengan post_id)."""
    with _lock:
        cache = _load()
    now = time.time()
    counts = {h: 0 for h in range(24)}
    for v in cache.values():
        ts = v.get("timestamp", 0)
        if v.get("post_id") and (now - ts) < 86400 * 7:
            h = int(time.strftime("%H", time.localtime(ts)))
            counts[h] += 1
    return counts


def get_peak_hours(top_n: int = 5) -> set:
    """Kembalikan set jam dengan aktivitas komentar tertinggi (dari 7 hari terakhir)."""
    counts = get_activity_by_hour()
    if not any(counts.values()):
        return set()
    sorted_hours = sorted(counts, key=lambda h: counts[h], reverse=True)
    return {h for h in sorted_hours[:top_n] if counts[h] > 0}


def get_recent_post_ids(max_age_hours: float = 24) -> list[dict]:
    """Kembalikan list {post_id, comment_id, caption, our_reply} yang dikomentari dalam N jam terakhir."""
    with _lock:
        cache = _load()
        now    = time.time()
        cutoff = now - max_age_hours * 3600
        result = []
        for caption, v in cache.items():
            if v.get("timestamp", 0) >= cutoff and v.get("post_id"):
                result.append({
                    "post_id":    v["post_id"],
                    "comment_id": v.get("comment_id", ""),
                    "caption":    caption,
                    "our_reply":  v.get("reply", ""),
                    "ts":         v["timestamp"],
                })
        return sorted(result, key=lambda x: x["ts"], reverse=True)
