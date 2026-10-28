"""
Regresi untuk batas arsitektur yang diminta eksplisit:
plafon harian HANYA untuk komentar proaktif, balasan bebas.

Kedua alur memakai fungsi kirim yang sama (post_reply di auto_commenter,
diimpor oleh reply_watcher), jadi pemisahan ini tidak terlihat dari tipe data --
hanya dari di mana daily_quota.increment dipanggil. Test ini mengunci batas itu
di tingkat impor modul: kalau suatu saat kuota dipasang ke jalur balasan,
test gagal dan alasannya terbaca.
"""
import ast
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _nama_modul_yang_diimpor(path):
    """Kumpulkan semua nama modul yang diimpor sebuah file, lewat AST (bukan grep teks)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    nama = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            nama.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            nama.add(node.module)
    return nama


def test_reply_watcher_tidak_memakai_kuota_harian():
    impor = _nama_modul_yang_diimpor(os.path.join(PROJECT_ROOT, "reply_watcher.py"))

    assert "daily_quota" not in impor, (
        "reply_watcher mulai memakai daily_quota -- balasan ke orang yang membalas "
        "harus tetap bebas dari plafon harian. Kalau ini memang disengaja, ubah "
        "test ini secara sadar, jangan diam-diam."
    )


def test_auto_commenter_memakai_kuota_harian():
    impor = _nama_modul_yang_diimpor(os.path.join(PROJECT_ROOT, "auto_commenter.py"))

    assert "daily_quota" in impor, (
        "auto_commenter tidak lagi memakai daily_quota -- plafon harian komentar "
        "proaktif hilang"
    )


def test_alur_pengirim_lain_tidak_ikut_dibatasi():
    """
    Yang berbahaya adalah kalau plafon 40/hari dipasang ke alur PENGIRIM lain
    yang tidak pernah diminta user untuk dibatasi. web_ui.py boleh membaca kuota
    (untuk menampilkannya di dashboard) -- itu bukan penegakan plafon.
    """
    pengirim = [
        "reply_watcher.py",         # balasan: harus bebas
        "threads_poster.py",        # postingan: sudah punya poster_daily_quota sendiri
        "job_hunter.py",            # komentar lowongan LinkedIn: alur terpisah
        "linkedin_commenter.py",
        "linkedin_poster.py",
        "linkedin_reply_watcher.py",
        "threads_liker.py",
    ]
    bersalah = []
    for nama in pengirim:
        path = os.path.join(PROJECT_ROOT, nama)
        if not os.path.exists(path):
            continue
        if "daily_quota" in _nama_modul_yang_diimpor(path):
            bersalah.append(nama)

    assert not bersalah, (
        f"{bersalah} mulai memakai daily_quota. Plafon 40/hari hanya untuk komentar "
        "proaktif Threads di auto_commenter.py -- alur lain punya batasnya sendiri "
        "atau memang sengaja tidak dibatasi."
    )
