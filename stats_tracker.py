import json
import os
import threading
from datetime import datetime

_DIR       = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.getenv("PROFILE_DIR", _DIR)
STATS_FILE = os.path.join(_DATA_DIR, "stats.json")
_lock = threading.Lock()

DEFAULT_STATS = {
    "system_start_time": "",
    "comments_sent": 0,
    "comment_failures": 0,
    "posts_created": 0,
    "post_failures": 0,
    "llm_errors": 0,
    "watchdog_restarts": 0,
    "trend_posts_found": 0,
    "trend_posts_selected": 0,
    "trend_fallbacks": 0,
    "last_comment_time": "",
    "last_post_time": "",
    "last_error": "",
    "last_error_time": "",
    "last_heartbeat": 0.0,
    # LinkedIn stats
    "li_comments_sent": 0,
    "li_comments_skipped": 0,
    "li_posts_sent": 0,
    "li_posts_skipped": 0,
    "li_replies_sent": 0,
    "li_errors": 0,
    # Job Hunter stats
    "job_comments_sent": 0,
    "job_comment_failures": 0,
    "job_dms_sent": 0,
}

def _load_stats():
    if not os.path.exists(STATS_FILE):
        return DEFAULT_STATS.copy()
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Ensure all keys exist
            for k, v in DEFAULT_STATS.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        return DEFAULT_STATS.copy()

def _save_stats(data):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def get_current_time_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_system_start():
    with _lock:
        stats = _load_stats()
        stats["system_start_time"] = get_current_time_str()
        _save_stats(stats)

def update_heartbeat(timestamp: float):
    with _lock:
        stats = _load_stats()
        stats["last_heartbeat"] = timestamp
        _save_stats(stats)

def increment(key: str):
    with _lock:
        stats = _load_stats()
        if key in stats:
            stats[key] += 1
        else:
            # Allow dynamic LinkedIn keys (li_*) even if not in DEFAULT_STATS
            stats[key] = 1

        # Auto-update corresponding timestamps
        time_str = get_current_time_str()
        if key == "comments_sent":
            stats["last_comment_time"] = time_str
        elif key == "posts_created":
            stats["last_post_time"] = time_str

        _save_stats(stats)

def log_comment(text: str):
    with _lock:
        stats = _load_stats()
        stats["last_comment_time"] = get_current_time_str()
        _save_stats(stats)

def log_error(error_msg: str):
    with _lock:
        stats = _load_stats()
        stats["last_error"] = str(error_msg)
        stats["last_error_time"] = get_current_time_str()
        _save_stats(stats)

def get_stats():
    with _lock:
        return _load_stats()
