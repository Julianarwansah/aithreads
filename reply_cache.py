"""
reply_cache.py -- Cache balasan per caption (TTL 24 jam).

Mencegah bot mengirim balasan yang sama persis untuk postingan yang muncul lagi.
"""
import json
import os
import threading
import time

CACHE_FILE = os.getenv("REPLY_CACHE_FILE", "reply_cache.json")
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


def save_to_cache(prompt: str, reply: str, post_id: str = ""):
    with _lock:
        cache = _load()
        cache[prompt] = {"reply": reply, "timestamp": time.time(), "post_id": post_id}
        _save(cache)


def get_recent_post_ids(max_age_hours: float = 24) -> list[dict]:
    """Kembalikan list {post_id, caption, reply} yang dikomentari dalam N jam terakhir."""
    with _lock:
        cache = _load()
        now   = time.time()
        cutoff = now - max_age_hours * 3600
        result = []
        for caption, v in cache.items():
            if v.get("timestamp", 0) >= cutoff and v.get("post_id"):
                result.append({
                    "post_id":   v["post_id"],
                    "caption":   caption,
                    "our_reply": v.get("reply", ""),
                    "ts":        v["timestamp"],
                })
        return sorted(result, key=lambda x: x["ts"], reverse=True)
