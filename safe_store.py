"""
safe_store.py -- baca/tulis file JSON state yang aman untuk banyak proses.

ThreadScope menjalankan beberapa proses sekaligus (auto_commenter, threads_poster,
reply_watcher, web_ui) yang berbagi file state yang sama: stats.json,
reply_cache.json, comment_memory.json, growth_stats.json. threading.Lock hanya
berlaku di dalam satu proses, jadi sebelumnya read-modify-write antar-proses
saling menimpa (terukur: 161 dari 200 increment hilang).

Dua jaminan yang diberikan modul ini:
  1. update_json() memegang flock eksklusif, jadi hanya satu proses yang boleh
     read-modify-write pada satu waktu -> tidak ada lost update.
  2. write_json() menulis ke file sementara lalu os.replace(), yang atomik di
     POSIX. Pembaca tidak pernah melihat file kosong atau setengah tertulis --
     inilah yang dulu membuat json.load gagal dan seluruh counter di-reset ke
     DEFAULT_STATS secara senyap.

Catatan: flock bekerja per open-file-description, jadi dua thread dalam satu
proses pun ikut terserialisasi. Tidak perlu lock tambahan.
"""
import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def file_lock(path):
    """Lock eksklusif antar-proses untuk `path`, memakai sidecar `<path>.lock`."""
    lock_path = f"{path}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_json(path, default=None):
    """
    Baca JSON tanpa memegang lock.

    Aman dipanggil kapan saja karena penulis memakai os.replace() (atomik), jadi
    isi file selalu salah satu versi lengkap -- tidak pernah parsial.
    """
    fallback = default.copy() if isinstance(default, dict) else default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError:
        # Hampir tidak mungkin terjadi lagi sekarang, tapi jangan di-reset senyap.
        logger.warning("JSON rusak di %s -- pakai default. File tidak ditimpa.", path)
        return fallback
    except OSError as exc:
        logger.warning("Gagal baca %s (%s) -- pakai default.", path, exc)
        return fallback


def write_json(path, data):
    """Tulis atomik: file sementara di direktori yang sama, lalu os.replace()."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_json(path, mutate, default=None):
    """
    Read-modify-write di bawah lock eksklusif.

    `mutate(data)` menerima data hasil baca dan mengembalikan data yang mau
    disimpan; kalau mengembalikan None, data yang sudah dimutasi in-place dipakai.
    Mengembalikan data final yang tersimpan.
    """
    with file_lock(path):
        data = read_json(path, default)
        result = mutate(data)
        if result is not None:
            data = result
        write_json(path, data)
        return data
