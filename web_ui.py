"""
web_ui.py -- ThreadScope Dashboard v3 (pure stdlib).
Jalankan: python3 web_ui.py  →  http://localhost:5000
"""
import csv, io, json, os, signal, subprocess, sys, time, threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
load_dotenv()

import config_loader
import growth_tracker
import account_manager
import daily_quota
import web_security

_DIR           = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT     = os.path.join(_DIR, "auto_commenter.py")
POSTER_SCRIPT  = os.path.join(_DIR, "threads_poster.py")
WATCHER_SCRIPT = os.path.join(_DIR, "reply_watcher.py")
BOT_LOG        = os.path.join(_DIR, "bot_output.log")
POSTER_LOG     = os.path.join(_DIR, "poster_output.log")
WATCHER_LOG    = os.path.join(_DIR, "watcher_output.log")
BOT_PID        = os.path.join(_DIR, "bot.pid")
POSTER_PID     = os.path.join(_DIR, "poster.pid")
WATCHER_PID    = os.path.join(_DIR, "watcher.pid")

JH_SCRIPT = os.path.join(_DIR, "job_hunter.py")
JH_LOG    = os.path.join(_DIR, "job_hunter.log")
JH_PID    = os.path.join(_DIR, "job_hunter.pid")

LI_COMMENTER_SCRIPT  = os.path.join(_DIR, "linkedin_commenter.py")
LI_POSTER_SCRIPT     = os.path.join(_DIR, "linkedin_poster.py")
LI_WATCHER_SCRIPT    = os.path.join(_DIR, "linkedin_reply_watcher.py")
LI_COMMENTER_LOG     = os.path.join(_DIR, "linkedin_bot.log")
LI_POSTER_LOG        = os.path.join(_DIR, "linkedin_poster.log")
LI_WATCHER_LOG       = os.path.join(_DIR, "linkedin_watcher.log")
LI_COMMENTER_PID     = os.path.join(_DIR, "linkedin_bot.pid")
LI_POSTER_PID        = os.path.join(_DIR, "linkedin_poster.pid")
LI_WATCHER_PID       = os.path.join(_DIR, "linkedin_watcher.pid")

PORT           = int(os.getenv("UI_PORT", "5000"))

# ── process helpers ──────────────────────────────────────────────────────────
def _read_pid(pidfile):
    try:
        pid = int(Path(pidfile).read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None

def _is_alive(pidfile):
    pid = _read_pid(pidfile)
    if pid is None:
        return False
    # os.kill(pid, 0) tetap sukses untuk zombie, jadi reap anak sendiri dulu
    # supaya bot yang sudah mati tidak terbaca "Running" dan menolak restart.
    try:
        rpid, _ = os.waitpid(pid, os.WNOHANG)
        if rpid == pid:
            Path(pidfile).unlink(missing_ok=True)
            return False
    except ChildProcessError:
        pass
    return True

_MAX_LOG_BYTES = 5 * 1024 * 1024

def _prepare_log(logfile, script):
    """
    Siapkan file log untuk proses baru: append, bukan truncate.

    Dulu file dikosongkan tiap start, jadi riwayat tepat sebelum crash hilang
    persis saat paling dibutuhkan untuk debugging. Ukuran tetap dibatasi supaya
    log tidak tumbuh tanpa batas.
    """
    try:
        if os.path.getsize(logfile) > _MAX_LOG_BYTES:
            open(logfile, "w").close()
    except OSError:
        pass
    try:
        with open(logfile, "ab") as f:
            f.write(
                f"\n{'=' * 60}\n"
                f"[START] {os.path.basename(script)} — {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"{'=' * 60}\n".encode()
            )
    except OSError:
        pass

def _launch(script, logfile, pidfile, extra_env=None):
    if _is_alive(pidfile):
        return {"ok": False, "msg": "Sudah berjalan"}
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    _prepare_log(logfile, script)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if extra_env:
        env.update(extra_env)
    with open(logfile, "ab") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-u", script],
            stdout=lf, stderr=subprocess.STDOUT, cwd=_DIR,
            env=env,
        )
    Path(pidfile).write_text(str(proc.pid))
    return {"ok": True, "pid": proc.pid}

def _terminate(pidfile):
    pid = _read_pid(pidfile)
    if not pid:
        return {"ok": False, "msg": "Tidak berjalan"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    Path(pidfile).unlink(missing_ok=True)
    # Tunggu proses benar-benar keluar sebelum melapor sukses. pidfile sudah
    # dihapus, jadi Start yang diklik 1-2 detik kemudian melihat "Tidak berjalan"
    # dan meluncurkan instance kedua yang paralel dengan proses lama yang masih
    # shutdown — dua bot bisa mengirim komentar ganda.
    for _ in range(50):
        try:
            rpid, _ = os.waitpid(pid, os.WNOHANG)
            if rpid == pid:
                return {"ok": True}
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"ok": True}
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return {"ok": True}

# ── watchdog thread ───────────────────────────────────────────────────────────

def _watchdog_loop():
    tick = 0
    while True:
        time.sleep(30)
        tick += 1
        # Heartbeat tiap 5 menit, bukan tiap tick. stats.json ikut ditulis oleh
        # daemon lain dari proses berbeda dan lock stats_tracker hanya berlaku
        # per-proses, jadi makin sering kita menulis makin besar peluang menimpa
        # increment (comments_sent dll) yang baru mereka catat.
        if tick % 10 == 0:
            try:
                import stats_tracker
                stats_tracker.update_heartbeat(time.time())
            except Exception:
                pass
        cfg = config_loader.load()
        if not cfg.get("watchdog_enabled", True):
            continue
        # Restart commenter if PID file exists but process died
        if Path(BOT_PID).exists() and not _is_alive(BOT_PID):
            Path(BOT_PID).unlink(missing_ok=True)
            _launch(BOT_SCRIPT, BOT_LOG, BOT_PID)
            import stats_tracker
            total = stats_tracker.increment("watchdog_restarts")
            with open(BOT_LOG, "a") as f:
                f.write(f"\n[WATCHDOG] Bot di-restart otomatis (total: {total}×)\n")
        # Restart poster if enabled and crashed
        if cfg.get("poster_enabled") and Path(POSTER_PID).exists() and not _is_alive(POSTER_PID):
            Path(POSTER_PID).unlink(missing_ok=True)
            _launch(POSTER_SCRIPT, POSTER_LOG, POSTER_PID)
        # Restart watcher only if it was previously started (PID file exists) but crashed
        if cfg.get("auto_reply_enabled") and Path(WATCHER_PID).exists() and not _is_alive(WATCHER_PID):
            Path(WATCHER_PID).unlink(missing_ok=True)
            _launch(WATCHER_SCRIPT, WATCHER_LOG, WATCHER_PID)

        # Restart proses per-akun yang crash
        for acct in account_manager.list_accounts():
            aname   = acct["name"]
            apath   = acct["path"]
            aenv    = account_manager.get_account_env(aname)
            acfg    = account_manager.get_account_config(aname)

            a_bot_pid     = os.path.join(apath, "bot.pid")
            a_poster_pid  = os.path.join(apath, "poster.pid")
            a_watcher_pid = os.path.join(apath, "watcher.pid")

            if Path(a_bot_pid).exists() and not _is_alive(a_bot_pid):
                Path(a_bot_pid).unlink(missing_ok=True)
                _launch(BOT_SCRIPT, os.path.join(apath, "bot.log"), a_bot_pid, extra_env=aenv)
                import stats_tracker
                stats_tracker.increment("watchdog_restarts")
            if acfg.get("poster_enabled") and Path(a_poster_pid).exists() and not _is_alive(a_poster_pid):
                Path(a_poster_pid).unlink(missing_ok=True)
                _launch(POSTER_SCRIPT, os.path.join(apath, "poster.log"), a_poster_pid, extra_env=aenv)
            if acfg.get("auto_reply_enabled") and Path(a_watcher_pid).exists() and not _is_alive(a_watcher_pid):
                Path(a_watcher_pid).unlink(missing_ok=True)
                _launch(WATCHER_SCRIPT, os.path.join(apath, "watcher.log"), a_watcher_pid, extra_env=aenv)

threading.Thread(target=_watchdog_loop, daemon=True).start()

# ── growth snapshot thread ────────────────────────────────────────────────────
_growth_session = None

def _growth_loop():
    """Ambil snapshot follower sekali per jam (hanya simpan 1 entry per hari)."""
    time.sleep(10)   # tunggu server siap
    while True:
        try:
            user_id  = os.getenv("THREADS_DS_USER_ID", "").strip()
            if user_id and _growth_session:
                growth_tracker.record_snapshot(_growth_session, user_id)
        except Exception as e:
            print(f"[GROWTH] Loop error: {e}")
        time.sleep(3600)   # cek setiap jam

def _init_growth_session():
    """Inisialisasi session untuk growth tracker (lazy, saat pertama dibutuhkan)."""
    global _growth_session
    if _growth_session:
        return
    try:
        from dotenv import load_dotenv; load_dotenv()
        from services.session_manager import SessionManager, SessionStatus
        sm = SessionManager()
        if sm.load_session() and sm.validate_session().status == SessionStatus.VALID:
            _growth_session = sm.inject_session()
    except Exception:
        pass

threading.Thread(target=lambda: (_init_growth_session(), _growth_loop()), daemon=True).start()

# ── data helpers ─────────────────────────────────────────────────────────────
def _get_stats():
    sp = os.path.join(_DIR, "stats.json")
    try:
        s = json.loads(Path(sp).read_text(encoding="utf-8")) if os.path.exists(sp) else {}
    except Exception:
        s = {}
    cfg = config_loader.load()
    s["app_mode"]        = cfg.get("app_mode", "normal")
    s["active_hours"]    = f"{cfg.get('active_start_hour',7):02d}:00–{cfg.get('active_end_hour',23):02d}:00"
    s["watchdog_restart"]= s.get("watchdog_restarts", 0)
    # Plafon komentar proaktif hari ini. Balasan tidak dihitung, jadi angka ini
    # memang lebih kecil dari comments_sent yang mencampur keduanya.
    quota = cfg.get("bot_daily_quota", 40)
    used  = daily_quota.read(os.path.join(_DIR, "bot_daily_count.json"))
    s["bot_quota_limit"]     = quota
    s["bot_quota_used"]      = used
    s["bot_quota_remaining"] = daily_quota.remaining(quota, used)
    return s

def _get_comments():
    cp = os.getenv("REPLY_CACHE_FILE", os.path.join(_DIR, "reply_cache.json"))
    try:
        cache = json.loads(Path(cp).read_text(encoding="utf-8")) if os.path.exists(cp) else {}
    except Exception:
        cache = {}
    # Enrich from comment_memory: engaged, style, topic_bucket
    mp = os.path.join(_DIR, "comment_memory.json")
    mem_map: dict = {}
    try:
        mem = json.loads(Path(mp).read_text(encoding="utf-8")) if os.path.exists(mp) else []
        for e in mem:
            key = e.get("reply", "")[:60]
            mem_map[key] = {
                "engaged":      bool(e.get("engaged")),
                "style":        e.get("style", ""),
                "topic_bucket": e.get("topic_bucket", ""),
            }
    except Exception:
        pass
    items = []
    for k, v in cache.items():
        reply = v.get("reply", "")
        info  = mem_map.get(reply[:60], {})
        items.append({
            "caption":    k,
            "reply":      reply,
            "ts":         v.get("timestamp", 0),
            "post_id":    v.get("post_id", ""),
            "comment_id": v.get("comment_id", ""),
            "engaged":    info.get("engaged", False),
            "style":      info.get("style", ""),
            "topic":      info.get("topic_bucket", ""),
        })
    return sorted(items, key=lambda x: x["ts"], reverse=True)

def _get_activity():
    """Kembalikan aktivitas komentar per jam untuk 24 jam terakhir."""
    items    = _get_comments()
    now      = time.time()
    buckets  = {}
    for i in range(23, -1, -1):
        t   = now - i * 3600
        key = datetime.fromtimestamp(t).strftime("%H:00")
        buckets[key] = 0
    for item in items:
        age = now - item.get("ts", 0)
        if 0 <= age < 86400:
            key = datetime.fromtimestamp(item["ts"]).strftime("%H:00")
            if key in buckets:
                buckets[key] += 1
    return [{"hour": k, "count": v} for k, v in buckets.items()]

def _get_trending_cache() -> dict:
    """Baca hasil scan trending terakhir dari threads_poster."""
    tc = os.path.join(_DIR, "trending_cache.json")
    try:
        if os.path.exists(tc):
            return json.loads(Path(tc).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ts": 0, "posts": []}


def _get_default_account_status() -> dict:
    """Status akun utama dari root .env — selalu muncul pertama di Multi-Akun."""
    import stats_tracker
    username = os.getenv("THREADS_USERNAME", "")
    stats    = {}
    try:
        stats = stats_tracker.get_stats()
    except Exception:
        pass
    cfg = config_loader.load()
    return {
        "name":               "__default__",
        "username":           username or "akun_utama",
        "is_default":         True,
        "bot_running":        _is_alive(BOT_PID),
        "poster_running":     _is_alive(POSTER_PID),
        "watcher_running":    _is_alive(WATCHER_PID),
        "last_comment":       stats.get("last_comment_time", ""),
        "comments_sent":      stats.get("comments_sent", 0),
        "poster_enabled":     cfg.get("poster_enabled", False),
        "auto_reply_enabled": cfg.get("auto_reply_enabled", False),
    }


def _get_account_full_status(name: str) -> dict:
    """Status lengkap satu akun: proses + stats + config."""
    apath       = os.path.join(_DIR, "accounts", name)
    bot_pid     = os.path.join(apath, "bot.pid")
    poster_pid  = os.path.join(apath, "poster.pid")
    watcher_pid = os.path.join(apath, "watcher.pid")
    stats       = account_manager.get_account_stats(name)
    cfg         = account_manager.get_account_config(name)
    creds       = account_manager._dotenv_values(os.path.join(apath, ".env"))
    return {
        "name":              name,
        "username":          creds.get("THREADS_USERNAME", name),
        "bot_running":       _is_alive(bot_pid),
        "poster_running":    _is_alive(poster_pid),
        "watcher_running":   _is_alive(watcher_pid),
        "last_comment":      stats.get("last_comment_time", ""),
        "comments_sent":     stats.get("comments_sent", 0),
        "poster_enabled":    cfg.get("poster_enabled", False),
        "auto_reply_enabled":cfg.get("auto_reply_enabled", False),
    }


def _get_daily_comments(days: int = 7) -> list:
    """Hitung komentar yang dikirim per hari selama N hari terakhir."""
    import reply_cache as rc
    from collections import defaultdict
    cached      = rc.get_recent_post_ids(max_age_hours=24 * days)
    by_date: dict = defaultdict(int)
    for e in cached:
        if e.get("ts"):
            d = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")
            by_date[d] += 1
    import datetime as _dt
    today   = _dt.date.today()
    result  = []
    for i in range(days - 1, -1, -1):
        day = (today - _dt.timedelta(days=i)).isoformat()
        result.append({"date": day, "count": by_date.get(day, 0)})
    return result


def _get_watcher_stats() -> dict:
    """Baca statistik Reply Watcher dari replied_cache.json + watcher_output.log."""
    import reply_cache as rc
    recent    = rc.get_recent_post_ids(max_age_hours=24)
    trackable = [e for e in recent if e.get("comment_id")]

    # replied_cache: {reply_id: {ts, text}}
    rp = os.path.join(_DIR, "replied_cache.json")
    replied: dict = {}
    try:
        replied = json.loads(Path(rp).read_text(encoding="utf-8")) if os.path.exists(rp) else {}
    except Exception:
        pass

    replies_received = 0
    auto_blacklisted = 0
    last_cycle = ""
    try:
        with open(WATCHER_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[WATCHER]" in line and "balas:" in line and "@" in line:
                    replies_received += 1
                if "blacklist & skip" in line or "auto-blacklist" in line.lower():
                    auto_blacklisted += 1
                if "Tidur" in line:
                    last_cycle = line.strip()
    except Exception:
        pass

    replied_list = sorted(
        [{"reply_id": rid, "text": v.get("text", ""), "ts": v.get("ts", 0)}
         for rid, v in replied.items() if v.get("text")],
        key=lambda x: x["ts"], reverse=True
    )[:15]

    return {
        "trackable":        len(trackable),
        "trackable_list":   trackable[:10],
        "replies_received": replies_received,
        "replies_sent":     len(replied_list),
        "auto_blacklisted": auto_blacklisted,
        "last_cycle":       last_cycle,
        "replied_list":     replied_list,
    }


def _get_li_stats() -> dict:
    """Statistik LinkedIn dari stats.json (prefix li_) dan log files."""
    sp = os.path.join(_DIR, "stats.json")
    try:
        s = json.loads(Path(sp).read_text(encoding="utf-8")) if os.path.exists(sp) else {}
    except Exception:
        s = {}
    return {
        "commenter_running": _is_alive(LI_COMMENTER_PID),
        "poster_running":    _is_alive(LI_POSTER_PID),
        "watcher_running":   _is_alive(LI_WATCHER_PID),
        "comments_sent":     s.get("li_comments_sent", 0),
        "posts_sent":        s.get("li_posts_sent", 0),
        "replies_sent":      s.get("li_replies_sent", 0),
        "errors":            s.get("li_errors", 0),
    }


def _get_jh_status() -> dict:
    """Status Job Hunter + stats."""
    sp = os.path.join(_DIR, "stats.json")
    try:
        s = json.loads(Path(sp).read_text(encoding="utf-8")) if os.path.exists(sp) else {}
    except Exception:
        s = {}
    cfg = config_loader.load()
    # Hitung total dari file aplikasi
    apps_file = os.path.join(_DIR, "job_applications.json")
    total_found = 0
    try:
        if os.path.exists(apps_file):
            total_found = len(json.loads(Path(apps_file).read_text(encoding="utf-8")))
    except Exception:
        pass
    return {
        "running":               _is_alive(JH_PID),
        "pid":                   _read_pid(JH_PID),
        "job_comments_sent":     s.get("job_comments_sent", 0),
        "job_dms_sent":          s.get("job_dms_sent", 0),
        "job_comment_failures":  s.get("job_comment_failures", 0),
        "total_found":           total_found,
        "cv_summary":            cfg.get("job_hunter_cv_summary", ""),
        "interval":              cfg.get("job_hunter_scan_interval_minutes", 45),
        "scan_pages":            cfg.get("job_hunter_scan_pages", 8),
        "h_start":               cfg.get("job_hunter_active_start_hour", 8),
        "h_end":                 cfg.get("job_hunter_active_end_hour", 21),
    }


def _get_jh_applications() -> list:
    """Daftar lamaran dari job_applications.json, terbaru dulu."""
    apps_file = os.path.join(_DIR, "job_applications.json")
    try:
        data = json.loads(Path(apps_file).read_text(encoding="utf-8")) if os.path.exists(apps_file) else {}
    except Exception:
        data = {}
    result = []
    for post_id, entry in data.items():
        result.append({
            "post_id":  post_id,
            "date":     entry.get("date", ""),
            "username": entry.get("username", "?"),
            "job_type": entry.get("job_type", "?"),
            "comment":  entry.get("comment", ""),
            "dm_sent":  entry.get("dm_sent", False),
        })
    result.sort(key=lambda x: x.get("date", ""), reverse=True)
    return result[:50]


def _get_poster_posts():
    """Ambil riwayat postingan dari poster_output.log."""
    posts = []
    if not os.path.exists(POSTER_LOG):
        return posts
    try:
        with open(POSTER_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "DRAF POST:" in line:
                    posts.append({"text": line.split("DRAF POST:")[-1].strip(), "ts": 0})
    except Exception:
        pass
    return posts[-20:]


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ThreadScope</title>
<style>
:root{
  --bg:#0d1117;--surf:#161b22;--surf2:#1c2128;--surf3:#21262d;
  --brd:#30363d;--brd2:#21262d;
  --txt:#e6edf3;--muted:#8b949e;--muted2:#6e7681;
  --green:#3fb950;--red:#f85149;--yellow:#e3b341;--blue:#58a6ff;
  --purple:#bc8cff;--orange:#f0883e;--teal:#39d353;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* topbar */
.topbar{background:var(--surf);border-bottom:1px solid var(--brd);padding:0 20px;height:48px;display:flex;align-items:center;gap:10px;flex-shrink:0}
.logo{font-size:16px;font-weight:700;letter-spacing:.3px}.logo em{color:var(--purple);font-style:normal}
.badge{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:600;display:flex;align-items:center;gap:5px}
.badge.on{background:#1a3a1a;color:var(--green)}.badge.off{background:#2d1b1b;color:var(--red)}.badge.warn{background:#2d2200;color:var(--yellow)}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot.on{background:var(--green);box-shadow:0 0 5px var(--green)}.dot.off{background:var(--red)}.dot.warn{background:var(--yellow)}
.spacer{flex:1}
.topbar-right{display:flex;align-items:center;gap:12px}
.topbar-time{font-size:11px;color:var(--muted)}
.topbar-schedule{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--surf2);color:var(--muted)}
.topbar-schedule.active{color:var(--green)}

/* layout */
.layout{display:flex;flex:1;overflow:hidden}
nav{width:196px;background:var(--surf);border-right:1px solid var(--brd);padding:10px 8px;display:flex;flex-direction:column;gap:1px;flex-shrink:0}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:6px;cursor:pointer;color:var(--muted);font-size:13px;font-weight:500;transition:all .15s;border:none;background:none;width:100%;text-align:left}
.nav-item:hover{background:var(--surf2);color:var(--txt)}
.nav-item.active{background:var(--surf2);color:var(--txt)}
.nav-item.active .nav-icon{color:var(--purple)}
.nav-icon{width:15px;height:15px;flex-shrink:0}
.nav-sep{height:1px;background:var(--brd);margin:5px 0}
.nav-bottom{margin-top:auto}
.nav-ver{padding:8px 10px;font-size:10px;color:var(--muted2)}

/* pages */
.pages{flex:1;overflow:hidden;display:flex;flex-direction:column}
.page{display:none;flex:1;overflow:hidden;flex-direction:column}
.page.active{display:flex}
.page-body{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}

/* grid */
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}

/* stat card */
.stat-card{background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:14px 16px}
.stat-card .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin-bottom:8px}
.stat-card .num{font-size:26px;font-weight:700}
.stat-card .sub{font-size:11px;color:var(--muted);margin-top:4px}
.num.g{color:var(--green)}.num.r{color:var(--red)}.num.b{color:var(--blue)}.num.y{color:var(--yellow)}.num.p{color:var(--purple)}

/* card */
.card{background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:14px}
.card-hdr{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin-bottom:12px;display:flex;align-items:center;justify-content:space-between}
.card-hdr a{color:var(--blue);cursor:pointer;text-decoration:none;text-transform:none;letter-spacing:0;font-size:11px}
.row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--brd2)}
.row:last-child{border-bottom:none}
.row .k{color:var(--muted);font-size:12px}.row .v{font-size:12px;font-weight:500}

