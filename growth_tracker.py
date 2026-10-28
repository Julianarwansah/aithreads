"""
growth_tracker.py -- Snapshot follower/following harian.
Dipanggil dari background thread web_ui.py, sekali per jam (cek tanggal berubah).
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_DIR         = os.path.dirname(os.path.abspath(__file__))
GROWTH_FILE  = os.path.join(_DIR, "growth_stats.json")
_lock        = threading.Lock()

USER_INFO_URL = "https://www.threads.com/api/v1/users/{user_id}/info/"
HEADERS = {
    "accept":          "application/json",
    "accept-language": "en-US,en;q=0.9",
    "origin":          "https://www.threads.com",
    "referer":         "https://www.threads.com/",
    "user-agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-ig-app-id":     "238260118697367",
}


def _load() -> list:
    try:
        if Path(GROWTH_FILE).exists():
            data = json.loads(Path(GROWTH_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save(data: list):
    Path(GROWTH_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_stats(session, user_id: str):
    """Ambil follower_count dan following_count dari Threads API."""
    try:
        url  = USER_INFO_URL.format(user_id=user_id)
        hdrs = HEADERS.copy()
        csrf = session.cookies.get("csrftoken")
        if csrf:
            hdrs["x-csrftoken"] = csrf
        resp = session.get(url, headers=hdrs, timeout=15)
        if resp.status_code == 200:
            user = resp.json().get("user", {})
            return {
                "followers":  user.get("follower_count",  0),
                "following":  user.get("following_count", 0),
                "posts":      user.get("media_count",     0),
                "username":   user.get("username",        ""),
            }
    except Exception as e:
        print(f"[GROWTH] Fetch error: {e}")
    return None


def record_snapshot(session, user_id: str):
    """Simpan snapshot hari ini (skip kalau sudah ada hari ini)."""
    stats = fetch_stats(session, user_id)
    if not stats:
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        data = _load()
        # Kalau sudah ada entry hari ini, update nilainya
        for entry in data:
            if entry.get("date") == today:
                entry.update(stats)
                _save(data)
                print(f"[GROWTH] Update hari ini: {stats['followers']} followers")
                return entry
        # Entry baru
        entry = {"date": today, **stats}
        data.append(entry)
        # Simpan max 90 hari
        if len(data) > 90:
            data = data[-90:]
        _save(data)
        print(f"[GROWTH] Snapshot baru: {stats['followers']} followers ({today})")
        return entry


def get_history(days: int = 30) -> list:
    """Kembalikan history N hari terakhir."""
    with _lock:
        data = _load()
    return data[-days:] if data else []


def get_latest() :
    with _lock:
        data = _load()
    return data[-1] if data else None


def get_delta(days: int = 7) -> dict:
    """Hitung pertumbuhan follower dalam N hari terakhir."""
    with _lock:
        data = _load()
    if len(data) < 2:
        return {"followers": 0, "following": 0, "days": days}
    recent = data[-days:] if len(data) >= days else data
    delta_f = recent[-1].get("followers", 0) - recent[0].get("followers", 0)
    delta_g = recent[-1].get("following", 0) - recent[0].get("following", 0)
    return {"followers": delta_f, "following": delta_g, "days": len(recent)}
