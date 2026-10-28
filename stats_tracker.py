import os
from datetime import datetime

import safe_store

_DIR       = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR  = os.getenv("PROFILE_DIR", _DIR)
STATS_FILE = os.path.join(_DATA_DIR, "stats.json")

# Cuplikan komentar disimpan di stats.json yang dibaca-tulis oleh banyak proses;
# tanpa batas, satu komentar panjang ikut tersalin setiap kali file ditulis ulang.
MAX_COMMENT_TEXT = 300

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
    "last_comment_text": "",
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


def _with_defaults(data):
    """Pastikan semua key DEFAULT_STATS ada, tanpa menimpa nilai yang sudah tersimpan."""
    if not isinstance(data, dict):
        return DEFAULT_STATS.copy()
    for k, v in DEFAULT_STATS.items():
        data.setdefault(k, v)
    return data


def _update(mutator):
    """
    Read-modify-write atomik yang aman dipanggil dari proses mana pun.

    Semua penulis stats.json (bot, poster, watcher, job hunter, web_ui) lewat sini,
    jadi counter tidak lagi saling menimpa.
    """
    def _apply(stats):
        stats = _with_defaults(stats)
        mutator(stats)
        return stats

    return safe_store.update_json(STATS_FILE, _apply, DEFAULT_STATS.copy())


def get_current_time_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_system_start():
    def _set(stats):
        stats["system_start_time"] = get_current_time_str()
    _update(_set)


def update_heartbeat(timestamp: float):
    def _set(stats):
        stats["last_heartbeat"] = timestamp
    _update(_set)


def increment(key: str):
    """Naikkan counter dan kembalikan total terbarunya.

    Total yang dikembalikan adalah angka seumur hidup dari stats.json, jadi
    pemanggil (mis. log watchdog di web_ui) bisa menampilkan angka yang tidak
    ikut ter-reset saat prosesnya di-restart.
    """
    now = get_current_time_str()

    def _inc(stats):
        stats[key] = stats.get(key, 0) + 1
        if key == "comments_sent":
            stats["last_comment_time"] = now
        elif key == "posts_created":
            stats["last_post_time"] = now

    return _update(_inc).get(key, 0)


def log_comment(text: str):
    """Catat waktu dan cuplikan komentar terakhir yang berhasil dikirim."""
    stamp = get_current_time_str()
    snippet = str(text or "")[:MAX_COMMENT_TEXT]

    def _set(stats):
        stats["last_comment_time"] = stamp
        stats["last_comment_text"] = snippet

    _update(_set)


def log_error(error_msg: str):
    stamp = get_current_time_str()

    def _set(stats):
        stats["last_error"] = str(error_msg)
        stats["last_error_time"] = stamp

    _update(_set)


def get_stats():
    return _with_defaults(safe_store.read_json(STATS_FILE, DEFAULT_STATS.copy()))
