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

QODER_BIN = os.getenv("QODER_BIN", "").strip() or shutil.which("qoder") or "qoder"
QODER_MODEL = os.getenv("QODER_MODEL", "").strip()
QODER_TIMEOUT = int(os.getenv("QODER_TIMEOUT", "180"))
QODER_MAX_TOKENS = os.getenv("QODER_MAX_TOKENS", "120").strip()

# --tools "" mematikan semua tool supaya Qoder cuma menghasilkan teks,
# tidak menyentuh filesystem atau menjalankan perintah apapun.
_BASE_ARGS = [
    "-p",
    "--tools", "",
    "--no-session-persistence",
    "--setting-sources", "",
    "--max-output-tokens", QODER_MAX_TOKENS,
]


def is_available() -> bool:
    return shutil.which(QODER_BIN) is not None or os.path.exists(QODER_BIN)


def generate(prompt: str, system_prompt: str = "") -> str:
    """Kirim prompt ke Qoder CLI dan kembalikan teks balasannya.

    Jika system_prompt diberikan, dikirim via --system-prompt supaya Qoder
    memisahkan konteks/aturan dari pesan pengguna — mencegah format markdown
    multi-bagian di output.
    """
    args = [QODER_BIN, *_BASE_ARGS]
    if QODER_MODEL:
        args += ["-m", QODER_MODEL]
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    args += ["--", prompt]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=QODER_TIMEOUT,
            cwd=_PROJECT_DIR,
        )
    except subprocess.TimeoutExpired:
        raise Exception(f"Qoder CLI tidak merespons dalam {QODER_TIMEOUT}s")
    except FileNotFoundError:
        raise Exception(f"Qoder CLI tidak ditemukan di '{QODER_BIN}'. Set QODER_BIN di .env")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise Exception(f"Qoder CLI exit code {result.returncode}: {detail}")

    reply = result.stdout.strip()
    if not reply:
        raise Exception("Qoder CLI mengembalikan balasan kosong")

    return reply
