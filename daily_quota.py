"""
daily_quota.py -- penghitung kuota harian per-profil, aman lintas proses.

Dipakai auto_commenter untuk memberi plafon pada jumlah komentar proaktif per
hari kalender. Balasan ke orang yang membalas (reply_watcher) sengaja TIDAK
memakai modul ini -- membiarkan balasan menggantung lebih berisiko daripada
mengirim komentar baru, jadi balasan tidak dibatasi.

Bentuk file mengikuti poster_daily_count.json yang sudah ada:
    {"date": "YYYY-MM-DD", "count": N}
tetapi ditulis lewat safe_store, karena watchdog bisa meluncurkan ulang bot
sementara proses lama masih dalam tahap shutdown dan keduanya sempat menulis.

Konvensi quota mengikuti poster_daily_quota: 0 (atau nilai rusak) = tanpa batas.
"""
from datetime import datetime

import safe_store


def _today(now=None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_number(value) -> float:
    """Config bisa berisi angka, string angka, atau sampah; sampah -> 0 (tanpa batas)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read(path, now=None) -> int:
    """Jumlah hitungan pada hari kalender berjalan; 0 kalau tanggalnya bukan hari ini.

    Reset harian terjadi di sini: file kemarin dibaca sebagai 0, dan baru ditulis
    ulang pada increment berikutnya. Jadi tidak perlu tugas terjadwal tengah malam.
    """
    data = safe_store.read_json(path, {})
    if not isinstance(data, dict):
        return 0
    if str(data.get("date", "")) != _today(now):
        return 0
    return max(0, _as_int(data.get("count", 0)))


def increment(path, now=None) -> int:
    """Naikkan hitungan hari ini dan kembalikan total terbarunya."""
    day = _today(now)

    def _mut(data):
        if not isinstance(data, dict):
            data = {}
        same_day = str(data.get("date", "")) == day
        count = max(0, _as_int(data.get("count", 0))) if same_day else 0
        return {"date": day, "count": count + 1}

    return _as_int(safe_store.update_json(path, _mut, {}).get("count"))


def is_exhausted(quota, count) -> bool:
    """True kalau plafon harian sudah tercapai. quota <= 0 atau rusak = tanpa batas."""
    q = _as_number(quota)
    if q <= 0:
        return False
    return _as_int(count) >= int(q)


def remaining(quota, count):
    """Sisa kuota hari ini, atau None kalau tanpa batas (untuk ditampilkan di dashboard)."""
    q = _as_number(quota)
    if q <= 0:
        return None
    return max(0, int(q) - _as_int(count))
