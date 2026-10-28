"""
config_loader.py -- Baca/tulis ui_settings.json.

Default dipakai kalau file belum ada atau key-nya hilang.
"""
import os
import threading

import safe_store

_DIR          = os.path.dirname(os.path.abspath(__file__))
# Jika dijalankan sebagai sub-proses akun, gunakan folder profil akun tersebut
_DATA_DIR     = os.getenv("PROFILE_DIR", _DIR)
# Akun profil pakai config.json; root (default) tetap pakai ui_settings.json
SETTINGS_FILE = os.path.join(_DATA_DIR, "config.json" if os.getenv("PROFILE_DIR") else "ui_settings.json")
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
    "bot_daily_quota":      40,         # plafon komentar proaktif per hari kalender;
                                        # 0 = tanpa batas. Balasan (reply_watcher) tidak dihitung.

    # jadwal aktif (jam lokal)
    "active_start_hour":    7,          # mulai jam 07:00
    "active_end_hour":      23,         # berhenti jam 23:00

    # watchdog
    "watchdog_enabled":     True,       # auto-restart kalau bot crash

    # poster
    "poster_enabled":       False,
    "poster_interval_hours": 48,        # jeda antar postingan; 0 = nonaktif (pakai kuota harian)
    "poster_daily_quota":   5,          # maksimal postingan per hari kalender; 0 = tanpa batas
    "poster_min_gap_minutes": 120,      # jarak minimum antar postingan; 0 = nonaktif
    "poster_peak_only":     True,       # hanya boleh posting di jam ramai (reply_cache.get_peak_hours)
    "poster_topics": [                  # topik yang dirotasi
        "ngoding dan debugging",
        "AI dan tools terbaru",
        "developer life",
        "tech opinion",
        "startup dan produk digital",
    ],

    # quality gate komentar — learning loop 3 dimensi (natural / jelas / sopan)
    "quality_min_score":         6,     # ambang lolos per dimensi, skala 1-10
    "quality_max_retries":       2,     # generate ulang maks sebelum komentar di-skip
    "quality_negative_examples": 3,     # jumlah contoh buruk yang disuntik ke prompt

    # klasifikasi balasan masuk (reply_watcher)
    "reply_classify_enabled":  True,
    "auto_blacklist_impolite": False,   # permanen memblokir orang dari 1 penilaian LLM — opt-in

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
        data = safe_store.read_json(SETTINGS_FILE, {})
        merged = DEFAULTS.copy()
        if isinstance(data, dict):
            merged.update(data)
        return merged


def save(partial: dict) -> bool:
    # file_lock antar-proses + tulis atomik: tanpa ini, bot yang sedang membaca
    # config bisa melihat file setengah tertulis dan diam-diam jatuh ke DEFAULTS.
    with _lock, safe_store.file_lock(SETTINGS_FILE):
        current = load()
        current.update(partial)
        try:
            safe_store.write_json(SETTINGS_FILE, current)
            return True
        except Exception:
            return False


def get(key: str):
    return load().get(key, DEFAULTS.get(key))
