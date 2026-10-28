"""
Test untuk dua bug HIGH pada stats_tracker / dashboard.

Bug #21 -- stats_tracker.log_comment(text) mengabaikan parameter `text`
sepenuhnya. linkedin_commenter.py:505 mengirim teks komentar asli, tetapi
fungsi hanya menulis last_comment_time. Akibatnya tidak ada jejak komentar
terakhir yang bisa dilihat di dashboard, dan parameternya menipu pembaca kode.

Bug #22 -- web_ui.py:249 melaporkan `_watchdog_restart_count`, yaitu counter
in-process yang kembali ke 0 setiap kali web_ui di-restart, sementara
stats.json menyimpan `watchdog_restarts` yang kumulatif. Angka "Watchdog
Restart" di UI jadi tidak bisa dipercaya. Agar UI bisa menampilkan total yang
benar, stats_tracker.increment() harus mengembalikan nilai counter terbaru --
dan nilai itu harus benar-benar kumulatif lintas proses, bukan per-proses.
"""
import json
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "profile"
    data.mkdir()
    monkeypatch.setenv("PROFILE_DIR", str(data))
    monkeypatch.setattr(sys, "path", [PROJECT_ROOT] + sys.path)
    import stats_tracker
    monkeypatch.setattr(stats_tracker, "STATS_FILE",
                        os.path.join(str(data), "stats.json"))
    return str(data)


# ── Bug #21: log_comment harus menyimpan teks ─────────────────────────────────

def test_log_comment_menyimpan_teks(isolated_data_dir):
    import stats_tracker

    stats_tracker.log_comment("komentar singkat tentang AI")
    stats = stats_tracker.get_stats()

    assert stats.get("last_comment_text") == "komentar singkat tentang AI", (
        "log_comment(text) membuang parameternya: teks komentar tidak pernah "
        "tersimpan di stats.json"
    )


def test_kunci_last_comment_text_selalu_ada(isolated_data_dir):
    import stats_tracker

    stats = stats_tracker.get_stats()

    assert "last_comment_text" in stats, (
        "last_comment_text harus ada di DEFAULT_STATS supaya dashboard tidak "
        "perlu menangani key yang hilang"
    )


def test_log_comment_memotong_teks_sangat_panjang(isolated_data_dir):
    import stats_tracker

    stats_tracker.log_comment("x" * 5000)
    tersimpan = stats_tracker.get_stats().get("last_comment_text", "")

    assert tersimpan, "teks panjang harus tersimpan, bukan hilang"
    assert len(tersimpan) <= 300, (
        f"stats.json membengkak: last_comment_text {len(tersimpan)} karakter, "
        "harus dipotong"
    )
    assert set(tersimpan) == {"x"}, "isi teks harus tetap utuh, hanya dipotong"


def test_log_comment_tetap_mencatat_waktu(isolated_data_dir):
    import stats_tracker

    stats_tracker.log_comment("halo")
    stats = stats_tracker.get_stats()

    assert stats.get("last_comment_time"), (
        "perilaku lama (last_comment_time) tidak boleh hilang"
    )


def test_log_comment_teks_kosong_tidak_error(isolated_data_dir):
    import stats_tracker

    stats_tracker.log_comment("")

    assert stats_tracker.get_stats().get("last_comment_text") == ""


# ── Bug #22: increment harus mengembalikan total kumulatif ────────────────────

def test_increment_mengembalikan_nilai_baru(isolated_data_dir):
    import stats_tracker

    pertama = stats_tracker.increment("watchdog_restarts")
    kedua = stats_tracker.increment("watchdog_restarts")

    assert pertama == 1, f"increment pertama harus mengembalikan 1, dapat {pertama!r}"
    assert kedua == 2, f"increment kedua harus mengembalikan 2, dapat {kedua!r}"


def _watchdog_worker(data_dir, times, out_file):
    """Proses anak: kembalikan nilai yang dilaporkan tiap increment."""
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import stats_tracker

    nilai = [stats_tracker.increment("watchdog_restarts") for _ in range(times)]
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(nilai, f)


def test_total_watchdog_kumulatif_lintas_proses(isolated_data_dir, tmp_path):
    """
    Tiga proses web_ui bergantian (restart) masing-masing mencatat 4 restart.
    Total seumur hidup harus 12 dan tiap increment harus melihat lanjutan dari
    proses sebelumnya -- bukan mulai dari 1 seperti counter in-process.
    """
    ctx = mp.get_context("spawn")
    out_files = []
    procs = []
    for i in range(3):
        out = tmp_path / f"worker_{i}.json"
        out_files.append(out)
        p = ctx.Process(target=_watchdog_worker, args=(isolated_data_dir, 4, str(out)))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(60)

    semua = []
    for out in out_files:
        assert out.exists(), f"worker tidak menulis hasil: {out}"
        semua.extend(json.loads(out.read_text(encoding="utf-8")))

    assert sorted(semua) == list(range(1, 13)), (
        f"total watchdog restart harus kumulatif 1..12 lintas proses, "
        f"dapat {sorted(semua)}"
    )

    import stats_tracker
    assert stats_tracker.get_stats().get("watchdog_restarts") == 12