/* buttons */
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:7px 14px;border-radius:6px;border:none;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .15s;display:inline-flex;align-items:center;gap:5px}
.btn:hover{opacity:.85}.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-green{background:var(--green);color:#0d1117}
.btn-red{background:var(--red);color:#fff}
.btn-blue{background:var(--blue);color:#0d1117}
.btn-ghost{background:var(--surf2);color:var(--txt);border:1px solid var(--brd)}
.btn-danger{background:transparent;color:var(--red);border:1px solid var(--red)}
.btn.sm{padding:4px 10px;font-size:11px}
.btn.xs{padding:2px 7px;font-size:10px}

/* activity chart */
.chart-wrap{margin-top:4px}
.bars{display:flex;align-items:flex-end;gap:2px;height:60px}
.bar{flex:1;border-radius:2px 2px 0 0;background:var(--purple);opacity:.65;min-height:2px;transition:height .4s,opacity .2s;cursor:default;position:relative}
.bar:hover{opacity:1}
.bar-tip{position:absolute;bottom:calc(100%+4px);left:50%;transform:translateX(-50%);background:var(--surf3);border:1px solid var(--brd);border-radius:4px;padding:3px 7px;font-size:10px;white-space:nowrap;pointer-events:none;display:none;z-index:9}
.bar:hover .bar-tip{display:block}
.bar-xlbl{display:flex;justify-content:space-between;margin-top:3px}
.bar-xlbl span{font-size:9px;color:var(--muted2)}

/* terminal */
.term-toolbar{padding:8px 14px;border-bottom:1px solid var(--brd);display:flex;align-items:center;gap:8px;background:var(--surf);flex-shrink:0}
.fchip{padding:3px 8px;border-radius:10px;font-size:11px;cursor:pointer;background:var(--surf2);color:var(--muted);border:1px solid var(--brd)}
.fchip.on{background:var(--purple);color:#fff;border-color:var(--purple)}
#term{flex:1;overflow-y:auto;padding:10px 14px;font-family:"JetBrains Mono","Fira Code",ui-monospace,monospace;font-size:12px;line-height:1.65;background:#0a0e14}
.ln{white-space:pre-wrap;word-break:break-all;padding:1px 0}
.ln.info{color:#79c0ff}.ln.debug{color:#404850}.ln.filt{color:var(--yellow)}
.ln.draf{color:var(--green);font-weight:600}.ln.ok{color:var(--green)}.ln.err{color:var(--red)}
.ln.warn{color:var(--orange)}.ln.sched{color:var(--teal)}.ln.def{color:#b1bac4}.ln.hidden{display:none}

/* comments */
.search-row{display:flex;gap:8px}
.search-row input{flex:1;background:var(--surf);border:1px solid var(--brd);border-radius:6px;padding:7px 12px;color:var(--txt);font-size:13px;outline:none}
.search-row input:focus{border-color:var(--blue)}
.cmt-grid{display:flex;flex-direction:column;gap:8px}
.cmt{background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:11px 13px}.cmt.eng{border-color:rgba(250,204,21,.35);background:rgba(250,204,21,.04)}
.cmt .cap{font-size:11px;color:var(--muted);margin-bottom:5px;display:flex;justify-content:space-between;gap:8px}
.cmt .cap span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmt .rep{font-size:13px;line-height:1.5}
.cmt .meta{font-size:10px;color:var(--muted2);margin-top:5px}

/* settings */
.set-layout{display:grid;grid-template-columns:172px 1fr;flex:1;overflow:hidden}
.set-nav{border-right:1px solid var(--brd);padding:10px 8px;display:flex;flex-direction:column;gap:1px}

/* multi-account */
.acct-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.acct-card{background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:10px}
.acct-card .acct-hdr{display:flex;justify-content:space-between;align-items:flex-start}
.acct-card .acct-uname{font-size:14px;font-weight:700;color:var(--txt)}
.acct-card .acct-label{font-size:10px;color:var(--muted);margin-top:2px}
.acct-card .acct-proc{display:flex;flex-direction:column;gap:6px}
.proc-row{display:flex;align-items:center;gap:8px}
.proc-row .proc-lbl{font-size:11px;color:var(--muted);width:52px;flex-shrink:0}
.proc-row .badge{flex:1}
.acct-foot{font-size:11px;color:var(--muted2);padding-top:4px;border-top:1px solid var(--brd2)}
/* modal overlay */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:100;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal-box{background:var(--surf);border:1px solid var(--brd);border-radius:10px;width:min(520px,95vw);max-height:90vh;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px}
.modal-box h3{font-size:15px;font-weight:700;margin:0}
.form-row{display:flex;flex-direction:column;gap:4px}
.form-row label{font-size:11px;color:var(--muted)}
.form-row input,.form-row textarea,.form-row select{background:var(--surf2);border:1px solid var(--brd);border-radius:6px;padding:7px 10px;color:var(--txt);font-size:12px;outline:none;width:100%}
.form-row input:focus,.form-row textarea:focus{border-color:var(--blue)}
.form-row textarea{resize:vertical;min-height:60px;font-family:inherit}
.snav{padding:7px 10px;border-radius:5px;cursor:pointer;color:var(--muted);font-size:12px;transition:all .1s}
.snav:hover{background:var(--surf2);color:var(--txt)}
.snav.on{background:var(--surf2);color:var(--txt);font-weight:600}
.set-content{overflow-y:auto;padding:22px}
.ss{display:none}.ss.on{display:block}
.sec-title{font-size:16px;font-weight:600;margin-bottom:3px}
.sec-desc{font-size:12px;color:var(--muted);margin-bottom:20px;line-height:1.5}
.fg{margin-bottom:18px}
.fg label{font-size:12px;color:var(--muted);display:block;margin-bottom:6px;font-weight:500}
.fi{width:100%;background:var(--surf2);border:1px solid var(--brd);border-radius:6px;padding:8px 12px;color:var(--txt);font-size:13px;outline:none}
.fi:focus{border-color:var(--blue)}
select.fi option{background:var(--surf)}
input[type=range].fi{padding:4px 0;cursor:pointer;accent-color:var(--purple)}
.frow{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hint{font-size:11px;color:var(--muted2);margin-top:4px;line-height:1.5}
.rv{color:var(--purple);font-weight:700;margin-left:6px}
.save-bar{position:sticky;bottom:0;background:var(--bg);padding:12px 0 0;border-top:1px solid var(--brd);margin-top:20px;display:flex;gap:8px;align-items:center}
.save-ok{font-size:12px;color:var(--green)}

/* keyword chips */
.kw-wrap{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px;min-height:28px;align-items:center}
.kw{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:4px;font-size:11px;background:var(--surf3);border:1px solid var(--brd)}
.kw.custom{border-color:var(--purple);color:var(--purple)}
.kw-x{cursor:pointer;color:var(--muted2);font-size:13px;line-height:1}
.kw-x:hover{color:var(--red)}
.kw-add{display:flex;gap:6px;margin-top:4px}
.kw-add input{flex:1;background:var(--surf2);border:1px solid var(--brd);border-radius:5px;padding:5px 10px;color:var(--txt);font-size:12px;outline:none}
.kw-add input:focus{border-color:var(--purple)}

/* poster card */
.poster-card{background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:14px}
.poster-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.poster-hdr h3{font-size:13px;font-weight:600}
.post-item{padding:8px 0;border-bottom:1px solid var(--brd2)}
.post-item:last-child{border-bottom:none}
.post-item .txt{font-size:12px;line-height:1.5;color:var(--muted)}

/* watchdog badge */
.wd-badge{font-size:10px;padding:1px 7px;border-radius:10px;background:var(--surf3);color:var(--muted)}
.wd-badge.ok{background:#1a3a1a;color:var(--green)}

/* toast */
#toast{position:fixed;bottom:20px;right:20px;background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:9px 15px;font-size:12px;z-index:999;opacity:0;transform:translateY(8px);transition:all .2s;pointer-events:none}
#toast.show{opacity:1;transform:translateY(0)}
#toast.ok{border-color:var(--green);color:var(--green)}#toast.err{border-color:var(--red);color:var(--red)}

/* scrollbar */
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--brd);border-radius:3px}

/* hamburger (mobile only) */
.hamburger{display:none;background:none;border:none;cursor:pointer;color:var(--txt);padding:6px;flex-shrink:0;align-items:center;justify-content:center}
.nav-overlay{display:none;position:fixed;inset:0;top:48px;background:rgba(0,0,0,.55);z-index:49}
.nav-overlay.open{display:block}

/* table scroll wrapper */
.tbl-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

/* responsive */
@media(max-width:860px){
  .grid4{grid-template-columns:repeat(2,1fr)}
  .grid3{grid-template-columns:1fr 1fr}
}
@media(max-width:768px){
  .hamburger{display:inline-flex}
  nav{
    position:fixed;left:-220px;top:48px;bottom:0;z-index:50;
    transition:left .22s ease;overflow-y:auto;width:196px
  }
  nav.open{left:0;box-shadow:4px 0 20px rgba(0,0,0,.5)}
  .layout{overflow:visible}
  body{overflow:hidden}
  .grid4{grid-template-columns:1fr 1fr}
  .grid2,.grid3{grid-template-columns:1fr}
  .set-layout{grid-template-columns:1fr;overflow:auto}
  .set-nav{border-right:none;border-bottom:1px solid var(--brd);flex-direction:row;flex-wrap:wrap;padding:6px 8px;gap:2px;overflow-x:auto}
  .set-content{padding:14px}
  .frow{grid-template-columns:1fr}
  .page-body{padding:12px;gap:12px}
  .topbar-schedule,.topbar-time{display:none}
  .topbar-right{gap:8px}
  #session-warn span{display:none}
}
@media(max-width:480px){
  .topbar{padding:0 10px;gap:8px}
  .logo{font-size:14px}
  .page-body{padding:10px;gap:10px}
  .grid4{grid-template-columns:1fr 1fr}
  .stat-card{padding:10px 12px}
  .stat-card .num{font-size:22px}
  .modal-box{padding:14px}
}
</style>
</head>
<body>

<!-- topbar -->
<div class="topbar">
  <button class="hamburger" onclick="toggleNav()" aria-label="Menu">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
  </button>
  <div class="logo">Thread<em>Scope</em></div>
  <div class="badge off" id="top-badge"><div class="dot off" id="top-dot"></div><span id="top-txt">Stopped</span></div>
  <div class="badge off" id="poster-badge" style="display:none"><div class="dot off" id="poster-dot"></div><span id="poster-txt">Poster</span></div>
  <div id="session-warn" style="display:none;background:#2d1b1b;border:1px solid var(--red);border-radius:6px;padding:3px 12px;font-size:11px;color:var(--red);cursor:pointer" onclick="goPage('log')">⚠ Session mungkin expired — klik untuk lihat log</div>
  <div class="spacer"></div>
  <div class="topbar-right">
    <div class="topbar-schedule" id="sched-badge">⏰ —</div>
    <button class="btn btn-ghost" style="font-size:11px;padding:3px 10px;gap:5px;display:flex;align-items:center" onclick="goPage('akun')" title="Kelola akun">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
      <span id="topbar-username">@—</span>
    </button>
    <div class="topbar-time" id="clock"></div>
  </div>
</div>

<div id="nav-overlay" class="nav-overlay" onclick="toggleNav()"></div>
<div class="layout">
<!-- nav -->
<nav>
  <button class="nav-item active" onclick="goPage('ov')" id="nav-ov">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>Overview
  </button>
  <button class="nav-item" onclick="goPage('log')" id="nav-log">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>Live Log
  </button>
  <button class="nav-item" onclick="goPage('cmt')" id="nav-cmt">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>Komentar <span id="nav-cmt-count" style="margin-left:auto;font-size:10px;color:var(--muted)"></span>
  </button>
  <button class="nav-item" onclick="goPage('poster')" id="nav-poster">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>Poster
  </button>
  <button class="nav-item" onclick="goPage('watcher')" id="nav-watcher">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>Watcher
  </button>
  <button class="nav-item" onclick="goPage('akun')" id="nav-akun">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>Multi-Akun
  </button>
  <button class="nav-item" onclick="goPage('growth')" id="nav-growth">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>Growth
  </button>
  <button class="nav-item" onclick="goPage('analytics')" id="nav-analytics">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>Analitik
  </button>
  <button class="nav-item" onclick="goPage('bl')" id="nav-bl">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>Blacklist
  </button>
  <button class="nav-item" onclick="goPage('jobs')" id="nav-jobs">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/><line x1="12" y1="12" x2="12" y2="16"/><line x1="10" y1="14" x2="14" y2="14"/></svg>Job Hunter
  </button>
  <div class="nav-sep"></div>
  <button class="nav-item" onclick="goPage('li')" id="nav-li">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 8a6 6 0 016 6v7h-4v-7a2 2 0 00-2-2 2 2 0 00-2 2v7h-4v-7a6 6 0 016-6z"/><rect x="2" y="9" width="4" height="12"/><circle cx="4" cy="4" r="2"/></svg>LinkedIn
  </button>
  <div class="nav-sep"></div>
  <button class="nav-item" onclick="goPage('set')" id="nav-set">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 010 14.14M4.93 4.93a10 10 0 000 14.14"/></svg>Pengaturan
  </button>
  <div class="nav-sep nav-bottom"></div>
  <div class="nav-ver">ThreadScope v3</div>
</nav>

<div class="pages">

<!-- ════════════════════════════ OVERVIEW ═══════════════════════════════════ -->
<div class="page active" id="page-ov">
<div class="page-body">

  <div class="btn-row">
    <button class="btn btn-green" id="btn-start"   onclick="startBot()">▶ Commenter</button>
    <button class="btn btn-red"   id="btn-stop"    onclick="stopBot()" disabled>■ Stop Commenter</button>
    <button class="btn btn-green" id="wbtn-start"  onclick="startWatcher()" style="background:var(--teal)">▶ Reply Watcher</button>
    <button class="btn btn-red"   id="wbtn-stop"   onclick="stopWatcher()" disabled>■ Stop Watcher</button>
    <button class="btn btn-ghost" onclick="pollStatus();loadComments()">↻ Refresh</button>
  </div>

  <!-- stat cards -->
  <div class="grid4">
    <div class="stat-card"><div class="lbl">Komentar Terkirim</div><div class="num g" id="s-sent">0</div><div class="sub" id="s-last">—</div></div>
    <div class="stat-card"><div class="lbl">Post Dibuat</div><div class="num p" id="s-posts">0</div><div class="sub" id="s-lastpost">—</div></div>
    <div class="stat-card"><div class="lbl">Gagal / Error</div><div class="num r" id="s-err">0</div><div class="sub" id="s-lasterr" style="color:var(--red);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">—</div></div>
    <div class="stat-card"><div class="lbl">Mode</div><div class="num b" style="font-size:18px" id="s-mode">—</div><div class="sub"><span id="s-wd-badge" class="wd-badge">watchdog —</span></div></div>
  </div>

  <!-- chart + status -->
  <div class="grid2">
    <div class="card">
      <div class="card-hdr">Aktivitas 24 Jam Terakhir</div>
      <div class="chart-wrap">
        <div class="bars" id="chart-bars"><!-- diisi JS --></div>
        <div class="bar-xlbl"><span id="xlbl-left">—</span><span>Sekarang</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-hdr">Status Runtime</div>
      <div class="row"><span class="k">Commenter</span><span class="v" id="d-bot" style="color:var(--red)">Stopped</span></div>
      <div class="row"><span class="k">Reply Watcher</span><span class="v" id="d-watcher" style="color:var(--muted)">Stopped</span></div>
      <div class="row"><span class="k">Poster</span><span class="v" id="d-poster" style="color:var(--muted)">Stopped</span></div>
      <div class="row"><span class="k">Like Sebelum Komentar</span><span class="v" id="d-like">—</span></div>
      <div class="row"><span class="k">Jam Aktif</span><span class="v" id="d-sched">—</span></div>
      <div class="row"><span class="k">Watchdog</span><span class="v" id="d-wd">—</span></div>
      <div class="row"><span class="k">Komentar Terakhir</span><span class="v" id="d-lcmt">—</span></div>
      <div class="row"><span class="k">Kuota Komentar Hari Ini</span><span class="v" id="d-quota">—</span></div>
      <div class="row"><span class="k">Komentar Berikutnya</span><span class="v" id="d-next" style="color:var(--teal)">—</span></div>
      <div class="row"><span class="k">Watchdog Restart</span><span class="v" id="d-wdr">0×</span></div>
    </div>
  </div>

  <!-- filter + recent -->
  <div class="grid2">
    <div class="card">
      <div class="card-hdr">Filter Aktif <a onclick="goPage('set');goSet('kw')">Edit →</a></div>
      <div class="row"><span class="k">Topik</span><span class="v" style="color:var(--green)">IT &amp; AI Only</span></div>
      <div class="row"><span class="k">Lowongan</span><span class="v" style="color:var(--green)">Skip Otomatis</span></div>
      <div class="row"><span class="k">Link Guard</span><span class="v" style="color:var(--green)">Aktif</span></div>
      <div class="row"><span class="k">Max Umur Post</span><span class="v" id="d-age">5 jam</span></div>
      <div class="row"><span class="k">IT/AI Keywords</span><span class="v b" id="d-itkw">—</span></div>
      <div class="row"><span class="k">Loker Keywords</span><span class="v" id="d-lokerkw">—</span></div>
    </div>
    <div class="card">
      <div class="card-hdr">Komentar Terbaru <a onclick="goPage('cmt')">Lihat Semua →</a></div>
      <div id="ov-cmts"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
    </div>
  </div>

</div></div>

<!-- ════════════════════════════ LIVE LOG ════════════════════════════════════ -->
<div class="page" id="page-log">
  <div class="term-toolbar">
    <div style="display:flex;gap:4px;margin-right:8px;border-right:1px solid var(--brd);padding-right:8px">
      <div class="fchip src-chip on" onclick="switchLogSource('bot',this)">Bot</div>
      <div class="fchip src-chip"    onclick="switchLogSource('watcher',this)">Watcher</div>
      <div class="fchip src-chip"    onclick="switchLogSource('poster',this)">Poster</div>
    </div>
    <div id="log-chips" style="display:flex;gap:4px">
      <div class="fchip on"  data-f="all"   onclick="setFilt('all',this)">Semua</div>
      <div class="fchip"     data-f="info"  onclick="setFilt('info',this)">Info</div>
      <div class="fchip"     data-f="filt"  onclick="setFilt('filt',this)">Filter</div>
      <div class="fchip"     data-f="draf"  onclick="setFilt('draf',this)">Komentar</div>
      <div class="fchip"     data-f="sched" onclick="setFilt('sched',this)">Jadwal</div>
      <div class="fchip"     data-f="err"   onclick="setFilt('err',this)">Error</div>
    </div>
    <div class="spacer"></div>
    <button class="btn btn-ghost sm" onclick="clearTerm()">Bersihkan</button>
    <button class="btn btn-ghost sm" id="asbtn" onclick="toggleAS()">↓ Auto ON</button>
  </div>
  <div id="term"></div>
</div>

<!-- ════════════════════════════ KOMENTAR ════════════════════════════════════ -->
<div class="page" id="page-cmt">
<div class="page-body">
  <div class="search-row">
    <input type="text" id="q" placeholder="Cari komentar atau caption…" oninput="filterCmts()">
    <button class="btn btn-ghost sm" id="eng-flt" onclick="toggleEngFilter()">⭐ Engaged</button>
    <button class="btn btn-ghost" onclick="loadComments()">↻</button>
    <a class="btn btn-ghost sm" href="/api/comments/export" download="threadscope_comments.csv">⬇ Export CSV</a>
    <button class="btn btn-danger sm" onclick="clearCache()">Hapus Cache</button>
  </div>
  <div style="font-size:11px;color:var(--muted)" id="cmt-info">—</div>
  <div class="cmt-grid" id="cmt-list"><div style="color:var(--muted)">Memuat…</div></div>
</div></div>

<!-- ════════════════════════════ POSTER ══════════════════════════════════════ -->
<div class="page" id="page-poster">
<div class="page-body">

  <div class="btn-row">
    <button class="btn btn-green" id="pbtn-start" onclick="startPoster()">▶ Jalankan Poster</button>
    <button class="btn btn-red"   id="pbtn-stop"  onclick="stopPoster()" disabled>■ Hentikan Poster</button>
  </div>

  <div class="grid3">
    <div class="stat-card"><div class="lbl">Total Postingan</div><div class="num p" id="p-total">0</div><div class="sub">sejak mulai</div></div>
    <div class="stat-card"><div class="lbl">Terakhir Post</div><div class="num b" style="font-size:16px" id="p-last">—</div></div>
    <div class="stat-card"><div class="lbl">Interval</div><div class="num" style="font-size:18px" id="p-interval">48 jam</div><div class="sub">antar postingan</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Riwayat Postingan <a onclick="loadPosterPosts()">↻ Refresh</a></div>
    <div id="poster-list"><div style="color:var(--muted);font-size:12px">Belum ada postingan atau poster belum pernah dijalankan.</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Tentang Poster</div>
    <div class="row"><span class="k">Topik</span><span class="v" id="p-topics">—</span></div>
    <div class="row"><span class="k">Max Panjang</span><span class="v">400 karakter</span></div>
    <div class="row"><span class="k">Gaya</span><span class="v">Opini IT/AI natural, tanpa promosi</span></div>
    <div class="row"><span class="k">Jadwal</span><span class="v" id="p-sched">—</span></div>
  </div>

  <div class="card">
    <div class="card-hdr">Inspirasi Tren Sekarang <span id="trend-age" style="font-weight:400;font-size:10px;color:var(--muted);text-transform:none;letter-spacing:0;margin-left:6px"></span> <a onclick="loadTrending()">↻</a></div>
    <div id="trend-list"><div style="color:var(--muted);font-size:12px">Data tren muncul setelah Poster pertama kali dijalankan.</div></div>
  </div>

</div></div>

<!-- ════════════════════════════ PENGATURAN ══════════════════════════════════ -->
<div class="page" id="page-set">
<div class="set-layout">
  <div class="set-nav">
    <div class="snav on"  onclick="goSet('bot')"     id="sn-bot">Bot &amp; Mode</div>
    <div class="snav"     onclick="goSet('sched')"   id="sn-sched">Jadwal Aktif</div>
    <div class="snav"     onclick="goSet('timing')"  id="sn-timing">Timing</div>
    <div class="snav"     onclick="goSet('llm')"     id="sn-llm">LLM (Qoder)</div>
    <div class="snav"     onclick="goSet('kw')"      id="sn-kw">Kata Kunci</div>
    <div class="snav"     onclick="goSet('poster-s')"id="sn-poster-s">Poster</div>
    <div class="snav"     onclick="goSet('sys')"     id="sn-sys">Sistem</div>
  </div>
  <div class="set-content">

    <!-- Bot & Mode -->
    <div class="ss on" id="ss-bot">
      <div class="sec-title">Bot &amp; Mode</div>
      <div class="sec-desc">Pengaturan dasar cara bot memilih postingan target.</div>
      <div class="fg">
        <label>APP_MODE</label>
        <select class="fi" id="s-mode">
          <option value="normal">normal — pilih post acak dari timeline</option>
          <option value="trend">trend — prioritaskan post paling ramai</option>
        </select>
        <div class="hint">Mode trend: min 30 likes &amp; 3 replies. Mode normal: acak.</div>
      </div>
      <div class="fg">
        <label>Batas Umur Post <span class="rv" id="rv-age">5</span> jam</label>
        <input type="range" class="fi" id="s-age" min="1" max="24" value="5" oninput="rv('rv-age',this.value)">
        <div class="hint">Postingan lebih lama dari ini akan dilewati.</div>
      </div>
      <div class="fg">
        <label>Persona Bot</label>
        <textarea class="fi" id="s-persona" rows="5" style="resize:vertical;line-height:1.55;font-size:12px"></textarea>
        <div class="hint">Karakter yang dipakai AI saat menulis komentar. Deskripsikan background, gaya bahasa, dan sudut pandang bot.</div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm1"></span></div>
    </div>

    <!-- Jadwal -->
    <div class="ss" id="ss-sched">
      <div class="sec-title">Jadwal Aktif</div>
      <div class="sec-desc">Bot hanya aktif dalam rentang jam ini. Di luar jam ini bot tidur tanpa mati.</div>
      <div class="frow">
        <div class="fg">
          <label>Mulai Jam <span class="rv" id="rv-sh">07</span>:00</label>
          <input type="range" class="fi" id="s-sh" min="0" max="23" value="7" oninput="rv('rv-sh',String(this.value).padStart(2,'0'))">
        </div>
        <div class="fg">
          <label>Selesai Jam <span class="rv" id="rv-eh">23</span>:00</label>
          <input type="range" class="fi" id="s-eh" min="0" max="23" value="23" oninput="rv('rv-eh',String(this.value).padStart(2,'0'))">
        </div>
      </div>
      <div class="hint">Contoh: 07–23 artinya bot aktif dari jam 07:00 sampai 22:59.</div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm2"></span></div>
    </div>

    <!-- Timing -->
    <div class="ss" id="ss-timing">
      <div class="sec-title">Timing &amp; Delay</div>
      <div class="sec-desc">Jeda acak antar komentar agar terlihat natural.</div>
      <div class="frow">
        <div class="fg">
          <label>Jeda Min <span class="rv" id="rv-dmin">14</span> menit</label>
          <input type="range" class="fi" id="s-dmin" min="5" max="60" value="14" oninput="rv('rv-dmin',this.value);syncDmax()">
        </div>
        <div class="fg">
          <label>Jeda Max <span class="rv" id="rv-dmax">17</span> menit</label>
          <input type="range" class="fi" id="s-dmax" min="5" max="60" value="17" oninput="rv('rv-dmax',this.value)">
        </div>
      </div>
      <div class="hint" style="margin-bottom:14px">⚠ Di bawah 10 menit berisiko rate-limit dari Meta.</div>
      <div class="fg">
        <label>Plafon Komentar per Hari <span class="rv" id="rv-bquota">40</span> komentar</label>
        <input type="range" class="fi" id="s-bquota" min="0" max="100" value="40" oninput="rv('rv-bquota',this.value)">
        <div class="hint">Pengaman runaway — kalau tercapai, bot berhenti komentar sampai tengah malam lalu lanjut otomatis, dan tidak menyentuh API sama sekali selama menunggu. <b>Hanya menghitung komentar proaktif</b>: balasan ke orang yang membalas Anda tidak dibatasi. Dihitung ulang tiap tengah malam, per akun. <b>0 = tanpa batas.</b></div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm3"></span></div>
    </div>

    <!-- LLM -->
    <div class="ss" id="ss-llm">
      <div class="sec-title">LLM — Qoder CLI</div>
      <div class="sec-desc">Konfigurasi backend AI untuk menghasilkan komentar.</div>
      <div class="fg">
        <label>Qoder Binary Path</label>
        <input type="text" class="fi" id="s-qbin" placeholder="Kosong = auto-detect dari PATH">
        <div class="hint">Contoh: /Users/mac/.qoder/entry/qoder</div>
      </div>
      <div class="fg">
        <label>Model (opsional)</label>
        <input type="text" class="fi" id="s-qmdl" placeholder="Kosong = default Qoder">
      </div>
      <div class="fg">
        <label>Max Output Tokens <span class="rv" id="rv-tok">120</span></label>
        <input type="range" class="fi" id="s-tok" min="50" max="400" value="120" oninput="rv('rv-tok',this.value)">
        <div class="hint">Lebih kecil = komentar lebih pendek dan natural. Rekomendasi: 80–150.</div>
      </div>
      <div class="fg">
        <label>Max Panjang Komentar <span class="rv" id="rv-mlen">350</span> karakter</label>
        <input type="range" class="fi" id="s-mlen" min="50" max="500" value="350" oninput="rv('rv-mlen',this.value)">
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm4"></span></div>
    </div>

    <!-- Keywords -->
    <div class="ss" id="ss-kw">
      <div class="sec-title">Filter Kata Kunci</div>
      <div class="sec-desc">Tambahkan kata kunci IT/AI agar bot ikut mendeteksinya, atau kata loker yang harus di-skip.</div>
      <div class="fg">
        <label>Kata Kunci IT/AI Tambahan</label>
        <div class="kw-wrap" id="it-kws"></div>
        <div class="kw-add">
          <input type="text" id="it-inp" placeholder="Tambah keyword…" onkeydown="if(event.key==='Enter')addKw('it')">
          <button class="btn btn-ghost sm" onclick="addKw('it')">+ Tambah</button>
        </div>
      </div>
      <div class="fg" style="margin-top:18px">
        <label>Kata Kunci Loker Tambahan (selalu di-skip)</label>
        <div class="kw-wrap" id="lk-kws"></div>
        <div class="kw-add">
          <input type="text" id="lk-inp" placeholder="Tambah keyword loker…" onkeydown="if(event.key==='Enter')addKw('lk')">
          <button class="btn btn-ghost sm" onclick="addKw('lk')">+ Tambah</button>
        </div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm5"></span></div>
    </div>

    <!-- Poster settings -->
    <div class="ss" id="ss-poster-s">
      <div class="sec-title">Poster Otomatis</div>
      <div class="sec-desc">Bot poster membuat postingan original di Threads secara berkala. Bisa dijalankan bersamaan dengan commenter.</div>
      <div class="fg">
        <label>Interval Posting <span class="rv" id="rv-pint">48</span> jam</label>
        <input type="range" class="fi" id="s-pint" min="0" max="168" value="48" oninput="rv('rv-pint',this.value)">
        <div class="hint">Jeda antara satu postingan ke postingan berikutnya. <b>0 = interval nonaktif</b> — frekuensi diatur sepenuhnya oleh Kuota Harian di bawah.</div>
      </div>
      <div class="fg">
        <label>Kuota Postingan per Hari <span class="rv" id="rv-pquota">5</span> post</label>
        <input type="range" class="fi" id="s-pquota" min="0" max="20" value="5" oninput="rv('rv-pquota',this.value)">
        <div class="hint">Batas maksimal postingan dalam satu hari kalender, dihitung ulang tiap tengah malam. <b>0 = tanpa batas</b> (tidak disarankan — berisiko terdeteksi spam oleh Meta).</div>
      </div>
      <div class="fg">
        <label>Jarak Minimum Antar Postingan <span class="rv" id="rv-pgap">120</span> menit</label>
        <input type="range" class="fi" id="s-pgap" min="0" max="720" step="15" value="120" oninput="rv('rv-pgap',this.value)">
        <div class="hint">Pengaman anti-menumpuk: dua postingan tidak mungkin keluar lebih rapat dari ini, walau sedang jam ramai dan kuota masih ada. <b>0 = nonaktif</b> (tidak disarankan).</div>
      </div>
      <div class="fg">
        <label>Hanya Posting di Jam Ramai</label>
        <select class="fi" id="s-ppeak">
          <option value="true">Aktif — tunggu sampai jam ramai (dari data aktivitas nyata)</option>
          <option value="false">Nonaktif — posting kapan pun selama jam aktif</option>
        </select>
        <div class="hint">Jam ramai dihitung otomatis dari riwayat balasan 7 hari terakhir. Kalau belum ada data, poster memakai jam aktif biasa supaya tidak macet.</div>
      </div>
      <div class="fg">
        <label>Topik (satu per baris)</label>
        <textarea class="fi" id="s-ptopics" rows="6" style="resize:vertical;line-height:1.6"></textarea>
        <div class="hint">Bot akan memilih satu topik secara acak tiap kali posting.</div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm6"></span></div>
    </div>

    <!-- Kualitas komentar AI -->
    <div class="ss" id="ss-quality">
      <div class="sec-title">Kualitas Komentar AI</div>
      <div class="sec-desc">Setiap draf dinilai LLM pada tiga dimensi — natural, jelas, dan sopan. Yang di bawah ambang akan di-generate ulang, dan kalau tetap jelek akan dibuang sekaligus dijadikan contoh negatif agar polanya tidak terulang.</div>
      <div class="fg">
        <label>Skor Minimum Lolos <span class="rv" id="rv-qmin">6</span> / 10</label>
        <input type="range" class="fi" id="s-qmin" min="0" max="10" value="6" oninput="rv('rv-qmin',this.value)">
        <div class="hint">Diterapkan pada nilai <b>terendah</b> dari tiga dimensi. 0 = gate dimatikan (semua draf lolos).</div>
      </div>
      <div class="fg">
        <label>Percobaan Ulang Maksimal <span class="rv" id="rv-qretry">2</span>×</label>
        <input type="range" class="fi" id="s-qretry" min="0" max="5" value="2" oninput="rv('rv-qretry',this.value)">
        <div class="hint">Tiap percobaan menambah satu panggilan LLM (~10 detik). 0 = langsung buang tanpa mencoba ulang.</div>
      </div>
      <div class="fg">
        <label>Contoh Buruk di Prompt <span class="rv" id="rv-qneg">3</span> buah</label>
        <input type="range" class="fi" id="s-qneg" min="0" max="10" value="3" oninput="rv('rv-qneg',this.value)">
        <div class="hint">Jumlah komentar lama berkualitas rendah yang disuntikkan ke prompt sebagai "jangan kayak gini". 0 = fitur belajar dari contoh buruk dimatikan.</div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm8"></span></div>
    </div>

    <!-- Sistem -->
    <div class="ss" id="ss-sys">
      <div class="sec-title">Sistem</div>
      <div class="sec-desc">Pengaturan watchdog dan data.</div>
      <div class="fg">
        <label>Like Sebelum Komentar</label>
        <select class="fi" id="s-like">
          <option value="true">Aktif — like post dulu sebelum komentar (lebih natural)</option>
          <option value="false">Nonaktif</option>
        </select>
        <div class="hint">Jeda 2–6 detik antara like dan komentar agar tidak terlihat seperti bot.</div>
      </div>
      <div class="fg">
        <label>Auto-Reply ke Balasan</label>
        <select class="fi" id="s-ar">
          <option value="false">Nonaktif</option>
          <option value="true">Aktif — balas orang yang reply ke komentar kita</option>
        </select>
        <div class="hint">Butuh THREADS_USERNAME di .env. Jalankan Reply Watcher dari tombol Overview.</div>
      </div>
      <div class="fg">
        <label>Interval Cek Balasan <span class="rv" id="rv-arc">30</span> menit</label>
        <input type="range" class="fi" id="s-arc" min="10" max="120" value="30" oninput="rv('rv-arc',this.value)">
      </div>
      <div class="fg">
        <label>Penyaring Balasan Masuk</label>
        <select class="fi" id="s-rcls">
          <option value="true">Aktif — bedakan balasan jelas / tidak jelas / tidak sopan</option>
          <option value="false">Nonaktif — balas semua seperti biasa</option>
        </select>
        <div class="hint">Balasan tidak jelas dijawab dengan klarifikasi singkat; balasan tidak sopan dilewati. Menambah satu panggilan LLM per balasan.</div>
      </div>
      <div class="fg">
        <label>Blacklist Otomatis Pengirim Kasar</label>
        <select class="fi" id="s-rbl">
          <option value="false">Nonaktif — lewati saja, jangan blacklist (disarankan)</option>
          <option value="true">Aktif — tambahkan username ke blacklist permanen</option>
        </select>
        <div class="hint">Blokir permanen diputuskan dari satu penilaian LLM, jadi berisiko salah orang. Masih bisa dibatalkan manual dari menu Blacklist.</div>
      </div>
      <div class="fg" style="margin-top:14px">
        <label>Watchdog Auto-Restart</label>
        <select class="fi" id="s-wd">
          <option value="true">Aktif — restart otomatis jika bot crash</option>
          <option value="false">Nonaktif</option>
        </select>
        <div class="hint">Watchdog berjalan di background server ini dan memeriksa setiap 30 detik.</div>
      </div>
      <div class="fg" style="margin-top:20px">
        <label>Aksi Data</label>
        <div class="btn-row">
          <button class="btn btn-danger sm" onclick="clearCache()">Hapus Cache Komentar</button>
          <button class="btn btn-danger sm" onclick="resetStats()">Reset Statistik</button>
        </div>
        <div class="hint">Cache komentar dipakai untuk menghindari jawaban duplikat. Reset statistik menghapus stats.json.</div>
      </div>
      <div class="fg" style="margin-top:18px">
        <label>Backup &amp; Restore Pengaturan</label>
        <div class="btn-row">
          <a class="btn btn-ghost sm" href="/api/settings/export" download="threadscope_settings.json">⬇ Export JSON</a>
          <label class="btn btn-ghost sm" style="cursor:pointer">⬆ Import JSON<input type="file" id="import-file" accept=".json" style="display:none" onchange="importSettings(this)"></label>
        </div>
        <div class="hint">Export untuk backup. Import untuk restore atau pindah mesin — pengaturan akan di-merge ke yang ada.</div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm7"></span></div>
    </div>

  </div>
</div>
</div>

<!-- ════════════════════════════ GROWTH ══════════════════════════════════════ -->
<div class="page" id="page-growth">
<div class="page-body">

  <div class="grid4">
    <div class="stat-card"><div class="lbl">Followers</div><div class="num p" id="g-fol">—</div><div class="sub" id="g-fol-sub">memuat…</div></div>
    <div class="stat-card"><div class="lbl">Following</div><div class="num b" id="g-fwg">—</div><div class="sub" id="g-fwg-sub"></div></div>
    <div class="stat-card"><div class="lbl">+Followers 7 Hari</div><div class="num g" id="g-delta">—</div><div class="sub" id="g-delta-sub"></div></div>
    <div class="stat-card"><div class="lbl">Total Post</div><div class="num" id="g-posts" style="color:var(--yellow)">—</div><div class="sub">di akun Threads</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Pertumbuhan Follower 30 Hari <a onclick="loadGrowth()">↻ Refresh</a></div>
    <div class="chart-wrap">
      <div class="bars" id="g-bars" style="height:80px"><!-- diisi JS --></div>
      <div class="bar-xlbl"><span id="g-xlbl-l">—</span><span id="g-xlbl-r">Hari ini</span></div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px">Snapshot diambil otomatis sekali per hari. Data mulai terkumpul setelah dashboard berjalan 24 jam.</div>
  </div>

  <div class="card">
    <div class="card-hdr">Riwayat Harian</div>
    <div id="g-table" style="font-size:12px;max-height:300px;overflow-y:auto">
      <div style="color:var(--muted)">Memuat…</div>
    </div>
    <div style="font-size:10px;color:var(--muted);margin-top:6px">Kolom Komentar menunjukkan berapa komentar dikirim hari itu (dari cache 30 hari).</div>
  </div>

</div></div>

<!-- ════════════════════════════ WATCHER ═════════════════════════════════════ -->
<div class="page" id="page-watcher">
<div class="page-body">

  <div class="btn-row">
    <button class="btn btn-green" id="wbtn2-start" onclick="startWatcher()" style="background:var(--teal)">▶ Jalankan Watcher</button>
    <button class="btn btn-red"   id="wbtn2-stop"  onclick="stopWatcher()" disabled>■ Hentikan Watcher</button>
    <button class="btn btn-ghost" onclick="loadWatcher()">↻ Refresh</button>
  </div>

  <div class="grid4">
    <div class="stat-card"><div class="lbl">Komentar Dipantau</div><div class="num b" id="w-trackable">—</div><div class="sub">dalam 24 jam terakhir</div></div>
    <div class="stat-card"><div class="lbl">Balasan Diterima</div><div class="num g" id="w-received">—</div><div class="sub">total dari log</div></div>
    <div class="stat-card"><div class="lbl">Balasan Terkirim</div><div class="num p" id="w-sent">—</div><div class="sub">berhasil dibalas bot</div></div>
    <div class="stat-card"><div class="lbl">Auto-Blacklist</div><div class="num r" id="w-bl">—</div><div class="sub">reply negatif terdeteksi</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Komentar Sedang Dipantau <span id="w-last-cycle" style="font-weight:400;font-size:10px;color:var(--muted);text-transform:none;letter-spacing:0;margin-left:6px"></span></div>
    <div id="w-trackable-list"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Riwayat Balasan Terkirim</div>
    <div id="w-replied-list"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
  </div>

</div></div>

<!-- ════════════════════════════ MULTI-AKUN ══════════════════════════════════ -->
<div class="page" id="page-akun">
<div class="page-body">

  <div class="btn-row" style="justify-content:space-between;align-items:center">
    <span style="font-size:13px;font-weight:600;color:var(--txt)">Akun Aktif</span>
    <button class="btn btn-green" onclick="openAddAccount()">+ Tambah Akun</button>
  </div>

  <div class="acct-grid" id="acct-grid">
    <div style="color:var(--muted);font-size:12px">Memuat…</div>
  </div>

  <div class="card" style="font-size:11px;color:var(--muted);line-height:1.7">
    <div class="card-hdr">Cara Pakai Multi-Akun</div>
    <div>1. Klik <b>Tambah Akun</b> dan masukkan credentials setiap akun Threads.</div>
    <div>2. Setiap akun punya folder data sendiri — histori, stats, dan memory tidak bercampur.</div>
    <div>3. Klik <b>▶ Bot</b> pada kartu akun untuk mulai. Semua akun bisa jalan bersamaan.</div>
    <div>4. Klik ⚙ untuk atur persona, delay, topik, dan blacklist per-akun.</div>
  </div>

</div></div>

<!-- modal tambah akun -->
<div class="modal-overlay" id="modal-add-acct" onclick="if(event.target===this)closeModal('modal-add-acct')">
<div class="modal-box">
  <h3>Tambah Akun Baru</h3>
  <div class="form-row"><label>Nama / Label Akun</label><input id="fa-name" placeholder="contoh: akun_utama"></div>
  <div class="form-row"><label>THREADS_USERNAME (tanpa @)</label><input id="fa-uname" placeholder="username_threads"></div>
  <div class="form-row"><label>THREADS_SESSION_ID *</label><input id="fa-sid" placeholder="sessionid dari cookie"></div>
  <div class="form-row"><label>THREADS_CSRF_TOKEN *</label><input id="fa-csrf" placeholder="csrftoken dari cookie"></div>
  <div class="form-row"><label>THREADS_DS_USER_ID *</label><input id="fa-uid" placeholder="ds_user_id dari cookie"></div>
  <div class="form-row"><label>THREADS_MID</label><input id="fa-mid" placeholder="mid dari cookie (opsional)"></div>
  <div class="form-row"><label>THREADS_IG_DID</label><input id="fa-igdid" placeholder="ig_did dari cookie (opsional)"></div>
  <div class="btn-row">
    <button class="btn btn-green" onclick="createAccount()">Simpan Akun</button>
    <button class="btn btn-ghost" onclick="closeModal('modal-add-acct')">Batal</button>
  </div>
  <div id="fa-msg" style="font-size:11px;color:var(--red);display:none"></div>
</div></div>

<!-- modal settings akun -->
<div class="modal-overlay" id="modal-acct-set" onclick="if(event.target===this)closeModal('modal-acct-set')">
<div class="modal-box">
  <h3 id="aset-title">Pengaturan Akun</h3>
  <input type="hidden" id="aset-name">
  <div class="form-row"><label>Persona Bot</label><textarea id="aset-persona" rows="3"></textarea></div>
  <div class="form-row"><label>Delay Min (menit)</label><input type="number" id="aset-dmin"></div>
  <div class="form-row"><label>Delay Max (menit)</label><input type="number" id="aset-dmax"></div>
  <div class="form-row"><label>Jam Aktif Mulai</label><input type="number" id="aset-hstart" min="0" max="23"></div>
  <div class="form-row"><label>Jam Aktif Selesai</label><input type="number" id="aset-hend" min="0" max="23"></div>
  <div class="form-row"><label>Interval Poster (jam)</label><input type="number" id="aset-pint"></div>
  <div class="form-row"><label>Topik Poster (satu per baris)</label><textarea id="aset-topics" rows="4"></textarea></div>
  <div class="form-row"><label>Blacklist (satu username per baris)</label><textarea id="aset-bl" rows="3"></textarea></div>
  <div class="btn-row">
    <button class="btn btn-green" onclick="saveAcctSettings()">Simpan</button>
    <button class="btn btn-ghost" onclick="closeModal('modal-acct-set')">Batal</button>
  </div>
  <div id="aset-msg" style="font-size:11px;color:var(--green);display:none"></div>
</div></div>

<!-- ════════════════════════════ ANALITIK ════════════════════════════════════ -->
<div class="page" id="page-analytics">
<div class="page-body">

  <div class="grid4">
    <div class="stat-card"><div class="lbl">Total Komentar</div><div class="num p" id="an-total">—</div><div class="sub">sejak mulai bot</div></div>
    <div class="stat-card"><div class="lbl">Komentar Engaged</div><div class="num g" id="an-engaged">—</div><div class="sub">dapat balasan</div></div>
    <div class="stat-card"><div class="lbl">Engagement Rate</div><div class="num b" id="an-rate">—</div><div class="sub">keseluruhan</div></div>
    <div class="stat-card"><div class="lbl">Gaya Terbaik</div><div class="num" style="font-size:18px;color:var(--purple)" id="an-best">—</div><div class="sub" id="an-best-sub">—</div></div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="card-hdr">Distribusi Gaya Komentar <a onclick="loadAnalytics()">↻</a></div>
      <div id="an-style-bars"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
    </div>
    <div class="card">
      <div class="card-hdr">Engagement per Tipe Postingan</div>
      <div id="an-sent-bars"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-hdr">Komentar per Hari — 7 Hari Terakhir</div>
    <div id="an-daily-chart"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Jam Aktif — 7 Hari Terakhir <a onclick="loadAnalytics()">↻</a></div>
    <div id="an-hour-chart"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
    <div style="font-size:10px;color:var(--muted);margin-top:6px">Hijau = jam peak · batang = jumlah komentar dikirim</div>
  </div>

  <div class="card">
    <div class="card-hdr">Panjang Komentar vs Engagement</div>
    <div id="an-length-stats"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Komentar yang Dapat Balasan ⭐</div>
    <div id="an-engaged-list"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
  </div>

</div></div>

<!-- ════════════════════════════ BLACKLIST ════════════════════════════════════ -->
<div class="page" id="page-bl">
<div class="page-body">

  <div class="card">
    <div class="card-hdr">Tambah ke Blacklist</div>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <input type="text" id="bl-inp" class="fi" style="flex:1;background:var(--surf2)" placeholder="username tanpa @ (contoh: somebot123)" onkeydown="if(event.key==='Enter')addBlacklist()">
      <button class="btn btn-red sm" onclick="addBlacklist()">Blacklist</button>
    </div>
    <div class="hint">Akun yang di-blacklist akan dilewati oleh commenter secara permanen.</div>
  </div>

  <div class="card">
    <div class="card-hdr">Akun yang Di-blacklist <span id="bl-count" style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;font-size:11px"></span> <a onclick="loadBlacklist()">↻</a></div>
    <div id="bl-list"><div style="color:var(--muted);font-size:12px">Memuat…</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Auto-Blacklist dari Komentar</div>
    <div class="row"><span class="k">Akun yang sudah dikomentari</span><span class="v" id="bl-commented">—</span></div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px;line-height:1.6">
      Klik username di bawah untuk langsung blacklist akun yang sudah pernah dikomentari.
    </div>
    <div id="bl-auto-list" style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px"></div>
  </div>

</div></div>

<!-- ════════════════════════════ JOB HUNTER ══════════════════════════════════ -->
<div class="page" id="page-jobs">
<div class="page-header"><h2>Job Hunter</h2><span class="page-sub">Auto-lamar lowongan IT di Threads</span></div>

<div class="card" style="margin-bottom:14px">
  <div class="card-title">Status & Kontrol</div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <button class="btn btn-green" id="jh-start-btn" onclick="jhAction('start')">▶ Start Job Hunter</button>
    <button class="btn btn-red"   id="jh-stop-btn"  onclick="jhAction('stop')" style="display:none">■ Stop</button>
    <span id="jh-status-badge" style="font-size:12px;color:var(--muted)">Memuat…</span>
  </div>
</div>

<div class="stats-grid" style="margin-bottom:14px">
  <div class="stat-card"><div class="stat-val" id="jh-stat-found">–</div><div class="stat-lbl">Lowongan Ditemukan</div></div>
  <div class="stat-card"><div class="stat-val" id="jh-stat-applied">–</div><div class="stat-lbl">Sudah Dilamar</div></div>
  <div class="stat-card"><div class="stat-val" id="jh-stat-dm">–</div><div class="stat-lbl">DM Terkirim</div></div>
  <div class="stat-card"><div class="stat-val" id="jh-stat-fail">–</div><div class="stat-lbl">Gagal</div></div>
</div>

<div class="card" style="margin-bottom:14px">
  <div class="card-title">Pengaturan CV Summary</div>
  <div class="form-row">
    <label>Ringkasan skill / CV kamu (dipakai AI untuk buat komentar lamaran)</label>
    <textarea id="jh-cv" rows="3" placeholder="contoh: dev ~3 tahun (python + js + react), pernah bikin SaaS kecil, ngulik AI/LLM"></textarea>
  </div>
  <div class="form-row" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
    <div><label>Interval Scan (menit)</label><input type="number" id="jh-interval" min="15" max="180" value="45"></div>
    <div><label>Halaman per Scan</label><input type="number" id="jh-pages" min="1" max="20" value="8" title="Lebih banyak halaman = lebih banyak post yang di-scan per siklus"></div>
    <div><label>Jam Aktif</label>
      <div style="display:flex;gap:6px;align-items:center">
        <input type="number" id="jh-hstart" min="0" max="23" value="8" style="width:60px">
        <span style="color:var(--muted)">–</span>
        <input type="number" id="jh-hend" min="0" max="23" value="21" style="width:60px">
      </div>
    </div>
  </div>
  <div class="btn-row"><button class="btn btn-green" onclick="saveJhSettings()">Simpan Pengaturan</button><span id="jh-save-msg" style="font-size:11px;color:var(--green);display:none">Tersimpan!</span></div>
</div>

<div class="card">
  <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
    Riwayat Lamaran
    <button class="btn btn-ghost xs" onclick="loadJhApplications()">↺ Refresh</button>
  </div>
  <div id="jh-app-list" style="font-size:13px">Memuat…</div>
</div>

</div></div>

<!-- ════════════════════════════ LINKEDIN ════════════════════════════════════ -->
<div class="page" id="page-li">
<div class="page-body">

  <div class="card">
    <div class="card-hdr">LinkedIn — Session</div>
    <div class="form-row" style="margin-bottom:10px">
      <label>Cookie <code>li_at</code> (dari DevTools → Application → Cookies)</label>
      <input type="password" id="li-at-inp" class="fi" placeholder="AQE..." autocomplete="off">
    </div>
    <div class="form-row" style="margin-bottom:10px">
      <label>Cookie <code>JSESSIONID</code> (untuk csrf-token)</label>
      <input type="password" id="li-jsession-inp" class="fi" placeholder="ajax:..." autocomplete="off">
    </div>
    <div class="btn-row">
      <button class="btn btn-blue" onclick="saveLiSession()">💾 Simpan Session</button>
    </div>
    <div class="hint" style="margin-top:8px">Login LinkedIn → F12 → Application → Cookies → www.linkedin.com → copy nilai <code>li_at</code> dan <code>JSESSIONID</code>.</div>
  </div>

  <div class="card">
    <div class="card-hdr">LinkedIn — Kontrol Bot</div>
    <div class="btn-row" style="margin-bottom:10px">
      <button class="btn btn-green" id="li-comm-start" onclick="liAction('commenter','start')">▶ Commenter</button>
      <button class="btn btn-red"   id="li-comm-stop"  onclick="liAction('commenter','stop')" disabled>■ Stop Commenter</button>
    </div>
    <div class="btn-row" style="margin-bottom:10px">
      <button class="btn btn-green" id="li-post-start" onclick="liAction('poster','start')" style="background:var(--teal)">▶ Poster</button>
      <button class="btn btn-red"   id="li-post-stop"  onclick="liAction('poster','stop')" disabled>■ Stop Poster</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-green" id="li-watch-start" onclick="liAction('watcher','start')" style="background:var(--blue)">▶ Reply Watcher</button>
      <button class="btn btn-red"   id="li-watch-stop"  onclick="liAction('watcher','stop')" disabled>■ Stop Watcher</button>
    </div>
  </div>

  <div class="grid4">
    <div class="stat-card"><div class="lbl">Komentar Terkirim</div><div class="num g" id="li-s-sent">—</div></div>
    <div class="stat-card"><div class="lbl">Post Dibuat</div><div class="num p" id="li-s-posts">—</div></div>
    <div class="stat-card"><div class="lbl">Balasan Dikirim</div><div class="num b" id="li-s-replies">—</div></div>
    <div class="stat-card"><div class="lbl">Error</div><div class="num r" id="li-s-errors">—</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">Status Runtime LinkedIn</div>
    <div class="row"><span class="k">Commenter</span><span class="v" id="li-d-comm" style="color:var(--red)">Stopped</span></div>
    <div class="row"><span class="k">Poster</span><span class="v" id="li-d-post" style="color:var(--muted)">Stopped</span></div>
    <div class="row"><span class="k">Reply Watcher</span><span class="v" id="li-d-watch" style="color:var(--muted)">Stopped</span></div>
  </div>

  <div class="card">
    <div class="card-hdr">Live Log LinkedIn
      <div style="display:flex;gap:6px">
        <span class="fchip" id="li-log-comm" onclick="switchLiLog('commenter')">Commenter</span>
        <span class="fchip" id="li-log-post" onclick="switchLiLog('poster')">Poster</span>
        <span class="fchip" id="li-log-watch" onclick="switchLiLog('watcher')">Watcher</span>
      </div>
    </div>
    <div id="li-term" style="height:260px;overflow-y:auto;padding:10px;font-family:monospace;font-size:12px;background:#0a0e14;border-radius:6px;line-height:1.6">
      <div style="color:var(--muted)">Pilih sumber log di atas lalu klik Start untuk melihat output.</div>
    </div>
  </div>

</div></div>

</div><!-- /pages -->
</div><!-- /layout -->
<div id="toast"></div>

<script>
// ── globals ──────────────────────────────────────────────────────────────────
let allCmts=[], logFilt='all', autoScroll=true, settings={};
let extraIt=[], extraLk=[];

// ── utils ──────────────────────────────────────────────────────────────────
function rv(id,val){document.getElementById(id).textContent=val}
function fmtTs(u){if(!u)return'—';return new Date(u*1e3).toLocaleString('id-ID',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})}
function toast(msg,type='ok'){const el=document.getElementById('toast');el.textContent=msg;el.className='show '+type;setTimeout(()=>el.className='',2500)}

// ── clock & schedule ────────────────────────────────────────────────────────
function updateClock(){
  const now=new Date();
  document.getElementById('clock').textContent=now.toLocaleString('id-ID',{weekday:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
setInterval(updateClock,1000);updateClock();

// ── page nav ──────────────────────────────────────────────────────────────
function goSet(id){
  document.querySelectorAll('.ss').forEach(s=>s.classList.remove('on'));
  document.querySelectorAll('.snav').forEach(s=>s.classList.remove('on'));
  document.getElementById('ss-'+id).classList.add('on');
  document.getElementById('sn-'+id).classList.add('on');
}

// ── bot controls ──────────────────────────────────────────────────────────
async function startBot(){
  document.getElementById('btn-start').disabled=true;
  const d=await(await fetch('/api/bot/start',{method:'POST'})).json();
  toast(d.ok?'Commenter dimulai!':'Gagal: '+d.msg,d.ok?'ok':'err');
  pollStatus();
}
async function stopBot(){
  document.getElementById('btn-stop').disabled=true;
  await fetch('/api/bot/stop',{method:'POST'});
  toast('Commenter dihentikan');pollStatus();
}
async function startPoster(){
  document.getElementById('pbtn-start').disabled=true;
  const d=await(await fetch('/api/poster/start',{method:'POST'})).json();
  toast(d.ok?'Poster dimulai!':'Gagal: '+d.msg,d.ok?'ok':'err');
  pollStatus();
}
async function stopPoster(){
  document.getElementById('pbtn-stop').disabled=true;
  await fetch('/api/poster/stop',{method:'POST'});
  toast('Poster dihentikan');pollStatus();
}
async function startWatcher(){
  document.getElementById('wbtn-start').disabled=true;
  const d=await(await fetch('/api/watcher/start',{method:'POST'})).json();
  toast(d.ok?'Reply Watcher dimulai!':'Gagal: '+d.msg,d.ok?'ok':'err');
  pollStatus();
}
async function stopWatcher(){
  document.getElementById('wbtn-stop').disabled=true;
  await fetch('/api/watcher/stop',{method:'POST'});
  toast('Reply Watcher dihentikan');pollStatus();
}

// ── status ────────────────────────────────────────────────────────────────
async function pollStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    const rb=d.running, rp=d.poster_running;
    const s=d.stats||{};

    // topbar
    document.getElementById('top-dot').className='dot '+(rb?'on':'off');
    const tb=document.getElementById('top-badge');
    tb.className='badge '+(rb?'on':'off');
    document.getElementById('top-txt').textContent=rb?'Running':'Stopped';

    // session warning banner
    const sw=document.getElementById('session-warn');
    if(sw) sw.style.display=d.session_warning?'flex':'none';

    const pb=document.getElementById('poster-badge');
    pb.style.display=rp?'flex':'none';
    document.getElementById('poster-dot').className='dot '+(rp?'on':'off');
    document.getElementById('poster-txt').textContent='Poster';

    // schedule badge
    const fi=d.filter_info||{};
    const now=new Date();const h=now.getHours();
    const sh=fi.active_start||7, eh=fi.active_end||23;
    const inHours=(sh<=h&&h<eh);
    const sb=document.getElementById('sched-badge');
    sb.className='topbar-schedule'+(inHours?' active':'');
    sb.textContent=`⏰ ${String(sh).padStart(2,'0')}:00–${String(eh).padStart(2,'0')}:00 `+(inHours?'✓':'zzz');

    // stat cards
    const fail=(s.comment_failures||0)+(s.post_failures||0)+(s.llm_errors||0);
    document.getElementById('s-sent').textContent=s.comments_sent??0;
    document.getElementById('s-posts').textContent=s.posts_created??0;
    document.getElementById('s-err').textContent=fail;
    document.getElementById('s-last').textContent='Terakhir: '+(s.last_comment_time||'—');
    document.getElementById('s-lastpost').textContent='Terakhir: '+(s.last_post_time||'—');
    document.getElementById('s-lasterr').textContent=s.last_error||'—';
    document.getElementById('s-mode').textContent=s.app_mode||'normal';
    const wdBadge=document.getElementById('s-wd-badge');
    const wdOn=fi.watchdog;
    wdBadge.className='wd-badge'+(wdOn?' ok':'');
    wdBadge.textContent='watchdog '+(wdOn?'✓':'off');

    // detail
    const rw=d.watcher_running;
    const wbS=document.getElementById('wbtn-start'),wbT=document.getElementById('wbtn-stop');
    if(wbS)wbS.disabled=rw; if(wbT)wbT.disabled=!rw;
    const wb2S=document.getElementById('wbtn2-start'),wb2T=document.getElementById('wbtn2-stop');
    if(wb2S)wb2S.disabled=rw; if(wb2T)wb2T.disabled=!rw;
    const bEl=document.getElementById('d-bot');bEl.textContent=rb?'Running':'Stopped';bEl.style.color=rb?'var(--green)':'var(--red)';
    const pEl=document.getElementById('d-poster');pEl.textContent=rp?'Running':'Stopped';pEl.style.color=rp?'var(--purple)':'var(--muted)';
    const wEl=document.getElementById('d-watcher');if(wEl){wEl.textContent=rw?'Running':'Stopped';wEl.style.color=rw?'var(--teal)':'var(--muted)';}
    const lEl=document.getElementById('d-like');if(lEl)lEl.textContent=fi.like_before_comment?'Aktif ✓':'Nonaktif';
    document.getElementById('d-sched').textContent=`${String(sh).padStart(2,'0')}:00–${String(eh).padStart(2,'0')}:00 ${inHours?'✓ Aktif':'(di luar jam)'}`;
    document.getElementById('d-wd').textContent=wdOn?'Aktif — auto-restart ON':'Nonaktif';
    document.getElementById('d-lcmt').textContent=s.last_comment_time||'—';
    const qEl=document.getElementById('d-quota');
    if(qEl){
      const rem=s.bot_quota_remaining;
      if(rem===null||rem===undefined){
        qEl.textContent='tanpa batas'; qEl.style.color='';
      }else{
        qEl.textContent=rem>0?`${rem} sisa (${s.bot_quota_used}/${s.bot_quota_limit})`
                             :`habis (${s.bot_quota_used}/${s.bot_quota_limit})`;
        qEl.style.color=rem>0?'':'var(--red)';
      }
    }
    const nextEl=document.getElementById('d-next');
    if(nextEl)nextEl.textContent=d.est_next||(rb?'menghitung…':'—');
    document.getElementById('d-wdr').textContent=(s.watchdog_restart||0)+'×';

    // filter info
    document.getElementById('d-age').textContent=(fi.max_age||5)+' jam';
    document.getElementById('d-itkw').textContent=(fi.it_kw||0)+' keyword';
    document.getElementById('d-lokerkw').textContent=(fi.lk_kw||0)+' keyword';

    // bot controls
    document.getElementById('btn-start').disabled=rb;
    document.getElementById('btn-stop').disabled=!rb;
    document.getElementById('pbtn-start').disabled=rp;
    document.getElementById('pbtn-stop').disabled=!rp;

    // poster page stats
    document.getElementById('p-total').textContent=s.posts_created??0;
    document.getElementById('p-last').textContent=s.last_post_time||'—';
    document.getElementById('p-interval').textContent=(fi.poster_interval||48)+' jam';
    document.getElementById('p-sched').textContent=`${String(sh).padStart(2,'0')}:00–${String(eh).padStart(2,'0')}:00`;
    document.getElementById('p-topics').textContent=(fi.poster_topics||[]).join(', ')||'—';

  }catch(e){}
}

// ── activity chart ────────────────────────────────────────────────────────
async function loadChart(){
  try{
    const data=await(await fetch('/api/activity')).json();
    const bars=document.getElementById('chart-bars');
    if(!data.length){bars.innerHTML='<div style="color:var(--muted);font-size:11px">Belum ada data aktivitas.</div>';return;}
    const max=Math.max(...data.map(d=>d.count),1);
    bars.innerHTML=data.map(d=>`
      <div class="bar" style="height:${Math.max(4,Math.round(d.count/max*56))}px">
        <div class="bar-tip">${d.hour}: ${d.count}×</div>
      </div>`).join('');
    document.getElementById('xlbl-left').textContent=data[0]?.hour||'—';
  }catch(e){}
}

// ── log terminal ──────────────────────────────────────────────────────────
const term=document.getElementById('term');
function clsLine(l){
  const t=l.toLowerCase();
  if(t.includes('berhasil')||t.includes('>> status'))return'ok';
  if(t.includes('[filter]'))return'filt';
  if(t.includes('draf ai:')||t.includes('draf post:'))return'draf';
  if(t.includes('[schedule]')||t.includes('[health]')||t.includes('[watchdog]'))return'sched';
  if(t.includes('[info]')||t.includes('[session]'))return'info';
  if(t.includes('[debug]')||t.includes('router debug')||t.includes('final debug'))return'debug';
  if(t.includes('error')||t.includes('[error]'))return'err';
  if(t.includes('warn')||t.includes('[warn]'))return'warn';
  return'def';
}
function addLine(text){
  if(!text.trim())return;
  const cls=clsLine(text);
  const d=document.createElement('div');d.className='ln '+cls;d.textContent=text;
  if(logFilt!=='all'&&cls!==logFilt)d.classList.add('hidden');
  term.appendChild(d);
  if(autoScroll)term.scrollTop=term.scrollHeight;
  while(term.children.length>1500)term.removeChild(term.firstChild);
}
function setFilt(f,el){
  logFilt=f;
  document.querySelectorAll('.fchip:not(.src-chip)').forEach(c=>c.classList.remove('on'));el.classList.add('on');
  term.querySelectorAll('.ln').forEach(ln=>{
    const c=[...ln.classList].find(x=>!['ln','hidden'].includes(x));
    ln.classList.toggle('hidden',f!=='all'&&c!==f);
  });
}
function clearTerm(){term.innerHTML=''}
function toggleAS(){autoScroll=!autoScroll;document.getElementById('asbtn').textContent='↓ Auto '+(autoScroll?'ON':'OFF')}
term.addEventListener('scroll',()=>{autoScroll=term.scrollTop+term.clientHeight>=term.scrollHeight-40;document.getElementById('asbtn').textContent='↓ Auto '+(autoScroll?'ON':'OFF')})
let _logSource='bot', _sseConn=null;
function connectSSE(source){
  source=source||_logSource;
  if(_sseConn){_sseConn.close();_sseConn=null;}
  _sseConn=new EventSource('/api/logs?source='+source);
  _sseConn.onmessage=e=>addLine(e.data);
  _sseConn.onerror=()=>{addLine('[UI] Koneksi terputus, menyambung…');_sseConn.close();setTimeout(()=>connectSSE(_logSource),3000)};
}
function switchLogSource(src,el){
  _logSource=src;
  logFilt='all';
  clearTerm();
  document.querySelectorAll('.src-chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on');
  document.querySelectorAll('.fchip').forEach(c=>{c.classList.toggle('on',c.dataset.f==='all');});
  connectSSE(src);
}

// ── comments ──────────────────────────────────────────────────────────────
async function loadComments(){
  try{
    allCmts=await(await fetch('/api/comments')).json();
    renderCmts(allCmts);
    // nav badge
    document.getElementById('nav-cmt-count').textContent=allCmts.length?allCmts.length:'';
    // overview preview
    const el=document.getElementById('ov-cmts');
    if(!allCmts.length){el.innerHTML='<div style="color:var(--muted);font-size:12px">Belum ada.</div>';return}
    el.innerHTML=allCmts.slice(0,3).map(i=>`
      <div style="padding:7px 0;border-bottom:1px solid var(--brd2)">
        <div style="font-size:11px;color:var(--muted);margin-bottom:3px">${i.caption.replace(/</g,'&lt;').substring(0,80)}…</div>
        <div style="font-size:13px">${i.reply.replace(/</g,'&lt;')}</div>
        <div style="font-size:10px;color:var(--muted2);margin-top:2px">${fmtTs(i.ts)}</div>
      </div>`).join('');
  }catch(e){}
}
let showEngOnly=false;
function renderCmts(items){
  const el=document.getElementById('cmt-list');
  const engCount=allCmts.filter(i=>i.engaged).length;
  document.getElementById('cmt-info').textContent=items.length+' komentar'+(engCount?' · '+engCount+' ⭐':'');
  if(!items.length){el.innerHTML='<div style="color:var(--muted)">Kosong.</div>';return}
  el.innerHTML=items.map(i=>`
    <div class="cmt${i.engaged?' eng':''}">
      <div class="cap"><span>${i.engaged?'⭐ ':''}📌 ${i.caption.replace(/</g,'&lt;').substring(0,90)}</span><span style="flex-shrink:0">${fmtTs(i.ts)}</span></div>
      <div class="rep">${i.reply.replace(/</g,'&lt;')}</div>
      ${(i.style||i.topic)?`<div style="margin-top:5px;display:flex;gap:4px">${i.style?`<span class="kw" style="color:var(--purple)">${i.style}</span>`:''}${i.topic?`<span class="kw" style="color:var(--muted)">${i.topic}</span>`:''}</div>`:''}
    </div>`).join('');
}
function filterCmts(){
  const q=document.getElementById('q').value.toLowerCase();
  let items=q?allCmts.filter(i=>i.caption.toLowerCase().includes(q)||i.reply.toLowerCase().includes(q)):allCmts;
  if(showEngOnly)items=items.filter(i=>i.engaged);
  renderCmts(items);
}
function toggleEngFilter(){
  showEngOnly=!showEngOnly;
  const btn=document.getElementById('eng-flt');
  btn.style.background=showEngOnly?'rgba(250,204,21,.18)':'';
  btn.style.color=showEngOnly?'var(--yellow)':'';
  btn.textContent=showEngOnly?'⭐ Engaged ×':'⭐ Engaged';
  filterCmts();
}
async function clearCache(){
  if(!confirm('Hapus semua cache komentar?'))return;
  await fetch('/api/comments/clear',{method:'POST'});
  allCmts=[];renderCmts([]);toast('Cache dihapus');
}
async function resetStats(){
  if(!confirm('Reset semua statistik ke nol?'))return;
  await fetch('/api/stats/reset',{method:'POST'});
  toast('Statistik direset');pollStatus();
}

// ── poster posts ─────────────────────────────────────────────────────────
async function loadPosterPosts(){
  try{
    const posts=await(await fetch('/api/poster/posts')).json();
    const el=document.getElementById('poster-list');
    if(!posts.length){el.innerHTML='<div style="color:var(--muted);font-size:12px">Belum ada postingan.</div>';return}
    el.innerHTML=posts.reverse().map(p=>`
      <div class="post-item"><div class="txt">${p.text.replace(/</g,'&lt;')}</div></div>`).join('');
  }catch(e){}
  loadTrending();
}

// ── settings ──────────────────────────────────────────────────────────────
async function loadSettings(){
  try{
    settings=await(await fetch('/api/settings')).json();
    const s=settings;
    document.getElementById('s-mode').value =s.app_mode||'normal';
    setSlider('s-age','rv-age',s.max_post_age_hours,5);
    setSlider('s-sh','rv-sh',s.active_start_hour,7,v=>String(v).padStart(2,'0'));
    setSlider('s-eh','rv-eh',s.active_end_hour,23,v=>String(v).padStart(2,'0'));
    setSlider('s-dmin','rv-dmin',s.delay_min_minutes,14);
    setSlider('s-dmax','rv-dmax',s.delay_max_minutes,17);
    setSlider('s-tok','rv-tok',s.max_tokens,120);
    setSlider('s-mlen','rv-mlen',s.max_comment_length,350);
    setSlider('s-bquota','rv-bquota',s.bot_daily_quota,40);
    document.getElementById('s-qbin').value=s.qoder_bin||'';
    document.getElementById('s-qmdl').value=s.qoder_model||'';
    setSlider('s-pint','rv-pint',s.poster_interval_hours,48);
    setSlider('s-pquota','rv-pquota',s.poster_daily_quota,5);
    setSlider('s-pgap','rv-pgap',s.poster_min_gap_minutes,120);
    document.getElementById('s-ppeak').value=s.poster_peak_only===false?'false':'true';
    document.getElementById('s-ptopics').value=(s.poster_topics||[]).join('\n');
    setSlider('s-qmin','rv-qmin',s.quality_min_score,6);
    setSlider('s-qretry','rv-qretry',s.quality_max_retries,2);
    setSlider('s-qneg','rv-qneg',s.quality_negative_examples,3);
    document.getElementById('s-like').value=s.like_before_comment===false?'false':'true';
    document.getElementById('s-ar').value=s.auto_reply_enabled?'true':'false';
    setSlider('s-arc','rv-arc',s.auto_reply_check_minutes,30);
    document.getElementById('s-rcls').value=s.reply_classify_enabled===false?'false':'true';
    document.getElementById('s-rbl').value=s.auto_blacklist_impolite?'true':'false';
    document.getElementById('s-wd').value=s.watchdog_enabled===false?'false':'true';
    extraIt=[...(s.it_ai_keywords_extra||[])];
    extraLk=[...(s.loker_keywords_extra||[])];
    renderKws();
    document.getElementById('s-persona').value=s.bot_persona||'';
  }catch(e){}
}
function setSlider(sid,rvid,val,def,fmt=v=>v){
  const v=val??def;
  document.getElementById(sid).value=v;
  document.getElementById(rvid).textContent=fmt(v);
}
function syncDmax(){
  const mn=parseInt(document.getElementById('s-dmin').value);
  const mx=document.getElementById('s-dmax');
  if(parseInt(mx.value)<mn){mx.value=mn;document.getElementById('rv-dmax').textContent=mn;}
}

function renderKws(){
  const it=document.getElementById('it-kws');
  const lk=document.getElementById('lk-kws');
  it.innerHTML=extraIt.length?extraIt.map((k,i)=>`<span class="kw custom">${k}<span class="kw-x" onclick="rmKw('it',${i})">×</span></span>`).join(''):'<span style="font-size:11px;color:var(--muted2)">Belum ada.</span>';
  lk.innerHTML=extraLk.length?extraLk.map((k,i)=>`<span class="kw custom">${k}<span class="kw-x" onclick="rmKw('lk',${i})">×</span></span>`).join(''):'<span style="font-size:11px;color:var(--muted2)">Belum ada.</span>';
}
function addKw(type){
  const inp=document.getElementById(type==='it'?'it-inp':'lk-inp');
  const v=inp.value.trim().toLowerCase();if(!v)return;
  if(type==='it'){if(!extraIt.includes(v))extraIt.push(v);}else{if(!extraLk.includes(v))extraLk.push(v);}
  inp.value='';renderKws();
}
function rmKw(type,idx){if(type==='it')extraIt.splice(idx,1);else extraLk.splice(idx,1);renderKws();}

async function saveAll(){
  const topics=document.getElementById('s-ptopics').value.split('\n').map(t=>t.trim()).filter(Boolean);
  const payload={
    app_mode:              document.getElementById('s-mode').value,
    max_post_age_hours:    +document.getElementById('s-age').value,
    active_start_hour:     +document.getElementById('s-sh').value,
    active_end_hour:       +document.getElementById('s-eh').value,
    delay_min_minutes:     +document.getElementById('s-dmin').value,
    delay_max_minutes:     +document.getElementById('s-dmax').value,
    max_tokens:            +document.getElementById('s-tok').value,
    max_comment_length:    +document.getElementById('s-mlen').value,
    bot_daily_quota:       +document.getElementById('s-bquota').value,
    qoder_bin:             document.getElementById('s-qbin').value.trim(),
    qoder_model:           document.getElementById('s-qmdl').value.trim(),
    poster_interval_hours: +document.getElementById('s-pint').value,
    poster_daily_quota:    +document.getElementById('s-pquota').value,
    poster_min_gap_minutes:+document.getElementById('s-pgap').value,
    poster_peak_only:      document.getElementById('s-ppeak').value==='true',
    poster_topics:         topics.length?topics:undefined,
    quality_min_score:         +document.getElementById('s-qmin').value,
    quality_max_retries:       +document.getElementById('s-qretry').value,
    quality_negative_examples: +document.getElementById('s-qneg').value,
    like_before_comment:       document.getElementById('s-like').value==='true',
    auto_reply_enabled:        document.getElementById('s-ar').value==='true',
    auto_reply_check_minutes:  +document.getElementById('s-arc').value,
    reply_classify_enabled:    document.getElementById('s-rcls').value==='true',
    auto_blacklist_impolite:   document.getElementById('s-rbl').value==='true',
    watchdog_enabled:          document.getElementById('s-wd').value==='true',
    it_ai_keywords_extra:  extraIt,
    loker_keywords_extra:  extraLk,
    bot_persona:           document.getElementById('s-persona').value.trim()||undefined,
  };
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.ok){
      toast('Pengaturan disimpan!');
      ['sm1','sm2','sm3','sm4','sm5','sm6','sm7','sm8'].forEach(id=>{const e=document.getElementById(id);if(e){e.textContent='Tersimpan ✓';setTimeout(()=>e.textContent='',3000);}});
    } else toast('Gagal: '+d.msg,'err');
  }catch(e){toast('Error: '+e,'err');}
}

// ── growth ────────────────────────────────────────────────────────────────
async function loadGrowth(){
  try{
    const d=await(await fetch('/api/growth')).json();
    const lat=d.latest||{};
    const dlt=d.delta||{};
    const hist=d.history||[];

    document.getElementById('g-fol').textContent=lat.followers??'—';
    document.getElementById('g-fwg').textContent=lat.following??'—';
    const df=dlt.followers??0;
    document.getElementById('g-delta').textContent=(df>0?'+':'')+df;
    document.getElementById('g-delta').style.color=df>0?'var(--green)':df<0?'var(--red)':'var(--muted)';
    document.getElementById('g-delta-sub').textContent=`dalam ${dlt.days||7} hari`;
    document.getElementById('g-posts').textContent=lat.posts??'—';
    document.getElementById('g-fol-sub').textContent=lat.date?'Update: '+lat.date:'Belum ada data';

    // chart
    const bars=document.getElementById('g-bars');
    if(!hist.length){bars.innerHTML='<div style="color:var(--muted);font-size:11px">Belum ada data. Snapshot akan otomatis diambil setiap jam.</div>';return;}
    const vals=hist.map(h=>h.followers||0);
    const max=Math.max(...vals)||1;
    const min=Math.min(...vals);
    const range=max-min||1;
    bars.innerHTML=hist.map(h=>{
      const pct=Math.max(4,Math.round((h.followers-min)/range*70));
      return `<div class="bar" style="height:${pct}px;background:var(--purple)"><div class="bar-tip">${h.date}: ${h.followers} followers</div></div>`;
    }).join('');
    document.getElementById('g-xlbl-l').textContent=hist[0]?.date||'—';
    document.getElementById('g-xlbl-r').textContent=hist[hist.length-1]?.date||'—';

    // table with comments correlation
    const tbl=document.getElementById('g-table');
    if(!hist.length){tbl.innerHTML='<div style="color:var(--muted)">Belum ada data.</div>';return;}
    tbl.innerHTML='<div class="tbl-scroll"><table style="width:100%;border-collapse:collapse">'
      +'<tr style="color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px"><td style="padding:4px 8px">Tanggal</td><td style="padding:4px 8px;text-align:right">Followers</td><td style="padding:4px 8px;text-align:right">±</td><td style="padding:4px 8px;text-align:right">Komentar</td></tr>'
      +[...hist].reverse().map((h,i,a)=>{
        const prev=a[i+1];
        const df=prev?(h.followers-prev.followers):0;
        const dc=df>0?'var(--green)':df<0?'var(--red)':'var(--muted)';
        const cmtCol=h.comments>0?`<span style="color:var(--purple)">${h.comments}</span>`:'<span style="color:var(--muted2)">—</span>';
        return `<tr style="border-top:1px solid var(--brd2)"><td style="padding:5px 8px;font-size:12px">${h.date}</td><td style="padding:5px 8px;text-align:right;font-weight:600">${h.followers}</td><td style="padding:5px 8px;text-align:right;color:${dc};font-weight:600">${df>0?'+':''}${df||'—'}</td><td style="padding:5px 8px;text-align:right">${cmtCol}</td></tr>`;
      }).join('')+'</table></div>';
  }catch(e){}
}

// ── poster trending ───────────────────────────────────────────────────────
async function loadTrending(){
  try{
    const d=await(await fetch('/api/poster/trending')).json();
    const el=document.getElementById('trend-list');
    const ageEl=document.getElementById('trend-age');
    if(d.ts&&ageEl){
      const mins=Math.round((Date.now()/1000-d.ts)/60);
      ageEl.textContent=mins<60?`${mins} mnt lalu`:`${Math.round(mins/60)} jam lalu`;
    }
    if(!d.posts||!d.posts.length){
      el.innerHTML='<div style="color:var(--muted);font-size:12px">Data tren muncul setelah Poster pertama kali dijalankan.</div>';return;
    }
    el.innerHTML=d.posts.map((p,i)=>`
      <div style="padding:8px 0;border-bottom:1px solid var(--brd2);display:flex;gap:10px;align-items:flex-start">
        <span style="color:var(--muted2);font-size:11px;min-width:14px">${i+1}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">@${p.username}</div>
          <div style="font-size:13px;line-height:1.4">${p.caption.replace(/</g,'&lt;').substring(0,120)}${p.caption.length>120?'…':''}</div>
          <div style="font-size:10px;color:var(--muted2);margin-top:3px">👍 ${p.likes} · 💬 ${p.replies} · engagement ${p.engagement}</div>
        </div>
      </div>`).join('');
  }catch(e){}
}

// ── settings import ───────────────────────────────────────────────────────
async function importSettings(input){
  const f=input.files[0];if(!f)return;
  try{
    const text=await f.text();
    const data=JSON.parse(text);
    const r=await fetch('/api/settings/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    if(d.ok){toast('Pengaturan berhasil di-import!');loadSettings();}
    else toast('Gagal import: '+(d.msg||'unknown'),'err');
  }catch(e){toast('File tidak valid: '+e,'err');}
  input.value='';
}

// ── watcher ───────────────────────────────────────────────────────────────
async function loadWatcher(){
  try{
    const d=await(await fetch('/api/watcher/stats')).json();
    document.getElementById('w-trackable').textContent=d.trackable??'—';
    document.getElementById('w-received').textContent=d.replies_received??'—';
    document.getElementById('w-sent').textContent=d.replies_sent??'—';
    document.getElementById('w-bl').textContent=d.auto_blacklisted??'—';
    if(d.last_cycle){
      const lc=document.getElementById('w-last-cycle');
      if(lc)lc.textContent=d.last_cycle.replace('[WATCHER]','').replace('[INFO]','').trim();
    }
    // trackable list
    const tEl=document.getElementById('w-trackable-list');
    const tl=d.trackable_list||[];
    if(!tl.length){tEl.innerHTML='<div style="color:var(--muted);font-size:12px">Belum ada komentar yang bisa dipantau dalam 24 jam terakhir.</div>';}
    else{tEl.innerHTML=tl.map(e=>`
      <div style="padding:7px 0;border-bottom:1px solid var(--brd2)">
        <div style="font-size:11px;color:var(--muted);margin-bottom:2px">${e.caption.replace(/</g,'&lt;').substring(0,80)}…</div>
        <div style="font-size:13px">${e.our_reply.replace(/</g,'&lt;')}</div>
        <div style="font-size:10px;color:var(--muted2);margin-top:3px">comment_id: ${e.comment_id||'—'} · ${fmtTs(e.ts)}</div>
      </div>`).join('');}
    // replied list
    const rEl=document.getElementById('w-replied-list');
    const rl=d.replied_list||[];
    if(!rl.length){rEl.innerHTML='<div style="color:var(--muted);font-size:12px">Belum ada balasan yang dikirim. Bot perlu komentar aktif yang mendapat balasan dulu.</div>';}
    else{rEl.innerHTML=rl.map(r=>`
      <div style="padding:7px 0;border-bottom:1px solid var(--brd2)">
        <div style="font-size:13px;color:var(--teal)">${r.text.replace(/</g,'&lt;')}</div>
        <div style="font-size:10px;color:var(--muted2);margin-top:2px">${fmtTs(r.ts)}</div>
      </div>`).join('');}
  }catch(e){}
}

// ── analytics ─────────────────────────────────────────────────────────────
async function loadAnalytics(){
  try{
    const d=await(await fetch('/api/analytics')).json();

    // stat cards
    document.getElementById('an-total').textContent=d.total??0;
    document.getElementById('an-engaged').textContent=d.engaged??0;
    document.getElementById('an-rate').textContent=d.total?Math.round((d.rate||0)*100)+'%':'—';
    const bs=d.best_style||'—';
    document.getElementById('an-best').textContent=bs;
    const bsr=d.by_style?.[bs]?.rate;
    document.getElementById('an-best-sub').textContent=bsr!=null?Math.round(bsr*100)+'% engaged':'belum cukup data';

    // style distribution bars
    const styleEl=document.getElementById('an-style-bars');
    const styles=['nanya','react','share','saran'];
    const sLabel={'nanya':'Nanya','react':'React','share':'Share','saran':'Saran'};
    const total=d.total||1;
    styleEl.innerHTML=styles.map(s=>{
      const sd=d.by_style?.[s]||{total:0,engaged:0,rate:0};
      const pct=Math.round(sd.total/total*100);
      const engPct=Math.round((sd.rate||0)*100);
      const warn=s===d.overused_style?'<span style="color:var(--orange);font-size:10px;margin-left:4px">⚠ dominan</span>':'';
      return `<div style="margin-bottom:13px">
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
          <span>${sLabel[s]}${warn}</span>
          <span style="color:var(--muted)">${sd.total}× · ${engPct}% engaged</span>
        </div>
        <div style="background:var(--surf3);border-radius:3px;height:8px;overflow:hidden">
          <div style="height:100%;width:${pct}%;background:var(--purple);border-radius:3px;transition:width .4s"></div>
        </div>
      </div>`;
    }).join('');

    // sentiment engagement bars
    const sentEl=document.getElementById('an-sent-bars');
    const sents=['frustrated','win','question','opinion','info'];
    const sentLabel={'frustrated':'Frustrasi 😤','win':'Berhasil 🎉','question':'Pertanyaan ❓','opinion':'Opini 💬','info':'Info ℹ️'};
    sentEl.innerHTML=sents.map(s=>{
      const sd=d.by_sentiment?.[s]||{total:0,engaged:0,rate:0};
      const engPct=Math.round((sd.rate||0)*100);
      const col=engPct>=40?'var(--green)':engPct>=20?'var(--yellow)':'var(--muted)';
      const isBest=s===d.best_sentiment;
      return `<div style="margin-bottom:11px">
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
          <span>${sentLabel[s]}${isBest?' ⭐':''}</span>
          <span style="color:var(--muted)">${sd.total} post · <span style="color:${col};font-weight:600">${engPct}%</span> engaged</span>
        </div>
        <div style="background:var(--surf3);border-radius:3px;height:6px;overflow:hidden">
          <div style="height:100%;width:${engPct}%;background:${col};border-radius:3px;transition:width .4s"></div>
        </div>
      </div>`;
    }).join('');

    // daily comments chart
    const dailyEl=document.getElementById('an-daily-chart');
    const dc=d.daily_comments||[];
    if(dc.length){
      const maxD=Math.max(...dc.map(x=>x.count),1);
      const dayHtml=dc.map(x=>{
        const pct=Math.round(x.count/maxD*100);
        const lbl=x.date.slice(5); // MM-DD
        return `<div style="display:flex;flex-direction:column;align-items:center;flex:1;min-width:0">
          <div style="font-size:9px;color:var(--muted);min-height:12px">${x.count||''}</div>
          <div style="background:var(--surf3);border-radius:2px 2px 0 0;height:50px;width:100%;display:flex;align-items:flex-end;overflow:hidden">
            <div style="width:100%;height:${pct}%;background:var(--teal);border-radius:2px 2px 0 0;transition:height .4s;min-height:${x.count?2:0}px"></div>
          </div>
          <div style="font-size:9px;color:var(--muted2);margin-top:2px">${lbl}</div>
        </div>`;
      }).join('');
      dailyEl.innerHTML=`<div style="display:flex;gap:3px;align-items:flex-end;padding:4px 0">${dayHtml}</div>`;
    }

    // length vs engagement stats
    const lenEl=document.getElementById('an-length-stats');
    const ls=d.length_stats||{};
    if(ls.sample_engaged>0||ls.sample_not>0){
      const diff=ls.avg_engaged-ls.avg_not_engaged;
      const tip=diff>10?`Komentar lebih panjang (~${ls.avg_engaged} kar) cenderung lebih engaged.`:
                diff<-10?`Komentar lebih pendek (~${ls.avg_engaged} kar) lebih banyak dapat balasan.`:
                `Panjang komentar tidak terlalu berpengaruh (beda ${Math.abs(diff)} kar).`;
      lenEl.innerHTML=`
        <div style="display:flex;gap:16px;margin-bottom:12px">
          <div style="text-align:center;flex:1">
            <div style="font-size:22px;font-weight:700;color:var(--green)">${ls.avg_engaged||'—'}</div>
            <div style="font-size:11px;color:var(--muted)">kar rata-rata engaged (n=${ls.sample_engaged})</div>
          </div>
          <div style="text-align:center;flex:1">
            <div style="font-size:22px;font-weight:700;color:var(--muted)">${ls.avg_not_engaged||'—'}</div>
            <div style="font-size:11px;color:var(--muted)">kar rata-rata tidak engaged (n=${ls.sample_not})</div>
          </div>
        </div>
        <div style="font-size:12px;color:var(--teal);padding:8px;background:var(--surf2);border-radius:6px">💡 ${tip}</div>`;
    } else {
      lenEl.innerHTML='<div style="color:var(--muted);font-size:12px">Butuh lebih banyak data engagement untuk analisis panjang komentar.</div>';
    }

    // peak hour chart
    const hourEl=document.getElementById('an-hour-chart');
    const byHour=d.activity_by_hour||{};
    const peaks=new Set(d.peak_hours||[]);
    const maxH=Math.max(...Object.values(byHour),1);
    const hourHtml=Array.from({length:24},(_,h)=>{
      const cnt=byHour[h]||0;
      const pct=Math.round(cnt/maxH*100);
      const isPeak=peaks.has(h);
      const col=isPeak?'var(--green)':'var(--purple)';
      return `<div style="display:flex;flex-direction:column;align-items:center;flex:1;min-width:0">
        <div style="font-size:8px;color:var(--muted);margin-bottom:1px;min-height:10px">${cnt||''}</div>
        <div style="background:var(--surf3);border-radius:2px 2px 0 0;height:60px;width:100%;display:flex;align-items:flex-end;overflow:hidden">
          <div style="width:100%;height:${pct}%;background:${col};border-radius:2px 2px 0 0;transition:height .4s;min-height:${cnt?2:0}px"></div>
        </div>
        <div style="font-size:8px;color:${isPeak?'var(--green)':'var(--muted2)'};margin-top:2px;font-weight:${isPeak?'600':'400'}">${String(h).padStart(2,'0')}</div>
      </div>`;
    }).join('');
    hourEl.innerHTML=`<div style="display:flex;gap:2px;align-items:flex-end;padding:4px 0">${hourHtml}</div>`;

    // engaged comments list
    const engEl=document.getElementById('an-engaged-list');
    const re=d.recent_engaged||[];
    if(!re.length){engEl.innerHTML='<div style="color:var(--muted);font-size:12px">Belum ada komentar yang dapat balasan. Bot perlu lebih banyak interaksi dulu.</div>';return;}
    engEl.innerHTML=re.map(e=>`
      <div style="padding:9px 0;border-bottom:1px solid var(--brd2)">
        <div style="font-size:13px;margin-bottom:4px">${e.reply.replace(/</g,'&lt;')} <span style="color:var(--yellow)">⭐</span></div>
        <div style="font-size:10px;color:var(--muted2)">${e.style} · ${e.topic||'misc'} · ${fmtTs(e.ts)}</div>
      </div>`).join('');
  }catch(e){}
}

// ── blacklist ─────────────────────────────────────────────────────────────
async function loadBlacklist(){
  try{
    const bl=await(await fetch('/api/blacklist')).json();
    document.getElementById('bl-count').textContent=bl.length+' akun';
    const el=document.getElementById('bl-list');
    if(!bl.length){el.innerHTML='<div style="color:var(--muted);font-size:12px">Kosong — belum ada akun yang di-blacklist.</div>';return;}
    el.innerHTML=bl.map(u=>`
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--brd2)">
        <span style="font-size:13px">@${u}</span>
        <button class="btn btn-ghost xs" onclick="removeBlacklist('${u}')">Hapus</button>
      </div>`).join('');
  }catch(e){}
}
async function addBlacklist(){
  const inp=document.getElementById('bl-inp');
  const u=inp.value.trim().replace(/^@/,'');
  if(!u)return;
  await fetch('/api/blacklist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})});
  inp.value='';loadBlacklist();toast('@'+u+' di-blacklist');
}
async function removeBlacklist(u){
  await fetch('/api/blacklist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})});
  loadBlacklist();toast('@'+u+' dihapus dari blacklist');
}
async function loadAutoBlacklist(){
  try{
    const cmts=await(await fetch('/api/comments')).json();
    const freq={};
    // hitung frekuensi dari history komentar (tidak ada username di cache, gunakan post_id sebagai proxy)
    document.getElementById('bl-commented').textContent=cmts.length+' komentar tersimpan';
    // Tampilkan tombol blacklist dari komentar terbaru
    const el=document.getElementById('bl-auto-list');
    el.innerHTML='<div style="font-size:11px;color:var(--muted2)">Fitur ini akan tersedia setelah bot aktif dan menyimpan riwayat username.</div>';
  }catch(e){}
}

// ── multi-account ─────────────────────────────────────────────────────────
function closeModal(id){document.getElementById(id).classList.remove('open');}
function openAddAccount(){
  ['fa-name','fa-uname','fa-sid','fa-csrf','fa-uid','fa-mid','fa-igdid'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.value='';
  });
  const m=document.getElementById('fa-msg'); if(m){m.style.display='none';}
  document.getElementById('modal-add-acct').classList.add('open');
}
async function createAccount(){
  const name   = document.getElementById('fa-name').value.trim();
  const uname  = document.getElementById('fa-uname').value.trim();
  const sid    = document.getElementById('fa-sid').value.trim();
  const csrf   = document.getElementById('fa-csrf').value.trim();
  const uid    = document.getElementById('fa-uid').value.trim();
  const mid    = document.getElementById('fa-mid').value.trim();
  const igdid  = document.getElementById('fa-igdid').value.trim();
  const msg    = document.getElementById('fa-msg');
  if(!name||!sid||!csrf||!uid){
    msg.textContent='Nama, SESSION_ID, CSRF_TOKEN, dan DS_USER_ID wajib diisi.';
    msg.style.display='block'; return;
  }
  const r = await fetch('/api/accounts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,creds:{THREADS_USERNAME:uname,THREADS_SESSION_ID:sid,
      THREADS_CSRF_TOKEN:csrf,THREADS_DS_USER_ID:uid,THREADS_MID:mid,THREADS_IG_DID:igdid}})});
  const d = await r.json();
  if(d.ok){closeModal('modal-add-acct');loadAccounts();}
  else{msg.textContent=d.msg||'Gagal';msg.style.display='block';}
}
async function deleteAcct(name){
  if(!confirm(`Hapus akun "${name}"? Semua data akan dihapus permanen.`)) return;
  const d=await(await fetch('/api/accounts/'+name+'/delete',{method:'POST'})).json();
  if(d.ok) loadAccounts();
  else alert(d.msg||'Gagal menghapus');
}
async function acctProcess(name,proc,action){
  const d=await(await fetch(`/api/accounts/${name}/${proc}/${action}`,{method:'POST'})).json();
  if(!d.ok && d.msg) console.warn(d.msg);
  loadAccounts();
}
async function openAcctSettings(name){
  const d=await(await fetch('/api/accounts/'+name+'/settings')).json();
  document.getElementById('aset-name').value=name;
  document.getElementById('aset-title').textContent='⚙ Pengaturan: '+name;
  document.getElementById('aset-persona').value=d.bot_persona||'';
  document.getElementById('aset-dmin').value=d.delay_min_minutes||14;
  document.getElementById('aset-dmax').value=d.delay_max_minutes||17;
  document.getElementById('aset-hstart').value=d.active_start_hour||7;
  document.getElementById('aset-hend').value=d.active_end_hour||23;
  document.getElementById('aset-pint').value=d.poster_interval_hours||48;
  document.getElementById('aset-topics').value=(d.poster_topics||[]).join('\n');
  document.getElementById('aset-bl').value=(d.blacklisted_accounts||[]).join('\n');
  const m=document.getElementById('aset-msg'); if(m) m.style.display='none';
  document.getElementById('modal-acct-set').classList.add('open');
}
async function saveAcctSettings(){
  const name=document.getElementById('aset-name').value;
  const payload={
    bot_persona:     document.getElementById('aset-persona').value.trim(),
    delay_min_minutes: parseInt(document.getElementById('aset-dmin').value)||14,
    delay_max_minutes: parseInt(document.getElementById('aset-dmax').value)||17,
    active_start_hour: parseInt(document.getElementById('aset-hstart').value)||7,
    active_end_hour:   parseInt(document.getElementById('aset-hend').value)||23,
    poster_interval_hours: parseFloat(document.getElementById('aset-pint').value)||48,
    poster_topics: document.getElementById('aset-topics').value.split('\n').map(s=>s.trim()).filter(Boolean),
    blacklisted_accounts: document.getElementById('aset-bl').value.split('\n').map(s=>s.trim().replace('@','')).filter(Boolean),
  };
  const d=await(await fetch('/api/accounts/'+name+'/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})).json();
  const m=document.getElementById('aset-msg');
  m.textContent=d.ok?'Tersimpan!':d.msg||'Gagal';
  m.style.color=d.ok?'var(--green)':'var(--red)';
  m.style.display='block';
}
async function loadAccounts(){
  const grid=document.getElementById('acct-grid');
  if(!grid) return;
  const accounts=await(await fetch('/api/accounts')).json();
  // accounts selalu punya minimal 1 entry (akun default)
  grid.innerHTML=accounts.map(a=>{
    const isDefault = !!a.is_default;
    const botBadge  = a.bot_running    ?`<span class="badge on"><span class="dot on"></span>Aktif</span>`:`<span class="badge off"><span class="dot off"></span>Mati</span>`;
    const postBadge = a.poster_running ?`<span class="badge on"><span class="dot on"></span>Aktif</span>`:`<span class="badge off"><span class="dot off"></span>Mati</span>`;
    const watBadge  = a.watcher_running?`<span class="badge on"><span class="dot on"></span>Aktif</span>`:`<span class="badge off"><span class="dot off"></span>Mati</span>`;

    // Akun default pakai endpoint /api/bot, akun tambahan pakai /api/accounts/{name}
    const botBtn  = isDefault
      ? (a.bot_running
          ?`<button class="btn btn-red xs" onclick="fetch('/api/bot/stop',{method:'POST'}).then(()=>loadAccounts())">■ Stop</button>`
          :`<button class="btn btn-green xs" onclick="fetch('/api/bot/start',{method:'POST'}).then(()=>loadAccounts())">▶ Start</button>`)
      : (a.bot_running
          ?`<button class="btn btn-red xs" onclick="acctProcess('${a.name}','bot','stop')">■ Stop</button>`
          :`<button class="btn btn-green xs" onclick="acctProcess('${a.name}','bot','start')">▶ Start</button>`);

    const postBtn = isDefault
      ? (a.poster_running
          ?`<button class="btn btn-red xs" onclick="fetch('/api/poster/stop',{method:'POST'}).then(()=>loadAccounts())">■ Stop</button>`
          :`<button class="btn btn-ghost xs" onclick="fetch('/api/poster/start',{method:'POST'}).then(()=>loadAccounts())">▶ Start</button>`)
      : (a.poster_running
          ?`<button class="btn btn-red xs" onclick="acctProcess('${a.name}','poster','stop')">■ Stop</button>`
          :`<button class="btn btn-ghost xs" onclick="acctProcess('${a.name}','poster','start')">▶ Start</button>`);

    const watBtn  = isDefault
      ? (a.watcher_running
          ?`<button class="btn btn-red xs" onclick="fetch('/api/watcher/stop',{method:'POST'}).then(()=>loadAccounts())">■ Stop</button>`
          :`<button class="btn btn-ghost xs" onclick="fetch('/api/watcher/start',{method:'POST'}).then(()=>loadAccounts())">▶ Start</button>`)
      : (a.watcher_running
          ?`<button class="btn btn-red xs" onclick="acctProcess('${a.name}','watcher','stop')">■ Stop</button>`
          :`<button class="btn btn-ghost xs" onclick="acctProcess('${a.name}','watcher','start')">▶ Start</button>`);

    const lastCmt = a.last_comment ? `terakhir: ${a.last_comment}` : 'belum pernah komentar';
    const defBadge = isDefault ? `<span style="font-size:9px;background:var(--purple);color:#fff;padding:1px 6px;border-radius:8px;margin-left:6px">UTAMA</span>` : '';
    const actionBtns = isDefault
      ? `<button class="btn btn-ghost xs" onclick="refreshChromeSession()">🔄 Refresh Chrome</button>
         <button class="btn btn-ghost xs" onclick="goPage('set')">⚙ Pengaturan</button>`
      : `<button class="btn btn-ghost xs" onclick="openAcctSettings('${a.name}')">⚙</button>
         <button class="btn btn-danger xs" onclick="deleteAcct('${a.name}')">✕</button>`;

    return `<div class="acct-card"${isDefault?' style="border-color:var(--purple);border-width:1px"':''}>
      <div class="acct-hdr">
        <div>
          <div class="acct-uname">@${a.username}${defBadge}</div>
          <div class="acct-label">${isDefault ? 'Akun utama (.env)' : a.name}</div>
        </div>
        <div style="display:flex;gap:4px">${actionBtns}</div>
      </div>
      <div class="acct-proc">
        <div class="proc-row"><span class="proc-lbl">Bot</span>${botBadge}${botBtn}</div>
        <div class="proc-row"><span class="proc-lbl">Poster</span>${postBadge}${postBtn}</div>
        <div class="proc-row"><span class="proc-lbl">Watcher</span>${watBadge}${watBtn}</div>
      </div>
      <div class="acct-foot">${a.comments_sent} komentar total · ${lastCmt}</div>
    </div>`;
  }).join('');
}

async function refreshChromeSession(){
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ Mengambil…';
  try {
    const d = await (await fetch('/api/session/refresh-chrome', {method:'POST'})).json();
    if(d.ok){
      btn.textContent = '✅ Berhasil';
      btn.style.color = 'var(--green)';
      // Restart bot otomatis supaya pakai cookies baru
      await fetch('/api/bot/stop',{method:'POST'});
      await new Promise(r=>setTimeout(r,800));
      await fetch('/api/bot/start',{method:'POST'});
      await loadAccounts();
      setTimeout(()=>{btn.textContent='🔄 Refresh Chrome';btn.disabled=false;btn.style.color='';},3000);
    } else {
      btn.textContent = '❌ Gagal';
      btn.style.color = 'var(--red)';
      alert(d.msg || 'Gagal refresh session');
      setTimeout(()=>{btn.textContent='🔄 Refresh Chrome';btn.disabled=false;btn.style.color='';},3000);
    }
  } catch(e){
    btn.textContent = '❌ Error';
    btn.style.color = 'var(--red)';
    setTimeout(()=>{btn.textContent='🔄 Refresh Chrome';btn.disabled=false;btn.style.color='';},3000);
  }
}

// ── Job Hunter ────────────────────────────────────────────────────────────────
async function loadJhStatus(){
  try {
    const d = await (await fetch('/api/jh/status')).json();
    const running = d.running;
    document.getElementById('jh-start-btn').style.display = running ? 'none'  : '';
    document.getElementById('jh-stop-btn').style.display  = running ? ''      : 'none';
    document.getElementById('jh-status-badge').textContent = running
      ? `● Berjalan (PID ${d.pid||'?'})` : '○ Tidak berjalan';
    document.getElementById('jh-status-badge').style.color = running ? 'var(--green)' : 'var(--muted)';
    document.getElementById('jh-stat-applied').textContent = d.job_comments_sent  ?? '0';
    document.getElementById('jh-stat-dm').textContent      = d.job_dms_sent       ?? '0';
    document.getElementById('jh-stat-fail').textContent    = d.job_comment_failures ?? '0';
    document.getElementById('jh-stat-found').textContent   = d.total_found        ?? '0';
    // Populate settings
    if(d.cv_summary !== undefined) document.getElementById('jh-cv').value        = d.cv_summary;
    if(d.interval   !== undefined) document.getElementById('jh-interval').value  = d.interval;
    if(d.scan_pages !== undefined) document.getElementById('jh-pages').value     = d.scan_pages;
    if(d.h_start    !== undefined) document.getElementById('jh-hstart').value    = d.h_start;
    if(d.h_end      !== undefined) document.getElementById('jh-hend').value      = d.h_end;
  } catch(e){ console.error('jh status', e); }
}
async function jhAction(action){
  const d = await (await fetch('/api/jh/'+action, {method:'POST'})).json();
  if(!d.ok && d.msg) alert(d.msg);
  await loadJhStatus();
}
async function saveJhSettings(){
  const payload = {
    cv_summary: document.getElementById('jh-cv').value.trim(),
    interval:   parseInt(document.getElementById('jh-interval').value),
    scan_pages: parseInt(document.getElementById('jh-pages').value),
    h_start:    parseInt(document.getElementById('jh-hstart').value),
    h_end:      parseInt(document.getElementById('jh-hend').value),
  };
  const d = await (await fetch('/api/jh/settings', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})).json();
  const m = document.getElementById('jh-save-msg');
  m.textContent = d.ok ? 'Tersimpan!' : d.msg || 'Gagal';
  m.style.color = d.ok ? 'var(--green)' : 'var(--red)';
  m.style.display = 'block';
  setTimeout(()=>m.style.display='none', 2000);
}
async function loadJhApplications(){
  const el = document.getElementById('jh-app-list');
  if(!el) return;
  try {
    const apps = await (await fetch('/api/jh/applications')).json();
    if(!apps.length){ el.innerHTML='<div style="color:var(--muted);padding:12px">Belum ada lamaran.</div>'; return; }
    el.innerHTML = `<div class="tbl-scroll"><table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="color:var(--muted);border-bottom:1px solid var(--bdr)">
        <th style="text-align:left;padding:6px 4px">Tanggal</th>
        <th style="text-align:left;padding:6px 4px">Akun</th>
        <th style="text-align:left;padding:6px 4px">Tipe</th>
        <th style="text-align:left;padding:6px 4px">Komentar</th>
        <th style="text-align:center;padding:6px 4px">DM</th>
      </tr></thead>
      <tbody>${apps.map(a=>`<tr style="border-bottom:1px solid var(--bdr)">
        <td style="padding:6px 4px;color:var(--muted)">${a.date||''}</td>
        <td style="padding:6px 4px"><a href="https://www.threads.com/@${a.username}" target="_blank" style="color:var(--purple)">@${a.username||'?'}</a></td>
        <td style="padding:6px 4px"><span style="background:var(--surf2);padding:2px 6px;border-radius:8px">${a.job_type||'?'}</span></td>
        <td style="padding:6px 4px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(a.comment||'').replace(/"/g,'&quot;')}">${a.comment||''}</td>
        <td style="padding:6px 4px;text-align:center">${a.dm_sent?'<span style="color:var(--green)">✓</span>':'<span style="color:var(--muted)">–</span>'}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
  } catch(e){ el.innerHTML='<div style="color:var(--red)">Gagal memuat.</div>'; }
}

// ── page nav with data loading ───────────────────────────────────────────
function toggleNav(){
  const nav=document.querySelector('nav');
  const ov=document.getElementById('nav-overlay');
  nav.classList.toggle('open');
  ov.classList.toggle('open');
}
function goPage(id){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  document.getElementById('nav-'+id).classList.add('active');
  // close mobile nav on page switch
  if(window.innerWidth<=768){
    document.querySelector('nav').classList.remove('open');
    document.getElementById('nav-overlay').classList.remove('open');
  }
  if(id==='cmt')loadComments();
  if(id==='set')loadSettings();
  if(id==='poster')loadPosterPosts();
  if(id==='watcher')loadWatcher();
  if(id==='akun')loadAccounts();
  if(id==='growth'){loadGrowth();}
  if(id==='analytics'){loadAnalytics();}
  if(id==='bl'){loadBlacklist();loadAutoBlacklist();}
  if(id==='jobs'){loadJhStatus();loadJhApplications();}
  if(id==='li'){loadLiStatus();}
}

// ── init ──────────────────────────────────────────────────────────────────
// Load username ke topbar
(async()=>{
  try{
    const accounts = await(await fetch('/api/accounts')).json();
    const def = accounts.find(a=>a.is_default);
    if(def && def.username){
      document.getElementById('topbar-username').textContent = '@'+def.username;
    }
  }catch(e){}
})();

// ── LinkedIn ─────────────────────────────────────────────────────────────────
let _liLogSource=null;
async function loadLiStatus(){
  try{
    const d=await(await fetch('/api/li/status')).json();
    const st=(run,id)=>{ const el=document.getElementById(id); if(!el)return;
      el.textContent=run?'Running':'Stopped'; el.style.color=run?'var(--green)':'var(--red)'; };
    st(d.commenter_running,'li-d-comm');
    st(d.poster_running,'li-d-post');
    st(d.watcher_running,'li-d-watch');
    rv('li-s-sent',    d.comments_sent??'—');
    rv('li-s-posts',   d.posts_sent??'—');
    rv('li-s-replies', d.replies_sent??'—');
    rv('li-s-errors',  d.errors??'—');
    const setBtn=(startId,stopId,running)=>{
      const s=document.getElementById(startId), t=document.getElementById(stopId);
      if(!s||!t) return;
      s.disabled=running; t.disabled=!running;
    };
    setBtn('li-comm-start','li-comm-stop',d.commenter_running);
    setBtn('li-post-start','li-post-stop',d.poster_running);
    setBtn('li-watch-start','li-watch-stop',d.watcher_running);
  }catch(e){}
}
async function liAction(proc,action){
  const d=await(await fetch('/api/li/'+proc+'/'+action,{method:'POST'})).json();
  toast(d.ok?`LinkedIn ${proc} ${action==='start'?'dimulai':'dihentikan'}`:d.msg||'Gagal', d.ok?'ok':'err');
  setTimeout(loadLiStatus,1000);
}
async function saveLiSession(){
  const li_at=document.getElementById('li-at-inp').value.trim();
  const jsessionid=document.getElementById('li-jsession-inp').value.trim();
  if(!li_at){toast('li_at wajib diisi','err');return;}
  const d=await(await fetch('/api/li/session',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({li_at,jsessionid})})).json();
  toast(d.ok?'Session disimpan!':d.msg||'Gagal',d.ok?'ok':'err');
  if(d.ok){document.getElementById('li-at-inp').value='';document.getElementById('li-jsession-inp').value='';}
}
let _liLogSrc=null, _liLogCur='commenter';
async function switchLiLog(source){
  _liLogCur=source;
  ['commenter','poster','watcher'].forEach(s=>{
    const el=document.getElementById('li-log-'+s);
    if(el) el.classList.toggle('on',s===source);
  });
  const d=await(await fetch('/api/li/logs?source='+source)).json();
  const term=document.getElementById('li-term');
  if(!term) return;
  term.innerHTML=(d.lines||[]).map(l=>`<div class="ln">${l.replace(/</g,'&lt;')}</div>`).join('');
  term.scrollTop=term.scrollHeight;
}

