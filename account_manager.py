"""
account_manager.py -- Kelola beberapa akun Threads dalam profil terpisah.

Setiap akun disimpan di accounts/<name>/ berisi:
  .env        -- credentials (THREADS_SESSION_ID, THREADS_USERNAME, dst.)
  config.json -- pengaturan bot (delay, persona, blacklist, dst.)
  *.json      -- data runtime (reply_cache, comment_memory, stats, dst.)
  *.pid / *.log -- proses
"""
import json
import os
import shutil
import threading
from pathlib import Path

_DIR         = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_DIR = os.path.join(_DIR, "accounts")
_lock        = threading.Lock()


def _dotenv_values(path: str) -> dict:
    result = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return result


def _ensure_dir():
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)


def account_path(name):
    """
    Resolusi nama akun menjadi path di dalam ACCOUNTS_DIR.

    Mengembalikan None kalau hasil resolusinya keluar dari ACCOUNTS_DIR. Nama akun
    datang dari URL (/api/accounts/<name>/...) dan os.path.join tidak menormalkan
    "..", jadi tanpa penjaga ini delete_account("..") akan shutil.rmtree() root
    project. realpath dipakai supaya symlink tidak bisa dipakai memotong keluar.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    root = os.path.realpath(ACCOUNTS_DIR)
    candidate = os.path.realpath(os.path.join(root, name))
    if candidate == root or not candidate.startswith(root + os.sep):
        return None
    return candidate


def list_accounts() -> list:
    """Kembalikan list dict {name, username, path} semua akun."""
    _ensure_dir()
    accounts = []
    for name in sorted(os.listdir(ACCOUNTS_DIR)):
        path = os.path.join(ACCOUNTS_DIR, name)
        if not os.path.isdir(path):
            continue
        creds = _dotenv_values(os.path.join(path, ".env"))
        accounts.append({
            "name":     name,
            "username": creds.get("THREADS_USERNAME", name),
            "path":     path,
        })
    return accounts


def create_account(name: str, creds: dict) -> dict:
    """
    Buat profil akun baru.
    creds: {THREADS_SESSION_ID, THREADS_CSRF_TOKEN, THREADS_DS_USER_ID,
            THREADS_USERNAME, THREADS_MID, THREADS_IG_DID}
    """
    name = (name.strip().lower()
            .replace(" ", "_").replace("/", "_")
            .replace(".", "_").replace("@", ""))
    if not name:
        return {"ok": False, "msg": "Nama akun tidak boleh kosong"}
    _ensure_dir()
    path = os.path.join(ACCOUNTS_DIR, name)
    if os.path.isdir(path):
        return {"ok": False, "msg": f"Akun '{name}' sudah ada"}
    with _lock:
        os.makedirs(path, exist_ok=True)
        env_lines = [f"{k}={v}" for k, v in creds.items() if v]
        Path(os.path.join(path, ".env")).write_text(
            "\n".join(env_lines), encoding="utf-8"
        )
    return {"ok": True, "path": path, "name": name}


def update_credentials(name: str, creds: dict) -> dict:
    """Update .env credentials akun yang sudah ada."""
    path = account_path(name)
    if path is None:
        return {"ok": False, "msg": "Nama akun tidak valid"}
    if not os.path.isdir(path):
        return {"ok": False, "msg": f"Akun '{name}' tidak ditemukan"}
    with _lock:
        existing = _dotenv_values(os.path.join(path, ".env"))
        existing.update({k: v for k, v in creds.items() if v})
        env_lines = [f"{k}={v}" for k, v in existing.items()]
        Path(os.path.join(path, ".env")).write_text(
            "\n".join(env_lines), encoding="utf-8"
        )
    return {"ok": True}


def delete_account(name: str) -> dict:
    """Hapus profil akun beserta semua datanya."""
    path = account_path(name)
    if path is None:
        return {"ok": False, "msg": "Nama akun tidak valid"}
    if not os.path.isdir(path):
        return {"ok": False, "msg": f"Akun '{name}' tidak ditemukan"}
    with _lock:
        shutil.rmtree(path)
    return {"ok": True}


def get_account_env(name: str) -> dict:
    """
    Kembalikan dict env untuk subprocess akun ini.
    Berisi semua credentials dari .env + PROFILE_DIR.
    """
    path = account_path(name)
    if path is None:
        return {}
    creds = _dotenv_values(os.path.join(path, ".env"))
    return {**creds, "PROFILE_DIR": path}


def get_account_config(name: str) -> dict:
    """Baca config.json akun, fallback ke DEFAULTS config_loader."""
    import config_loader
    path = account_path(name)
    if path is None:
        return config_loader.DEFAULTS.copy()
    cfg_file = os.path.join(path, "config.json")
    try:
        if os.path.exists(cfg_file):
            with open(cfg_file, encoding="utf-8") as f:
                data = json.load(f)
            merged = config_loader.DEFAULTS.copy()
            merged.update(data)
            return merged
    except Exception:
        pass
    return config_loader.DEFAULTS.copy()


def save_account_config(name: str, partial: dict) -> bool:
    """Merge partial dict ke config.json akun."""
    path = account_path(name)
    if path is None:
        return False
    os.makedirs(path, exist_ok=True)
    cfg_file = os.path.join(path, "config.json")
    with _lock:
        current = get_account_config(name)
        current.update(partial)
        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False


def get_account_stats(name: str) -> dict:
    """Baca stats.json akun."""
    path = account_path(name)
    if path is None:
        return {}
    stats_file = os.path.join(path, "stats.json")
    try:
        if os.path.exists(stats_file):
            with open(stats_file, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}
