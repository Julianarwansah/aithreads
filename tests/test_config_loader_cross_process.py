"""
Bug CRITICAL #4, bagian terakhir: config_loader.

ui_settings.json / config.json dibaca oleh SEMUA proses (auto_commenter,
threads_poster, reply_watcher, job_hunter, web_ui) tapi ditulis dengan
open(...,"w") yang men-truncate lebih dulu. Pembaca yang membaca di jeda itu
dapat JSON parsial, load() menelan exception dan mengembalikan DEFAULTS.copy()
-- jadi seluruh konfigurasi milik user (persona, kuota, delay, blacklist) senyap
kembali ke default selama satu siklus.

save() juga read-modify-write di bawah threading.RLock, yang tidak berlaku
antar-proses, sehingga update dari dua penulis bisa saling menimpa.
"""
import json
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config_writer(data_dir, tag, count):
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import config_loader
    for i in range(count):
        config_loader.save({f"k_{tag}_{i}": i})


def _config_reader(data_dir, out_file, iterations):
    os.environ["PROFILE_DIR"] = data_dir
    sys.path.insert(0, PROJECT_ROOT)
    import config_loader

    anomalies = []
    for _ in range(iterations):
        try:
            cfg = config_loader.load()
        except Exception as exc:
            anomalies.append(f"EXCEPTION:{type(exc).__name__}")
            continue
        # load() selalu men-merge DEFAULTS, jadi satu-satunya tanda file korup yang
        # terlihat dari luar adalah persona yang kembali ke nilai default.
        if cfg.get("bot_persona") == config_loader.DEFAULTS["bot_persona"]:
            anomalies.append("persona-ter-reset")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(anomalies[:20], f)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "profile"
    data.mkdir()
    monkeypatch.setenv("PROFILE_DIR", str(data))
    return str(data)


def test_save_bersamaan_tidak_kehilangan_key(isolated_data_dir):
    """4 proses menulis key berbeda; semua harus bertahan."""
    procs, count = 4, 15
    ctx = mp.get_context("spawn")
    children = [
        ctx.Process(target=_config_writer, args=(isolated_data_dir, tag, count))
        for tag in range(procs)
    ]
    for c in children:
        c.start()
    for c in children:
        c.join(120)

    cfg = json.loads(
        open(os.path.join(isolated_data_dir, "config.json"), encoding="utf-8").read()
    )
    missing = [
        f"k_{tag}_{i}" for tag in range(procs) for i in range(count) if f"k_{tag}_{i}" not in cfg
    ]
    assert not missing, (
        f"{len(missing)} key config hilang karena save() saling menimpa, contoh: {missing[:8]}"
    )


def test_pembaca_tidak_pernah_melihat_config_ter_reset(isolated_data_dir):
    """Selama ada yang menulis, pembaca tidak boleh melihat persona kembali ke default."""
    ctx = mp.get_context("spawn")
    cfg_path = os.path.join(isolated_data_dir, "config.json")

    # Tanam persona non-default sebagai penanda. Ditulis langsung ke file, BUKAN
    # lewat config_loader.save() di proses induk: SETTINGS_FILE adalah konstanta
    # modul yang dihitung sekali saat impor, jadi kalau test lain sudah lebih
    # dulu mengimpor config_loader dengan PROFILE_DIR berbeda, save() di sini
    # akan menulis ke direktori yang salah dan penandanya tidak pernah ada.
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"bot_persona": "persona-khusus-milik-user"}, f)

    out_file = os.path.join(isolated_data_dir, "reader.json")
    reader = ctx.Process(target=_config_reader, args=(isolated_data_dir, out_file, 3000))
    writer = ctx.Process(target=_config_writer, args=(isolated_data_dir, 99, 60))

    reader.start()
    writer.start()
    writer.join(120)
    reader.join(60)

    anomalies = json.loads(open(out_file, encoding="utf-8").read())
    assert anomalies == [], f"pembaca melihat config rusak/ter-reset: {anomalies}"

    final = json.loads(open(cfg_path, encoding="utf-8").read())
    assert final["bot_persona"] == "persona-khusus-milik-user", (
        "persona user hilang tertimpa DEFAULTS"
    )
