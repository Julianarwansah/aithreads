"""
Test untuk bug CRITICAL #1: path traversal di account_manager.

Sebelum diperbaiki, nama akun diambil mentah dari URL (/api/accounts/<name>/delete)
lalu dipakai di os.path.join(ACCOUNTS_DIR, name). Karena os.path.join tidak
menormalkan "..", delete_account("..") menjalankan shutil.rmtree() pada direktori
induk accounts/ -- yaitu root project.

Kontrak yang diinginkan: nama apa pun yang resolusi path-nya keluar dari
ACCOUNTS_DIR harus ditolak, dan tidak ada satu pun operasi filesystem yang jalan.
"""
import os

import pytest

import account_manager


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """ACCOUNTS_DIR指向 tmp, plus satu direktori 'korban' di luarnya."""
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    victim = tmp_path / "VICTIM"
    victim.mkdir()
    (victim / "important.txt").write_text("jangan hapus aku", encoding="utf-8")

    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR", str(accounts))
    return accounts, victim


@pytest.mark.parametrize("evil", ["..", "../..", ".", "../VICTIM", "foo/../../VICTIM"])
def test_delete_account_menolak_nama_yang_keluar_dari_accounts_dir(sandbox, evil):
    accounts, victim = sandbox

    result = account_manager.delete_account(evil)

    assert result.get("ok") is False, f"delete_account({evil!r}) seharusnya ditolak, got {result}"
    assert victim.is_dir(), f"delete_account({evil!r}) menghapus direktori di luar ACCOUNTS_DIR"
    assert (victim / "important.txt").exists()


def test_delete_account_tetap_berfungsi_untuk_nama_valid(sandbox):
    """Penjaga regresi: fix tidak boleh merusak jalur yang sah."""
    accounts, _ = sandbox
    (accounts / "budi").mkdir()
    (accounts / "budi" / ".env").write_text("THREADS_USERNAME=budi", encoding="utf-8")

    result = account_manager.delete_account("budi")

    assert result.get("ok") is True
    assert not (accounts / "budi").exists()


@pytest.mark.parametrize("evil", ["..", "../VICTIM"])
def test_get_account_env_menolak_traversal(sandbox, evil):
    result = account_manager.get_account_env(evil)

    assert result.get("ok") is False or "PROFILE_DIR" not in result
    if "PROFILE_DIR" in result:
        resolved = os.path.realpath(result["PROFILE_DIR"])
        assert resolved.startswith(os.path.realpath(str(sandbox[0]))), (
            f"get_account_env({evil!r}) membocorkan path di luar ACCOUNTS_DIR: {resolved}"
        )


def test_save_account_config_tidak_membuat_direktori_di_luar_accounts_dir(sandbox):
    """save_account_config memanggil os.makedirs(path) -- bisa membuat dir di luar."""
    accounts, victim = sandbox
    before = sorted(p.name for p in sandbox[0].parent.iterdir())

    account_manager.save_account_config("../ESCAPED", {"app_mode": "trend"})

    after = sorted(p.name for p in sandbox[0].parent.iterdir())
    assert after == before, f"save_account_config membuat direktori liar: {set(after) - set(before)}"
    assert not (victim.parent / "ESCAPED").exists()


def test_update_credentials_menolak_traversal(sandbox):
    _, victim = sandbox

    result = account_manager.update_credentials("..", {"THREADS_USERNAME": "hacked"})

    assert result.get("ok") is False
    assert not (victim / ".env").exists()
