"""
web_security.py -- penjaga untuk dashboard HTTP ThreadScope.

Dipisahkan dari web_ui.py supaya bisa diuji tanpa mengimpor modul yang
menyalakan thread watchdog saat import.

Tiga tanggung jawab:
  1. resolve_bind_host()   -- menentukan alamat bind. Default loopback; membuka ke
                              jaringan harus opt-in lewat UI_HOST *dan* wajib punya
                              UI_TOKEN, kalau tidak diturunkan paksa ke loopback.
  2. is_authorized()       -- memutuskan satu request boleh dilayani atau tidak.
  3. validate_executable() -- memvalidasi nilai yang akan dipakai sebagai argv[0]
                              subprocess (QODER_BIN) sebelum masuk ke os.environ.
"""
import hmac
import ipaddress
import os
import re
import shutil

DEFAULT_BIND_HOST = "127.0.0.1"

# Karakter yang tidak pernah ada di path binary asli tapi lazim dipakai menyuntik
# perintah. llm_router memanggil subprocess dengan argv (bukan shell), jadi
# metakarakter tidak diinterpretasi hari ini -- tetap ditolak supaya nilai yang
# dipersist ke ui_settings.json tidak berubah jadi payload kalau pemanggilannya
# suatu saat diubah. Backslash sengaja tidak dilarang karena path Windows
# (C:\...\qoder.exe) adalah nilai yang sah.
_FORBIDDEN_CHARS = re.compile(r"[;&|`$<>\n\r\"']")


def _token() -> str:
    return os.getenv("UI_TOKEN", "").strip()


def _is_loopback(host: str) -> bool:
    """True untuk 127.0.0.0/8, ::1, dan 'localhost'."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        # Buang zone id ("fe80::1%en0") dan prefix IPv4-mapped ("::ffff:127.0.0.1")
        cleaned = host.split("%")[0]
        addr = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def resolve_bind_host() -> str:
    """
    Alamat bind untuk dashboard.

    Default 127.0.0.1 supaya dashboard tidak terlihat dari WiFi. UI_HOST boleh
    meminta alamat non-loopback, tapi hanya dihormati kalau UI_TOKEN sudah di-set;
    tanpa token, permintaan itu diabaikan dan kembali ke loopback.
    """
    requested = os.getenv("UI_HOST", "").strip()
    if not requested:
        return DEFAULT_BIND_HOST
    if _is_loopback(requested):
        return requested
    if not _token():
        return DEFAULT_BIND_HOST
    return requested


def is_authorized(client_host: str, supplied_token) -> bool:
    """
    Bolehkah request ini dilayani?

    Kalau UI_TOKEN di-set, token wajib untuk semua klien termasuk loopback -- tidak
    ada jalur belakang. Kalau tidak di-set, hanya klien loopback yang diterima.
    """
    expected = _token()
    if expected:
        if not supplied_token:
            return False
        return hmac.compare_digest(str(supplied_token), expected)
    return _is_loopback(client_host or "")


def validate_executable(value):
    """
    Validasi nilai yang akan dipakai sebagai path binary (mis. QODER_BIN).

    Mengembalikan (ok, keterangan). Kalau ok, keterangan berisi path hasil resolusi;
    kalau tidak, berisi alasan penolakan.
    """
    if not isinstance(value, str) or not value.strip():
        return False, "nilai kosong"

    candidate = value.strip()
    match = _FORBIDDEN_CHARS.search(candidate)
    if match:
        return False, f"mengandung karakter terlarang {match.group(0)!r}"

    resolved = shutil.which(candidate)
    if not resolved:
        return False, f"tidak ditemukan atau tidak dapat dieksekusi: {candidate!r}"
    if not os.path.isfile(resolved):
        return False, f"bukan file biasa: {resolved!r}"
    if not os.access(resolved, os.X_OK):
        return False, f"tidak punya izin eksekusi: {resolved!r}"

    return True, resolved
