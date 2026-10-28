"""
Test untuk daily_quota -- plafon harian komentar bot Threads.

Konteks: poster sudah punya kuota harian (poster_daily_count.json), tetapi
auto_commenter tidak punya plafon sama sekali. Data produksi menunjukkan bot
mengirim 18 komentar pada 27 Okt dan 28 komentar sebelum pukul 14:12 pada
28 Okt -- diproyeksikan menutup hari di ~45-50.

Plafon ini sengaja HANYA untuk komentar proaktif. Balasan ke orang yang
membalas (reply_watcher) tidak dihitung dan tidak dibatasi, jadi penghitung
ini tidak boleh dipanggil dari reply_watcher.

Konvensi mengikuti poster: quota 0 = tanpa batas.
"""
import datetime as dt
import json
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def quota_file(tmp_path):
    return str(tmp_path / "bot_daily_count.json")


# ── increment & read ──────────────────────────────────────────────────────────

def test_increment_pertama_membuat_file(quota_file):
    import daily_quota

    assert daily_quota.increment(quota_file) == 1

    data = json.loads(open(quota_file, encoding="utf-8").read())
    assert data["date"] == dt.date.today().strftime("%Y-%m-%d")
    assert data["count"] == 1


def test_increment_berurutan_naik_satu_per_satu(quota_file):
    import daily_quota

    assert [daily_quota.increment(quota_file) for _ in range(3)] == [1, 2, 3]
    assert daily_quota.read(quota_file) == 3


def test_read_file_belum_ada_menghasilkan_nol(quota_file):
    import daily_quota

    assert daily_quota.read(quota_file) == 0


# ── reset harian ──────────────────────────────────────────────────────────────

def test_rollover_tanggal_menghitung_nol(quota_file):
    """File berisi hitungan kemarin harus dibaca 0 hari ini."""
    import daily_quota

    kemarin = dt.date.today() - dt.timedelta(days=1)
    with open(quota_file, "w", encoding="utf-8") as f:
        json.dump({"date": kemarin.strftime("%Y-%m-%d"), "count": 39}, f)

    assert daily_quota.read(quota_file) == 0, (
        "hitungan kemarin tidak di-reset -- plafon harian jadi plafon seumur hidup"
    )


def test_increment_setelah_rollover_mulai_dari_satu(quota_file):
    import daily_quota

    kemarin = dt.date.today() - dt.timedelta(days=1)
    with open(quota_file, "w", encoding="utf-8") as f:
        json.dump({"date": kemarin.strftime("%Y-%m-%d"), "count": 39}, f)

    assert daily_quota.increment(quota_file) == 1

    data = json.loads(open(quota_file, encoding="utf-8").read())
    assert data["date"] == dt.date.today().strftime("%Y-%m-%d")


def test_inject_tanggal_eksplisit_untuk_rollover(quota_file):
    """Rollover harus bisa diuji tanpa menunggu tengah malam."""
    import daily_quota

    hari_ini = dt.datetime(2026, 10, 28, 23, 59)
    besok = dt.datetime(2026, 10, 29, 0, 1)

    for _ in range(5):
        daily_quota.increment(quota_file, now=hari_ini)
    assert daily_quota.read(quota_file, now=hari_ini) == 5
    assert daily_quota.read(quota_file, now=besok) == 0
    assert daily_quota.increment(quota_file, now=besok) == 1


# ── ketahanan terhadap file rusak ─────────────────────────────────────────────

def test_file_korup_dianggap_nol(quota_file):
    import daily_quota

    with open(quota_file, "w", encoding="utf-8") as f:
        f.write('{"date": "2026-10-28", "count": ')   # JSON terpotong

    assert daily_quota.read(quota_file) == 0
    assert daily_quota.increment(quota_file) == 1, "harus pulih, bukan macet"


def test_count_bukan_angka_dianggap_nol(quota_file):
    import daily_quota

    with open(quota_file, "w", encoding="utf-8") as f:
        json.dump({"date": dt.date.today().strftime("%Y-%m-%d"), "count": "banyak"}, f)

    assert daily_quota.read(quota_file) == 0


def test_count_negatif_dibulatkan_ke_nol(quota_file):
    import daily_quota

    with open(quota_file, "w", encoding="utf-8") as f:
        json.dump({"date": dt.date.today().strftime("%Y-%m-%d"), "count": -7}, f)

    assert daily_quota.read(quota_file) == 0


# ── semantik plafon ───────────────────────────────────────────────────────────

def test_is_exhausted_tepat_di_batas():
    import daily_quota

    assert daily_quota.is_exhausted(40, 39) is False
    assert daily_quota.is_exhausted(40, 40) is True, "tepat di plafon harus berhenti"
    assert daily_quota.is_exhausted(40, 41) is True


def test_quota_nol_berarti_tanpa_batas():
    import daily_quota

    assert daily_quota.is_exhausted(0, 0) is False
    assert daily_quota.is_exhausted(0, 9999) is False, (
        "quota 0 harus berarti tanpa batas, mengikuti konvensi poster_daily_quota"
    )


def test_quota_rusak_dianggap_tanpa_batas():
    """Config rusak tidak boleh menghentikan bot sama sekali."""
    import daily_quota

    for nilai in ("abc", None, -5, ""):
        assert daily_quota.is_exhausted(nilai, 500) is False, f"quota={nilai!r} harus aman"


def test_remaining():
    import daily_quota

    assert daily_quota.remaining(40, 12) == 28
    assert daily_quota.remaining(40, 40) == 0
    assert daily_quota.remaining(40, 45) == 0, "tidak boleh negatif"
    assert daily_quota.remaining(0, 12) is None, "tanpa batas -> tidak ada angka sisa"


# ── aman lintas proses ────────────────────────────────────────────────────────

def _worker(path, times, out_file):
    sys.path.insert(0, PROJECT_ROOT)
    import daily_quota

    nilai = [daily_quota.increment(path) for _ in range(times)]
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(nilai, f)


def test_increment_aman_lintas_proses(quota_file, tmp_path):
    """
    Bot per-akun berjalan sebagai proses terpisah dengan PROFILE_DIR sendiri,
    dan watchdog bisa meluncurkan ulang bot. Dua penulis pada file yang sama
    tidak boleh saling menimpa hitungan.
    """
    ctx = mp.get_context("spawn")
    outs, procs = [], []
    for i in range(4):
        out = tmp_path / f"w{i}.json"
        outs.append(out)
        p = ctx.Process(target=_worker, args=(quota_file, 10, str(out)))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(60)

    semua = []
    for out in outs:
        assert out.exists(), "worker tidak menulis hasil"
        semua.extend(json.loads(out.read_text(encoding="utf-8")))

    assert sorted(semua) == list(range(1, 41)), (
        f"40 increment lintas 4 proses harus menghasilkan 1..40 tanpa lost update, "
        f"dapat {sorted(semua)}"
    )

    import daily_quota
    assert daily_quota.read(quota_file) == 40
