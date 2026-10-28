"""
Test untuk persona_history -- jejak perubahan persona bot.

Masalah yang ditambal: refine_persona() di auto_commenter menimpa bot_persona
setiap 15 komentar berhasil tanpa menyimpan versi sebelumnya. Satu keluaran LLM
yang buruk berarti persona hasil tuning Anda hilang permanen, dan tidak ada cara
melihat kenapa karakter bot berubah.

Kontrak modul:
  - record() menambah entri {ts, persona, source, comments_sent} paling baru di akhir
  - riwayat dibatasi MAX_ENTRIES; entri TERLAMA yang dibuang
  - read() pada file hilang atau rusak mengembalikan [] (tidak melempar)
  - aman dipanggil dari beberapa proses (bot + web_ui menulis file yang sama)
"""
import json
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def hist_file(tmp_path):
    return str(tmp_path / "persona_history.json")


# ── record & read ─────────────────────────────────────────────────────────────

def test_record_pertama_membuat_satu_entri(hist_file):
    import persona_history

    hasil = persona_history.record(hist_file, "persona versi satu", "auto-refine")

    assert len(hasil) == 1
    entri = hasil[0]
    assert entri["persona"] == "persona versi satu"
    assert entri["source"] == "auto-refine"
    assert entri["ts"], "entri harus punya timestamp"


def test_record_menyimpan_comments_sent(hist_file):
    """Jumlah komentar saat perubahan berguna untuk menilai apakah persona baru efektif."""
    import persona_history

    hasil = persona_history.record(hist_file, "p", "auto-refine", comments_sent=45)

    assert hasil[0]["comments_sent"] == 45


def test_record_berurutan_menambah_bukan_menimpa(hist_file):
    import persona_history

    persona_history.record(hist_file, "v1", "manual")
    persona_history.record(hist_file, "v2", "auto-refine")
    hasil = persona_history.record(hist_file, "v3", "rollback")

    assert [e["persona"] for e in hasil] == ["v1", "v2", "v3"]
    assert [e["source"] for e in hasil] == ["manual", "auto-refine", "rollback"]


def test_read_file_belum_ada_mengembalikan_list_kosong(hist_file):
    import persona_history

    assert persona_history.read(hist_file) == []


def test_read_file_korup_mengembalikan_list_kosong(hist_file):
    import persona_history

    with open(hist_file, "w", encoding="utf-8") as f:
        f.write('[{"ts": "2026-10-28 10:00:00", "persona": ')

    assert persona_history.read(hist_file) == []


def test_read_isi_bukan_list_dinormalkan(hist_file):
    """File tertimpa objek/dict tidak boleh membuat dashboard crash."""
    import persona_history

    with open(hist_file, "w", encoding="utf-8") as f:
        json.dump({"persona": "bukan list"}, f)

    assert persona_history.read(hist_file) == []


# ── batas riwayat ─────────────────────────────────────────────────────────────

def test_riwayat_dibatasi_dan_terlama_dibuang(hist_file):
    import persona_history

    for i in range(persona_history.MAX_ENTRIES + 5):
        persona_history.record(hist_file, f"v{i}", "manual")

    hasil = persona_history.read(hist_file)
    assert len(hasil) == persona_history.MAX_ENTRIES
    assert hasil[0]["persona"] == "v5", "entri terlama harus yang dibuang"
    assert hasil[-1]["persona"] == f"v{persona_history.MAX_ENTRIES + 4}"


def test_persona_sangat_panjang_dipotong(hist_file):
    import persona_history

    persona_history.record(hist_file, "x" * 5000, "manual")
    tersimpan = persona_history.read(hist_file)[0]["persona"]

    assert tersimpan, "persona harus tersimpan"
    assert len(tersimpan) <= 1000, f"riwayat membengkak: {len(tersimpan)} karakter"


# ── aman lintas proses ────────────────────────────────────────────────────────

def _worker(path, n, out_file):
    sys.path.insert(0, PROJECT_ROOT)
    import persona_history

    for i in range(n):
        persona_history.record(path, f"w{os.getpid()}-{i}", "manual")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"ok": True}, f)


def test_record_aman_lintas_proses(hist_file, tmp_path):
    """
    Bot (auto-refine tiap 15 komentar) dan web_ui (suntingan manual + rollback)
    menulis file yang sama dari proses berbeda. Entri tidak boleh hilang.
    """
    ctx = mp.get_context("spawn")
    procs, outs = [], []
    for i in range(4):
        out = tmp_path / f"ph_w{i}.json"
        outs.append(out)
        p = ctx.Process(target=_worker, args=(hist_file, 5, str(out)))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(60)
    for out in outs:
        assert out.exists(), "worker gagal menulis hasil"

    import persona_history
    hasil = persona_history.read(hist_file)

    # 4 proses x 5 record = 20, tepat MAX_ENTRIES -> semuanya harus bertahan
    assert len(hasil) == 20, f"harus 20 entri tanpa lost update, dapat {len(hasil)}"
    sumber = {e["persona"] for e in hasil}
    assert len(sumber) == 20, "ada entri yang hilang atau tertimpa"
