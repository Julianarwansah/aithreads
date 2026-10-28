"""
persona_history.py -- jejak perubahan persona bot.

refine_persona() di auto_commenter menimpa bot_persona setiap 15 komentar
berhasil, dan suntingan manual dari dashboard menimpanya lagi kapan saja. Tanpa
riwayat, satu keluaran LLM yang buruk menghilangkan persona hasil tuning secara
permanen dan tidak ada cara melihat kenapa karakter bot berubah.

Modul ini menyimpan setiap perubahan sebagai satu entri
    {"ts", "persona", "source", "comments_sent"}
supaya bisa dibandingkan dan dipulihkan dari dashboard.

Ditulis lewat safe_store karena dua proses berbeda (bot dan web_ui) menambah
entri ke file yang sama.
"""
from datetime import datetime

import safe_store

# Cukup untuk melihat tren perubahan tanpa membuat file membengkak.
# Entri terlama dibuang lebih dulu.
MAX_ENTRIES = 20
# Persona hasil refine dibatasi 500 karakter di auto_commenter; suntingan manual
# bisa lebih panjang. Batas ini menjaga satu entri tidak mendominasi file.
MAX_PERSONA_CHARS = 1000


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read(path) -> list:
    """Kembalikan riwayat dari terlama ke terbaru; [] kalau file belum ada atau rusak."""
    data = safe_store.read_json(path, [])
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def record(path, persona, source, comments_sent=None) -> list:
    """Catat satu perubahan persona dan kembalikan riwayat terbarunya."""
    entri = {
        "ts": _now(),
        "persona": str(persona or "")[:MAX_PERSONA_CHARS],
        "source": str(source or "?"),
        "comments_sent": comments_sent,
    }

    def _mut(data):
        riwayat = data if isinstance(data, list) else []
        riwayat = [e for e in riwayat if isinstance(e, dict)]
        riwayat.append(entri)
        return riwayat[-MAX_ENTRIES:]

    return safe_store.update_json(path, _mut, [])
