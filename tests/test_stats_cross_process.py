"""
Test untuk bug CRITICAL #4: race condition antar-proses pada file JSON state.

ThreadScope menjalankan 4+ proses terpisah (auto_commenter, threads_poster,
reply_watcher, web_ui) yang membaca-modifikasi-menulis file JSON yang sama.
Lock yang dipakai semua modul adalah threading.Lock, yang hanya berlaku di
dalam satu proses -- jadi tidak ada proteksi sama sekali antar-proses.

Dua akibat yang diuji di sini:
  1. Lost update  -- read-modify-write saling menimpa, counter jauh di bawah
                     jumlah sebenarnya (repro manual: 200 diharapkan, 39 tercatat).
  2. Reset senyap -- _save_stats() memakai open(...,"w") yang men-truncate file
                     sebelum menulis. Proses lain yang membaca di jeda itu dapat
                     file kosong/parsial, json.load gagal, dan _load_stats()
                     mengembalikan DEFAULT_STATS sehingga seluruh counter jadi 0.
"""
import json
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _increment_worker(data_dir, key, times):
    """Dijalankan di proses anak: import segar supaya STATS_FILE dihitung dari env."""
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import stats_tracker
    for _ in range(times):
        stats_tracker.increment(key)


def _reader_worker(data_dir, out_file, stop_flag, iterations):
    """Baca stats.json berulang; catat kalau pernah melihat counter yang sudah naik jadi 0."""
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import stats_tracker

    highest_seen = 0
    regressions = []
    for _ in range(iterations):
        if stop_flag.value:
            break
        try:
            stats = stats_tracker.get_stats()
        except Exception as exc:                      # file korup / JSON parsial
            regressions.append(f"EXCEPTION:{type(exc).__name__}")
            continue
        value = stats.get("comments_sent", 0)
        if value < highest_seen:
            regressions.append(f"{highest_seen}->{value}")
        highest_seen = max(highest_seen, value)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"highest": highest_seen, "regressions": regressions}, f)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """PROFILE_DIR diarahkan ke tmp supaya stats.json asli tidak tersentuh."""
    data = tmp_path / "profile"
    data.mkdir()
    monkeypatch.setenv("PROFILE_DIR", str(data))
    return str(data)


def test_increment_lintas_proses_tidak_kehilangan_update(isolated_data_dir):
    """5 proses x 40 increment harus menghasilkan tepat 200, bukan kurang."""
    procs, times = 5, 40
    ctx = mp.get_context("spawn")
    children = [
        ctx.Process(target=_increment_worker, args=(isolated_data_dir, "comments_sent", times))
        for _ in range(procs)
    ]
    for c in children:
        c.start()
    for c in children:
        c.join(120)

    stats = json.loads(
        open(os.path.join(isolated_data_dir, "stats.json"), encoding="utf-8").read()
    )
    assert stats["comments_sent"] == procs * times, (
        f"Lost update: diharapkan {procs * times}, tercatat {stats['comments_sent']} "
        f"({procs * times - stats['comments_sent']} update hilang)"
    )


def test_pembaca_tidak_pernah_melihat_counter_mundur_atau_file_kosong(isolated_data_dir):
    """Selama ada proses menulis, pembaca tidak boleh melihat nilai turun atau error."""
    ctx = mp.get_context("spawn")
    stop_flag = ctx.Value("b", False)
    out_file = os.path.join(isolated_data_dir, "reader_result.json")

    writer = ctx.Process(target=_increment_worker, args=(isolated_data_dir, "comments_sent", 120))
    reader = ctx.Process(
        target=_reader_worker, args=(isolated_data_dir, out_file, stop_flag, 4000)
    )
    writer.start()
    reader.start()
    writer.join(120)
    stop_flag.value = True
    reader.join(120)

    result = json.loads(open(out_file, encoding="utf-8").read())
    assert result["highest"] > 0, "pembaca tidak pernah melihat satu pun update"
    assert result["regressions"] == [], (
        f"Pembaca melihat counter mundur / file korup: {result['regressions'][:10]}"
    )


def test_file_tetap_json_valid_setelah_banyak_penulisan(isolated_data_dir):
    """Hasil akhir harus selalu JSON utuh, bukan terpotong setengah tulis."""
    ctx = mp.get_context("spawn")
    children = [
        ctx.Process(target=_increment_worker, args=(isolated_data_dir, "posts_created", 25))
        for _ in range(4)
    ]
    for c in children:
        c.start()
    for c in children:
        c.join(120)

    raw = open(os.path.join(isolated_data_dir, "stats.json"), encoding="utf-8").read()
    data = json.loads(raw)          # melempar kalau file terpotong
    assert data["posts_created"] == 100
    assert set(data) >= {"comments_sent", "posts_created", "llm_errors"}, (
        "file kehilangan key DEFAULT_STATS -- indikasi pernah di-reset ke default"
    )
