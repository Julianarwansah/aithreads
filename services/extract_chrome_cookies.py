"""
extract_chrome_cookies.py -- Ambil session cookies Threads dari Chrome, update .env

Jalankan manual:
    python services/extract_chrome_cookies.py

Atau dipanggil dari kode lain:
    from services.extract_chrome_cookies import extract_and_update
    success = extract_and_update()
"""

import os
import sys
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).parent.parent  # project root

COOKIES_PATH = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Cookies"
)
TARGET_DOMAIN   = ".threads.com"
FALLBACK_DOMAIN = ".instagram.com"
EXPECTED_UID    = os.getenv("THREADS_DS_USER_ID", "")

TARGET_KEYS = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did")

ENV_FILE = _DIR / ".env"

# Mapping: env variable name → cookie key in browser
ENV_KEY_MAP = {
    "THREADS_SESSION_ID": "sessionid",
    "THREADS_CSRF_TOKEN": "csrftoken",
    "THREADS_DS_USER_ID": "ds_user_id",
    "THREADS_MID":        "mid",
    "THREADS_IG_DID":     "ig_did",
}


def _extract_cookies() -> dict:
    try:
        import browser_cookie3
    except ImportError:
        raise RuntimeError("browser-cookie3 tidak terinstall — jalankan: pip install browser-cookie3")

    if not os.path.exists(COOKIES_PATH):
        raise RuntimeError(f"Chrome Cookies tidak ditemukan: {COOKIES_PATH}")

    # Coba threads.com dulu, fallback ke instagram.com
    for domain in (TARGET_DOMAIN, FALLBACK_DOMAIN):
        cj     = browser_cookie3.chrome(domain_name=domain, cookie_file=COOKIES_PATH)
        result = {c.name: c.value for c in cj if c.name in TARGET_KEYS}
        uid    = result.get("ds_user_id", "")

        # Kalau ada EXPECTED_UID, pastikan cocok
        if EXPECTED_UID and uid and uid != EXPECTED_UID:
            continue

        if result.get("sessionid") and uid:
            result["_domain"] = domain
            return result

    raise RuntimeError(
        "Tidak menemukan sessionid Threads di Chrome. "
        "Pastikan sudah login di Chrome Default profile."
    )


def _update_env(cookies: dict) -> bool:
    """
    Update .env dengan cookies baru.
    Strategi: baca line-by-line, update baris yang cocok key-nya.
    Kunci baru ditambahkan setelah blok '--- Session cookie ---'.
    Return True kalau berhasil.
    """
    if not ENV_FILE.exists():
        print(f"[COOKIE_EXTRACT] .env tidak ditemukan di {ENV_FILE}")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)

    # Build lookup: env_key → cookie value yang harus diset
    updates = {}
    for env_key, cookie_key in ENV_KEY_MAP.items():
        val = cookies.get(cookie_key)
        if val:
            updates[env_key] = val

    updated_keys = set()
    new_lines = []

    for line in lines:
        # Update timestamp komentar
        if "# Diambil otomatis dari Chrome pada" in line:
            new_lines.append(f"# Diambil otomatis dari Chrome pada {today}.\n")
            continue

        stripped = line.rstrip("\n\r")
        if "=" in stripped and not stripped.lstrip().startswith("#"):
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
                continue

        new_lines.append(line)

    # Tambahkan kunci yang belum ada di file
    missing = set(updates.keys()) - updated_keys
    if missing:
        # Cari posisi setelah blok "--- Session cookie ---"
        insert_after = -1
        for i, line in enumerate(new_lines):
            if "Session cookie" in line:
                insert_after = i
                break

        missing_lines = [f"{k}={updates[k]}\n" for k in sorted(missing)]

        if insert_after >= 0:
            new_lines = new_lines[: insert_after + 1] + missing_lines + new_lines[insert_after + 1:]
        else:
            new_lines.extend(missing_lines)

    ENV_FILE.write_text("".join(new_lines), encoding="utf-8")
    return True


def extract_and_update(verbose: bool = True) -> bool:
    """
    Ekstrak cookies Threads dari Chrome lalu update .env.
    Return True kalau berhasil.
    """
    try:
        cookies = _extract_cookies()
        domain  = cookies.pop("_domain", "?")
        uid     = cookies.get("ds_user_id", "?")

        if verbose:
            print(f"[COOKIE_EXTRACT] Ditemukan di {domain}: ds_user_id={uid}")
            print(f"[COOKIE_EXTRACT] sessionid prefix: {cookies.get('sessionid','?')[:20]}…")

        ok = _update_env(cookies)
        if ok and verbose:
            print(f"[COOKIE_EXTRACT] .env diperbarui — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return ok

    except RuntimeError as e:
        if verbose:
            print(f"[COOKIE_EXTRACT] ERROR: {e}")
        return False
    except Exception as e:
        if verbose:
            import traceback
            traceback.print_exc()
            print(f"[COOKIE_EXTRACT] Unexpected error: {e}")
        return False


if __name__ == "__main__":
    success = extract_and_update(verbose=True)
    sys.exit(0 if success else 1)
