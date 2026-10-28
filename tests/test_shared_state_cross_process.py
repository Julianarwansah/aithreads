"""
Test bug CRITICAL #4 untuk dua file state lain yang ditulis banyak proses.

reply_cache.json  -> diimpor 4 proses (auto_commenter, reply_watcher,
                     threads_poster, web_ui). Entri hilang berarti dedup gagal dan
                     bot bisa mengirim komentar yang sama dua kali.
comment_memory.json -> diimpor 5 proses. Entri hilang berarti filter topik/gaya
                     kehilangan riwayat, jadi komentar jadi repetitif.

Keduanya memakai pola _load() -> mutasi -> _save() yang dilindungi threading.Lock,
yaitu tanpa efek antar-proses, dan _save() menulis langsung (truncate lalu tulis)
sehingga pembaca bisa melihat file kosong.
"""
import json
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reply_cache_worker(data_dir, count, tag):
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import reply_cache
    for i in range(count):
        reply_cache.save_to_cache(
            f"caption-{tag}-{i}", f"balasan-{tag}-{i}", post_id=f"post-{tag}-{i}"
        )


def _comment_memory_worker(data_dir, count, tag):
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import comment_memory
    for i in range(count):
        comment_memory.save(f"caption-{tag}-{i}", f"balasan-{tag}-{i}")


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "profile"
    data.mkdir()
    monkeypatch.setenv("PROFILE_DIR", str(data))
    return str(data)


def _run_parallel(worker, data_dir, count, procs=4):
    ctx = mp.get_context("spawn")
    children = [
        ctx.Process(target=worker, args=(data_dir, count, tag)) for tag in range(procs)
    ]
    for c in children:
        c.start()
    for c in children:
        c.join(120)
    return procs * count


def test_reply_cache_tidak_kehilangan_entri_lintas_proses(isolated_data_dir):
    """4 proses x 20 caption berbeda = 80 entri cache, bukan kurang."""
    expected = _run_parallel(_reply_cache_worker, isolated_data_dir, 20)

    cache = json.loads(
        open(os.path.join(isolated_data_dir, "reply_cache.json"), encoding="utf-8").read()
    )
    assert len(cache) == expected, (
        f"{expected - len(cache)} entri reply_cache hilang -> dedup bolong, "
        f"bot berisiko mengirim komentar ganda (tercatat {len(cache)}/{expected})"
    )


def test_comment_memory_tidak_kehilangan_entri_lintas_proses(isolated_data_dir):
    """4 proses x 20 = 80 entri memory (di bawah MAX_ENTRIES=100 agar cap tidak ikut campur)."""
    expected = _run_parallel(_comment_memory_worker, isolated_data_dir, 20)

    memory = json.loads(
        open(os.path.join(isolated_data_dir, "comment_memory.json"), encoding="utf-8").read()
    )
    assert len(memory) == expected, (
        f"{expected - len(memory)} entri comment_memory hilang "
        f"(tercatat {len(memory)}/{expected})"
    )


def test_reply_cache_dan_comment_memory_selalu_json_valid(isolated_data_dir):
    """Hasil akhir keduanya harus JSON utuh, tidak pernah terpotong setengah tulis."""
    _run_parallel(_reply_cache_worker, isolated_data_dir, 15)
    _run_parallel(_comment_memory_worker, isolated_data_dir, 15)

    cache = json.loads(
        open(os.path.join(isolated_data_dir, "reply_cache.json"), encoding="utf-8").read()
    )
    memory = json.loads(
        open(os.path.join(isolated_data_dir, "comment_memory.json"), encoding="utf-8").read()
    )
    assert isinstance(cache, dict) and len(cache) == 60
    assert isinstance(memory, list) and len(memory) == 60
