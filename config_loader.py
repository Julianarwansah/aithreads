"""
config_loader.py -- Baca/tulis ui_settings.json.

Default dipakai kalau file belum ada atau key-nya hilang.
"""
import json
import os
import threading

_DIR          = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_DIR, "ui_settings.json")
_lock         = threading.RLock()   # RLock: boleh di-acquire ulang oleh thread yang sama

DEFAULTS = {
    # mode & filter
    "app_mode":             "normal",
    "max_post_age_hours":   5,
    "it_ai_keywords_extra": [],
    "loker_keywords_extra": [],

    # komentar
    "delay_min_minutes":    14,
    "delay_max_minutes":    17,
    "max_tokens":           120,
    "max_comment_length":   350,

    # jadwal aktif (jam lokal)
    "active_start_hour":    7,          # mulai jam 07:00
    "active_end_hour":      23,         # berhenti jam 23:00

    # watchdog
    "watchdog_enabled":     True,       # auto-restart kalau bot crash

    # poster
    "poster_enabled":       False,
    "poster_interval_hours": 48,        # jeda antar postingan
    "poster_topics": [                  # topik yang dirotasi
        "ngoding dan debugging",
        "AI dan tools terbaru",
        "developer life",
        "tech opinion",
        "startup dan produk digital",
    ],

    # blacklist akun (username tanpa @)
    "blacklisted_accounts": [],

    # like sebelum komentar
    "like_before_comment":  True,

    # auto-reply ke balasan
    "auto_reply_enabled":          False,
    "auto_reply_check_minutes":    30,

    # qoder
    "qoder_bin":            "",
    "qoder_model":          "",

    # persona bot — deskripsi singkat karakter, dimasukkan ke prompt agar komentar konsisten
    "bot_persona": (
        "lo developer Indonesia, udah ~3 tahun di web dev, sekarang lagi ngulik AI tools. "
        "lo agak skeptis sama hype tapi tetap curious. "
        "lo pake python dan js sehari-hari, sering stuck di hal kecil, seneng pas akhirnya solved."
    ),
}


def load() -> dict:
    with _lock:
        if not os.path.exists(SETTINGS_FILE):
            return DEFAULTS.copy()
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            merged = DEFAULTS.copy()
            merged.update(data)
            return merged
        except Exception:
            return DEFAULTS.copy()


def save(partial: dict) -> bool:
    with _lock:
        current = load()
        current.update(partial)
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False


def get(key: str):
    return load().get(key, DEFAULTS.get(key))
