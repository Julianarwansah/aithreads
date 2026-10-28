"""
llm_router.py -- Backend LLM lewat Qoder CLI (mode headless).

Tidak butuh API key terpisah: memakai akun Qoder yang sedang login di mesin ini.
"""
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

QODER_BIN   = os.getenv("QODER_BIN", "").strip() or shutil.which("qoder") or "qoder"
QODER_MODEL = os.getenv("QODER_MODEL", "").strip()

# Baca timeout dari env saat module load; bisa di-override lewat QODER_TIMEOUT di .env
QODER_TIMEOUT = int(os.getenv("QODER_TIMEOUT", "180"))

# PENTING: jangan masukkan flag dengan nilai empty string ("") — Qoder CLI akan hang.
_BASE_ARGS = [
    "-p",
    "--no-session-persistence",
]


def _get_max_tokens() -> str:
    """Baca max_tokens secara dinamis dari env atau config (mendukung live update dari UI)."""
    env_val = os.getenv("QODER_MAX_TOKENS", "").strip()
    if env_val:
        return env_val
    try:
        import config_loader
        return str(config_loader.get("max_tokens") or 120)
    except Exception:
        return "120"


def is_available() -> bool:
    return shutil.which(QODER_BIN) is not None or os.path.exists(QODER_BIN)


def ping() -> bool:
    """Cek apakah Qoder CLI merespons saat startup. Gunakan arg text-only yang sama dengan generate()."""
    try:
        args = [
            QODER_BIN, "-p", "--no-session-persistence",
            "--max-output-tokens", "64", "--tools", "", "--", "hi",
        ]
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=_PROJECT_DIR
        )
        try:
            out, _ = proc.communicate(timeout=QODER_TIMEOUT)
            return bool(out.strip())
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False
    except Exception:
        return False


def generate(prompt: str, system_prompt: str = "") -> str:
    """Kirim prompt ke Qoder CLI dan kembalikan teks balasannya."""
    timeout = int(os.getenv("QODER_TIMEOUT", str(QODER_TIMEOUT)))

    args = [QODER_BIN, *_BASE_ARGS, "--max-output-tokens", _get_max_tokens()]
    if QODER_MODEL:
        args += ["-m", QODER_MODEL]
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    args += ["--", prompt]

    # Buang arg kosong — empty string menyebabkan Qoder CLI hang tanpa output
    args = [a for a in args if a != ""]

    # --tools bersifat variadic: "" mematikan semua tool supaya Qoder cuma
    # menghasilkan teks. Harus tepat sebelum "--" agar daftar tool berhenti
    # di separator; menaruhnya di awal membuat flag sesudahnya ikut tertelan.
    sep = args.index("--")
    args[sep:sep] = ["--tools", ""]

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_PROJECT_DIR,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise Exception(f"Qoder CLI tidak merespons dalam {timeout}s")
    except FileNotFoundError:
        raise Exception(f"Qoder CLI tidak ditemukan di '{QODER_BIN}'. Set QODER_BIN di .env")

    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()[:300]
        raise Exception(f"Qoder CLI exit code {proc.returncode}: {detail}")

    reply = stdout.strip()
    if not reply:
        raise Exception("Qoder CLI mengembalikan balasan kosong")

    return reply
