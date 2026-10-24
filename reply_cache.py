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


def save_to_cache(prompt: str, reply: str):
    with _lock:
        cache = _load()
        cache[prompt] = {"reply": reply, "timestamp": time.time()}
        _save(cache)