connectSSE();
pollStatus();loadComments();loadChart();
setInterval(pollStatus,5000);
setInterval(()=>{loadComments();loadChart();},20000);
setInterval(loadGrowth,300000); // refresh growth setiap 5 menit
</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _apply_llm_env(self, data):
        """
        Terapkan QODER_BIN / QODER_MODEL / APP_MODE dari payload settings.

        QODER_BIN dipakai llm_router sebagai argv[0] subprocess dan diwarisi ke
        proses anak lewat _launch(), jadi nilainya wajib berupa binary nyata.
        Tanpa validasi ini satu POST ke /api/settings setara remote code execution.

        Mengembalikan None kalau aman, atau pesan penolakan.
        """
        if data.get("qoder_bin"):
            ok, info = web_security.validate_executable(data["qoder_bin"])
            if not ok:
                return f"qoder_bin ditolak: {info}"
            os.environ["QODER_BIN"] = info
        if data.get("qoder_model"):
            model = str(data["qoder_model"]).strip()
            # Nilainya dipasang sebagai argv setelah "-m", jadi awalan "-" berarti
            # menyuntikkan flag tambahan ke Qoder CLI.
            if model.startswith("-"):
                return "qoder_model tidak boleh diawali '-'"
            os.environ["QODER_MODEL"] = model
        if data.get("app_mode"):
            os.environ["APP_MODE"] = str(data["app_mode"])
        return None

    def _client_token(self):
        """
        Ambil token dari Authorization: Bearer, header X-UI-Token, query ?token=,
        atau cookie ui_token.

        Cookie dan query param ikut didukung karena dashboard dipakai lewat browser:
        EventSource (SSE log) dan navigasi biasa tidak bisa mengirim header custom.
        """
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        header = self.headers.get("X-UI-Token", "")
        if header:
            return header.strip()
        if parse_qs(urlparse(self.path).query).get("token"):
            return parse_qs(urlparse(self.path).query)["token"][0].strip()
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == "ui_token":
                return value
        return None

    def _authorized(self) -> bool:
        client = self.client_address[0] if self.client_address else ""
        return web_security.is_authorized(client, self._client_token())

    def _deny(self):
        self._json(401, {
            "ok": False,
            "msg": ("Akses ditolak. Dashboard hanya menerima klien loopback, kecuali "
                    "UI_TOKEN di-set — lalu kirim header 'Authorization: Bearer <token>' "
                    "atau buka dengan ?token=<token>."),
        })

    def _body(self):
        # Payload rusak / Content-Length tidak valid tidak boleh menjatuhkan
        # koneksi — kembalikan {} supaya handler menjawab dengan JSON normal.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        if not self._authorized():
            self._deny()
            return

        path = urlparse(self.path).path

        if path == "/":
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            # Simpan token sebagai cookie supaya navigasi berikutnya dan SSE log
            # ikut terautentikasi tanpa harus menempel ?token= di setiap URL.
            qs_token = parse_qs(urlparse(self.path).query).get("token")
            if qs_token and self._authorized():
                self.send_header(
                    "Set-Cookie",
                    f"ui_token={qs_token[0]}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.end_headers(); self.wfile.write(b)

        elif path == "/api/status":
            cfg = config_loader.load()
            stats = _get_stats()
            try:
                if _DIR not in sys.path:
                    sys.path.insert(0, _DIR)
                import auto_commenter as ac
                it_kw = len(ac._IT_AI_KEYWORDS) + len(cfg.get("it_ai_keywords_extra",[]))
                lk_kw = len(ac._LOKER_KEYWORDS) + len(cfg.get("loker_keywords_extra",[]))
            except Exception:
                it_kw = lk_kw = 0
            # Session warning: error session dalam 2 jam terakhir
            session_warning = False
            last_err      = stats.get("last_error", "")
            last_err_time = stats.get("last_error_time", "")
            if any(kw in last_err.lower() for kw in ["session", "expired", "fetch fail"]):
                if last_err_time:
                    try:
                        err_dt = datetime.strptime(last_err_time, "%Y-%m-%d %H:%M:%S")
                        if (datetime.now() - err_dt).total_seconds() < 7200:
                            session_warning = True
                    except Exception:
                        pass
            # Estimasi komentar berikutnya
            est_next = ""
            if _is_alive(BOT_PID):
                last_cmt = stats.get("last_comment_time", "")
                if last_cmt:
                    try:
                        last_dt  = datetime.strptime(last_cmt, "%Y-%m-%d %H:%M:%S")
                        d_min    = int(cfg.get("delay_min_minutes", 14))
                        d_max    = int(cfg.get("delay_max_minutes", 17))
                        next_min = last_dt + timedelta(minutes=d_min)
                        next_max = last_dt + timedelta(minutes=d_max)
                        now      = datetime.now()
                        if next_max < now:
                            est_next = "sedang dalam proses…"
                        else:
                            m_min = max(0, int((next_min - now).total_seconds() / 60))
                            m_max = max(0, int((next_max - now).total_seconds() / 60))
                            est_next = f"~{m_min}–{m_max} menit lagi"
                    except Exception:
                        pass
            self._json(200, {
                "running":         _is_alive(BOT_PID),
                "poster_running":  _is_alive(POSTER_PID),
                "watcher_running": _is_alive(WATCHER_PID),
                "pid":             _read_pid(BOT_PID),
                "poster_pid":      _read_pid(POSTER_PID),
                "watcher_pid":     _read_pid(WATCHER_PID),
                "session_warning": session_warning,
                "est_next":        est_next,
                "stats":           stats,
                "filter_info": {
                    "it_kw":               it_kw,
                    "lk_kw":               lk_kw,
                    "max_age":             cfg["max_post_age_hours"],
                    "active_start":        cfg["active_start_hour"],
                    "active_end":          cfg["active_end_hour"],
                    "watchdog":            cfg["watchdog_enabled"],
                    "like_before_comment": cfg.get("like_before_comment", True),
                    "auto_reply_enabled":  cfg.get("auto_reply_enabled", False),
                    "poster_interval":     cfg["poster_interval_hours"],
                    "poster_topics":       cfg.get("poster_topics",[]),
                },
            })

        elif path == "/api/settings":
            self._json(200, config_loader.load())

        elif path == "/api/comments":
            self._json(200, _get_comments())

        elif path == "/api/activity":
            self._json(200, _get_activity())

        elif path == "/api/poster/posts":
            self._json(200, _get_poster_posts())

        elif path == "/api/growth":
            history = growth_tracker.get_history(30)
            # Enrich history dengan jumlah komentar per hari
            daily_cmts = {d["date"]: d["count"] for d in _get_daily_comments(30)}
            for snap in history:
                snap["comments"] = daily_cmts.get(snap.get("date",""), 0)
            self._json(200, {
                "history": history,
                "latest":  growth_tracker.get_latest(),
                "delta":   growth_tracker.get_delta(7),
            })

        elif path == "/api/poster/trending":
            self._json(200, _get_trending_cache())

        elif path == "/api/settings/export":
            try:
                b = json.dumps(config_loader.load(), ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="threadscope_settings.json"')
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        elif path == "/api/analytics":
            try:
                if _DIR not in sys.path:
                    sys.path.insert(0, _DIR)
                import comment_memory as cm
                import reply_cache as rc
                summary          = cm.get_summary()
                eng_stats        = cm.get_engagement_stats()
                recent_eng       = cm.get_engaged_comments(10)
                activity_by_hour = rc.get_activity_by_hour()
                peak_hours       = list(rc.get_peak_hours())
                # Length vs engagement stats
                try:
                    mp      = os.path.join(_DIR, "comment_memory.json")
                    all_mem = json.loads(Path(mp).read_text(encoding="utf-8")) if os.path.exists(mp) else []
                except Exception:
                    all_mem = []
                eng_lens    = [len(e.get("reply","")) for e in all_mem if e.get("engaged") and e.get("reply")]
                neng_lens   = [len(e.get("reply","")) for e in all_mem if not e.get("engaged") and e.get("reply")]
                avg_eng     = round(sum(eng_lens)/len(eng_lens))    if eng_lens  else 0
                avg_neng    = round(sum(neng_lens)/len(neng_lens))   if neng_lens else 0
                length_stats = {"avg_engaged": avg_eng, "avg_not_engaged": avg_neng,
                                "sample_engaged": len(eng_lens), "sample_not": len(neng_lens)}
                # Daily comments
                daily_comments = _get_daily_comments(7)
                self._json(200, {
                    "total":            summary["total"],
                    "engaged":          summary["engaged"],
                    "rate":             summary["rate"],
                    "by_style":         eng_stats.get("by_style",    {}),
                    "by_sentiment":     eng_stats.get("by_sentiment",{}),
                    "best_style":       eng_stats.get("best_style",   ""),
                    "best_sentiment":   eng_stats.get("best_sentiment",""),
                    "overused_style":   cm.get_overused_style(),
                    "overused_topic":   cm.get_overused_topic(),
                    "activity_by_hour": activity_by_hour,
                    "peak_hours":       peak_hours,
                    "length_stats":     length_stats,
                    "daily_comments":   daily_comments,
                    "recent_engaged": [
                        {"reply": e.get("reply",""), "style": e.get("style",""),
                         "topic": e.get("topic_bucket",""), "ts": e.get("ts",0)}
                        for e in recent_eng
                    ],
                })
            except Exception as exc:
                self._json(200, {"total":0,"engaged":0,"rate":0,"error":str(exc)})

        elif path == "/api/comments/export":
            try:
                items = _get_comments()
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["timestamp","datetime","caption","reply","post_id","comment_id"])
                for it in items:
                    ts = it.get("ts") or 0
                    try:
                        dt_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else ""
                    except (OSError, OverflowError):
                        dt_str = ""
                    w.writerow([ts, dt_str, it.get("caption",""), it.get("reply",""),
                                 it.get("post_id",""), it.get("comment_id","")])
                b = buf.getvalue().encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type",        "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="threadscope_comments.csv"')
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        elif path == "/api/watcher/stats":
            self._json(200, _get_watcher_stats())

        elif path == "/api/jh/status":
            self._json(200, _get_jh_status())
        elif path == "/api/jh/applications":
            self._json(200, _get_jh_applications())

        elif path == "/api/li/status":
            self._json(200, _get_li_stats())

        elif path == "/api/li/logs":
            qs     = parse_qs(urlparse(self.path).query)
            source = qs.get("source", ["commenter"])[0]
            log_map = {
                "commenter": LI_COMMENTER_LOG,
                "poster":    LI_POSTER_LOG,
                "watcher":   LI_WATCHER_LOG,
            }
            logfile = log_map.get(source, LI_COMMENTER_LOG)
            lines   = []
            try:
                with open(logfile, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-200:]
            except FileNotFoundError:
                pass
            self._json(200, {"lines": [l.rstrip("\n") for l in lines]})

        elif path == "/api/accounts":
            statuses = [_get_default_account_status()]
            statuses += [_get_account_full_status(a["name"]) for a in account_manager.list_accounts()]
            self._json(200, statuses)

        elif path.startswith("/api/accounts/") and path.endswith("/settings"):
            aname = path.split("/")[3]
            self._json(200, account_manager.get_account_config(aname))

        elif path == "/api/blacklist":
            cfg = config_loader.load()
            self._json(200, cfg.get("blacklisted_accounts", []))

        elif path == "/api/logs":
            qs     = urlparse(self.path).query
            source = (parse_qs(qs).get("source", ["bot"])[0]).lower()
            log_map = {"bot": BOT_LOG, "watcher": WATCHER_LOG, "poster": POSTER_LOG}
            log_file = log_map.get(source, BOT_LOG)
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream; charset=utf-8")
            self.send_header("Cache-Control","no-cache")
            self.send_header("X-Accel-Buffering","no")
            self.end_headers()
            try:
                if not os.path.exists(log_file):
                    open(log_file, "w").close()
            except OSError:
                pass
            try:
                with open(log_file,"r",encoding="utf-8",errors="replace") as f:
                    for line in f:
                        self.wfile.write(f"data: {line.rstrip()}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        line = f.readline()
                        if line:
                            self.wfile.write(f"data: {line.rstrip()}\n\n".encode())
                            self.wfile.flush()
                        else:
                            self.wfile.write(b": ping\n\n"); self.wfile.flush()
                            time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                pass

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._deny()
            return

        path = urlparse(self.path).path

        # ── per-account endpoints ─────────────────────────────────────────────
        if path == "/api/accounts":
            data  = self._body()
            aname = data.get("name", "").strip()
            creds = data.get("creds", {})
            self._json(200, account_manager.create_account(aname, creds))

        elif path.startswith("/api/accounts/") and len(path.split("/")) >= 4:
            parts  = path.split("/")   # ['','api','accounts','name','proc','action']
            aname  = parts[3]
            action = "/".join(parts[4:]) if len(parts) > 4 else ""
            # aname datang mentah dari URL. Tanpa validasi ini, "/api/accounts/../delete"
            # membuat apath menunjuk root project sehingga _terminate di bawah mematikan
            # bot asli dan delete_account() menjalankan shutil.rmtree() pada seluruh repo.
            apath = account_manager.account_path(aname)
            if apath is None:
                self._json(400, {"ok": False, "msg": "Nama akun tidak valid"})
                return
            aenv   = account_manager.get_account_env(aname)

            if action == "delete":
                # Hentikan semua proses akun sebelum hapus
                for pf in ["bot.pid", "poster.pid", "watcher.pid"]:
                    _terminate(os.path.join(apath, pf))
                self._json(200, account_manager.delete_account(aname))

            elif action == "bot/start":
                self._json(200, _launch(BOT_SCRIPT,
                    os.path.join(apath, "bot.log"), os.path.join(apath, "bot.pid"), extra_env=aenv))
            elif action == "bot/stop":
                self._json(200, _terminate(os.path.join(apath, "bot.pid")))

            elif action == "poster/start":
                r = _launch(POSTER_SCRIPT,
                    os.path.join(apath, "poster.log"), os.path.join(apath, "poster.pid"), extra_env=aenv)
                if r.get("ok"):
                    account_manager.save_account_config(aname, {"poster_enabled": True})
                self._json(200, r)
            elif action == "poster/stop":
                account_manager.save_account_config(aname, {"poster_enabled": False})
                self._json(200, _terminate(os.path.join(apath, "poster.pid")))

            elif action == "watcher/start":
                self._json(200, _launch(WATCHER_SCRIPT,
                    os.path.join(apath, "watcher.log"), os.path.join(apath, "watcher.pid"), extra_env=aenv))
            elif action == "watcher/stop":
                self._json(200, _terminate(os.path.join(apath, "watcher.pid")))

            elif action == "settings":
                data = self._body()
                self._json(200, {"ok": account_manager.save_account_config(aname, data)})

            else:
                self._json(404, {"error": "not found"})

        elif path == "/api/bot/start":
            self._json(200, _launch(BOT_SCRIPT, BOT_LOG, BOT_PID))
        elif path == "/api/bot/stop":
            self._json(200, _terminate(BOT_PID))
        elif path == "/api/poster/start":
            result = _launch(POSTER_SCRIPT, POSTER_LOG, POSTER_PID)
            if result.get("ok"):
                config_loader.save({"poster_enabled": True})
            self._json(200, result)
        elif path == "/api/poster/stop":
            config_loader.save({"poster_enabled": False})
            self._json(200, _terminate(POSTER_PID))
        elif path == "/api/watcher/start":
            self._json(200, _launch(WATCHER_SCRIPT, WATCHER_LOG, WATCHER_PID))
        elif path == "/api/watcher/stop":
            self._json(200, _terminate(WATCHER_PID))

        elif path == "/api/jh/start":
            self._json(200, _launch(JH_SCRIPT, JH_LOG, JH_PID))
        elif path == "/api/jh/stop":
            self._json(200, _terminate(JH_PID))
        elif path == "/api/jh/settings":
            data = self._body()
            saves = {}
            if data.get("cv_summary") is not None:
                saves["job_hunter_cv_summary"] = data["cv_summary"]
            if data.get("interval"):
                saves["job_hunter_scan_interval_minutes"] = int(data["interval"])
            if data.get("scan_pages"):
                saves["job_hunter_scan_pages"] = max(1, min(20, int(data["scan_pages"])))
            if data.get("h_start") is not None:
                saves["job_hunter_active_start_hour"] = int(data["h_start"])
            if data.get("h_end") is not None:
                saves["job_hunter_active_end_hour"] = int(data["h_end"])
            config_loader.save(saves)
            self._json(200, {"ok": True})

        elif path == "/api/session/refresh-chrome":
            try:
                from services.extract_chrome_cookies import extract_and_update
                ok = extract_and_update(verbose=False)
                if ok:
                    self._json(200, {"ok": True, "msg": "Session berhasil diperbarui dari Chrome. Restart bot untuk pakai cookies baru."})
                else:
                    self._json(200, {"ok": False, "msg": "Gagal ambil cookies dari Chrome. Pastikan sudah login Threads di Chrome Default profile."})
            except Exception as e:
                self._json(200, {"ok": False, "msg": f"Error: {e}"})

        elif path == "/api/li/commenter/start":
            self._json(200, _launch(LI_COMMENTER_SCRIPT, LI_COMMENTER_LOG, LI_COMMENTER_PID))
        elif path == "/api/li/commenter/stop":
            self._json(200, _terminate(LI_COMMENTER_PID))
        elif path == "/api/li/poster/start":
            result = _launch(LI_POSTER_SCRIPT, LI_POSTER_LOG, LI_POSTER_PID)
            if result.get("ok"):
                config_loader.save({"linkedin_poster_enabled": True})
            self._json(200, result)
        elif path == "/api/li/poster/stop":
            config_loader.save({"linkedin_poster_enabled": False})
            self._json(200, _terminate(LI_POSTER_PID))
        elif path == "/api/li/watcher/start":
            self._json(200, _launch(LI_WATCHER_SCRIPT, LI_WATCHER_LOG, LI_WATCHER_PID))
        elif path == "/api/li/watcher/stop":
            self._json(200, _terminate(LI_WATCHER_PID))

        elif path == "/api/li/session":
            data       = self._body()
            li_at      = (data.get("li_at") or "").strip()
            jsessionid = (data.get("jsessionid") or "").strip()
            if not li_at:
                self._json(200, {"ok": False, "msg": "li_at wajib diisi"})
            else:
                li_session = {"li_at": li_at}
                if jsessionid:
                    li_session["JSESSIONID"] = jsessionid
                sp = os.path.join(_DIR, "linkedin_session.json")
                try:
                    Path(sp).write_text(json.dumps(li_session, indent=2), encoding="utf-8")
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(200, {"ok": False, "msg": str(e)})

        elif path == "/api/settings":
            data = self._body()
            if not isinstance(data, dict):
                self._json(400, {"ok": False, "msg": "payload harus berupa JSON object"})
                return
            reject = self._apply_llm_env(data)
            if reject:
                self._json(400, {"ok": False, "msg": reject})
                return
            self._json(200, {"ok": config_loader.save(data)})

        elif path == "/api/settings/import":
            try:
                data = self._body()
                if not isinstance(data, dict):
                    self._json(400, {"ok": False, "msg": "payload harus berupa JSON object"})
                else:
                    reject = self._apply_llm_env(data)
                    if reject:
                        self._json(400, {"ok": False, "msg": reject})
                        return
                    self._json(200, {"ok": config_loader.save(data)})
            except Exception as exc:
                self._json(400, {"ok": False, "msg": str(exc)})

        elif path == "/api/blacklist/add":
            data = self._body()
            username = (data.get("username") or "").strip().lstrip("@").lower()
            if username:
                cfg = config_loader.load()
                bl  = cfg.get("blacklisted_accounts", [])
                if username not in bl:
                    bl.append(username)
                    config_loader.save({"blacklisted_accounts": bl})
                self._json(200, {"ok": True, "blacklist": bl})
            else:
                self._json(200, {"ok": False, "msg": "username kosong"})

        elif path == "/api/blacklist/remove":
            data = self._body()
            username = (data.get("username") or "").strip().lstrip("@").lower()
            cfg = config_loader.load()
            # Cocokkan case-insensitive: jalur auto-blacklist di reply_watcher
            # menyimpan username apa adanya, jadi entri bisa berhuruf kapital.
            bl  = [u for u in cfg.get("blacklisted_accounts", [])
                   if str(u).strip().lstrip("@").lower() != username]
            config_loader.save({"blacklisted_accounts": bl})
            self._json(200, {"ok": True, "blacklist": bl})

        elif path == "/api/comments/clear":
            cp = os.getenv("REPLY_CACHE_FILE", os.path.join(_DIR,"reply_cache.json"))
            try:
                Path(cp).write_text("{}", encoding="utf-8")
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(200, {"ok": False, "msg": str(e)})

        elif path == "/api/stats/reset":
            sp = os.path.join(_DIR, "stats.json")
            try:
                import stats_tracker
                Path(sp).write_text(json.dumps(stats_tracker.DEFAULT_STATS, indent=2), encoding="utf-8")
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(200, {"ok": False, "msg": str(e)})

        else:
            self._json(404, {"error": "not found"})


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        import stats_tracker
        stats_tracker.init_system_start()
    except Exception:
        pass
    bind_host = web_security.resolve_bind_host()
    requested = os.getenv("UI_HOST", "").strip()
    if requested and requested != bind_host:
        print(f"[SECURITY] UI_HOST={requested} diabaikan — dashboard tetap di {bind_host}. "
              f"Untuk membuka ke jaringan, set UI_TOKEN dulu.")
    server = ThreadingHTTPServer((bind_host, PORT), Handler)
    if bind_host == "127.0.0.1":
        print(f"ThreadScope Dashboard  →  http://localhost:{PORT}   (loopback saja)")
        if os.getenv("UI_TOKEN", "").strip():
            print("[SECURITY] UI_TOKEN aktif — kirim 'Authorization: Bearer <token>' atau buka ?token=<token>")
    else:
        print(f"ThreadScope Dashboard  →  http://{bind_host}:{PORT}   "
              f"(TERBUKA ke jaringan — token WAJIB, jangan di WiFi publik)")
    print("Ctrl+C untuk berhenti.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMenghentikan semua proses…")
        _terminate(BOT_PID)
        _terminate(POSTER_PID)
        _terminate(WATCHER_PID)
        _terminate(JH_PID)
        _terminate(LI_COMMENTER_PID)
        _terminate(LI_POSTER_PID)
        _terminate(LI_WATCHER_PID)
