"""
web_ui.py -- ThreadScope Dashboard v3 (pure stdlib).
Jalankan: python3 web_ui.py  →  http://localhost:5000
"""
import json, os, signal, subprocess, sys, time, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

import config_loader

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
    return _read_pid(pidfile) is not None

def _launch(script, logfile, pidfile):
    if _is_alive(pidfile):
        return {"ok": False, "msg": "Sudah berjalan"}
    open(logfile, "w").close()
    with open(logfile, "ab") as lf:
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=lf, stderr=subprocess.STDOUT, cwd=_DIR,
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
    return {"ok": True}

# ── watchdog thread ───────────────────────────────────────────────────────────
_watchdog_restart_count = 0

def _watchdog_loop():
    global _watchdog_restart_count
    while True:
        time.sleep(30)
        cfg = config_loader.load()
        if not cfg.get("watchdog_enabled", True):
            continue
        # Restart commenter if PID file exists but process died
        if Path(BOT_PID).exists() and not _is_alive(BOT_PID):
            Path(BOT_PID).unlink(missing_ok=True)
            _launch(BOT_SCRIPT, BOT_LOG, BOT_PID)
            _watchdog_restart_count += 1
            import stats_tracker
            stats_tracker.increment("watchdog_restarts")
            with open(BOT_LOG, "a") as f:
                f.write(f"\n[WATCHDOG] Bot di-restart otomatis (total: {_watchdog_restart_count}×)\n")
        # Restart poster if enabled and crashed
        if cfg.get("poster_enabled") and Path(POSTER_PID).exists() and not _is_alive(POSTER_PID):
            Path(POSTER_PID).unlink(missing_ok=True)
            _launch(POSTER_SCRIPT, POSTER_LOG, POSTER_PID)
        # Restart watcher if enabled and crashed
        if cfg.get("auto_reply_enabled") and Path(WATCHER_PID).exists() and not _is_alive(WATCHER_PID):
            Path(WATCHER_PID).unlink(missing_ok=True)
            _launch(WATCHER_SCRIPT, WATCHER_LOG, WATCHER_PID)

threading.Thread(target=_watchdog_loop, daemon=True).start()

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
    s["watchdog_restart"]= _watchdog_restart_count
    return s

def _get_comments():
    cp = os.getenv("REPLY_CACHE_FILE", os.path.join(_DIR, "reply_cache.json"))
    try:
        cache = json.loads(Path(cp).read_text(encoding="utf-8")) if os.path.exists(cp) else {}
    except Exception:
        cache = {}
    items = [{"caption": k, "reply": v.get("reply",""), "ts": v.get("timestamp",0)}
             for k, v in cache.items()]
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
.cmt{background:var(--surf);border:1px solid var(--brd);border-radius:8px;padding:11px 13px}
.cmt .cap{font-size:11px;color:var(--muted);margin-bottom:5px;display:flex;justify-content:space-between;gap:8px}
.cmt .cap span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmt .rep{font-size:13px;line-height:1.5}
.cmt .meta{font-size:10px;color:var(--muted2);margin-top:5px}

/* settings */
.set-layout{display:grid;grid-template-columns:172px 1fr;flex:1;overflow:hidden}
.set-nav{border-right:1px solid var(--brd);padding:10px 8px;display:flex;flex-direction:column;gap:1px}
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
</style>
</head>
<body>

<!-- topbar -->
<div class="topbar">
  <div class="logo">Thread<em>Scope</em></div>
  <div class="badge off" id="top-badge"><div class="dot off" id="top-dot"></div><span id="top-txt">Stopped</span></div>
  <div class="badge off" id="poster-badge" style="display:none"><div class="dot off" id="poster-dot"></div><span id="poster-txt">Poster</span></div>
  <div class="spacer"></div>
  <div class="topbar-right">
    <div class="topbar-schedule" id="sched-badge">⏰ —</div>
    <div class="topbar-time" id="clock"></div>
  </div>
</div>

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
    <button class="btn btn-ghost" onclick="loadComments()">↻</button>
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
        <input type="range" class="fi" id="s-pint" min="12" max="168" value="48" oninput="rv('rv-pint',this.value)">
        <div class="hint">Jeda antara satu postingan ke postingan berikutnya.</div>
      </div>
      <div class="fg">
        <label>Topik (satu per baris)</label>
        <textarea class="fi" id="s-ptopics" rows="6" style="resize:vertical;line-height:1.6"></textarea>
        <div class="hint">Bot akan memilih satu topik secara acak tiap kali posting.</div>
      </div>
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm6"></span></div>
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
      <div class="save-bar"><button class="btn btn-blue" onclick="saveAll()">Simpan</button><span class="save-ok" id="sm7"></span></div>
    </div>

  </div>
</div>
</div>

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
function goPage(id){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  document.getElementById('nav-'+id).classList.add('active');
  if(id==='cmt')loadComments();
  if(id==='set')loadSettings();
  if(id==='poster')loadPosterPosts();
}
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
    const bEl=document.getElementById('d-bot');bEl.textContent=rb?'Running':'Stopped';bEl.style.color=rb?'var(--green)':'var(--red)';
    const pEl=document.getElementById('d-poster');pEl.textContent=rp?'Running':'Stopped';pEl.style.color=rp?'var(--purple)':'var(--muted)';
    const wEl=document.getElementById('d-watcher');if(wEl){wEl.textContent=rw?'Running':'Stopped';wEl.style.color=rw?'var(--teal)':'var(--muted)';}
    const lEl=document.getElementById('d-like');if(lEl)lEl.textContent=fi.like_before_comment?'Aktif ✓':'Nonaktif';
    document.getElementById('d-sched').textContent=`${String(sh).padStart(2,'0')}:00–${String(eh).padStart(2,'0')}:00 ${inHours?'✓ Aktif':'(di luar jam)'}`;
    document.getElementById('d-wd').textContent=wdOn?'Aktif — auto-restart ON':'Nonaktif';
    document.getElementById('d-lcmt').textContent=s.last_comment_time||'—';
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
  document.querySelectorAll('.fchip').forEach(c=>c.classList.remove('on'));el.classList.add('on');
  term.querySelectorAll('.ln').forEach(ln=>{
    const c=[...ln.classList].find(x=>!['ln','hidden'].includes(x));
    ln.classList.toggle('hidden',f!=='all'&&c!==f);
  });
}
function clearTerm(){term.innerHTML=''}
function toggleAS(){autoScroll=!autoScroll;document.getElementById('asbtn').textContent='↓ Auto '+(autoScroll?'ON':'OFF')}
term.addEventListener('scroll',()=>{autoScroll=term.scrollTop+term.clientHeight>=term.scrollHeight-40;document.getElementById('asbtn').textContent='↓ Auto '+(autoScroll?'ON':'OFF')})
function connectSSE(){
  const es=new EventSource('/api/logs');
  es.onmessage=e=>addLine(e.data);
  es.onerror=()=>{addLine('[UI] Koneksi terputus, menyambung…');es.close();setTimeout(connectSSE,3000)};
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
function renderCmts(items){
  const el=document.getElementById('cmt-list');
  document.getElementById('cmt-info').textContent=items.length+' komentar';
  if(!items.length){el.innerHTML='<div style="color:var(--muted)">Kosong.</div>';return}
  el.innerHTML=items.map(i=>`
    <div class="cmt">
      <div class="cap"><span>📌 ${i.caption.replace(/</g,'&lt;').substring(0,90)}</span><span style="flex-shrink:0">${fmtTs(i.ts)}</span></div>
      <div class="rep">${i.reply.replace(/</g,'&lt;')}</div>
    </div>`).join('');
}
function filterCmts(){
  const q=document.getElementById('q').value.toLowerCase();
  renderCmts(q?allCmts.filter(i=>i.caption.toLowerCase().includes(q)||i.reply.toLowerCase().includes(q)):allCmts);
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
    document.getElementById('s-qbin').value=s.qoder_bin||'';
    document.getElementById('s-qmdl').value=s.qoder_model||'';
    setSlider('s-pint','rv-pint',s.poster_interval_hours,48);
    document.getElementById('s-ptopics').value=(s.poster_topics||[]).join('\n');
    document.getElementById('s-like').value=s.like_before_comment===false?'false':'true';
    document.getElementById('s-ar').value=s.auto_reply_enabled?'true':'false';
    setSlider('s-arc','rv-arc',s.auto_reply_check_minutes,30);
    document.getElementById('s-wd').value=s.watchdog_enabled===false?'false':'true';
    extraIt=[...(s.it_ai_keywords_extra||[])];
    extraLk=[...(s.loker_keywords_extra||[])];
    renderKws();
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
    qoder_bin:             document.getElementById('s-qbin').value.trim(),
    qoder_model:           document.getElementById('s-qmdl').value.trim(),
    poster_interval_hours: +document.getElementById('s-pint').value,
    poster_topics:         topics.length?topics:undefined,
    like_before_comment:       document.getElementById('s-like').value==='true',
    auto_reply_enabled:        document.getElementById('s-ar').value==='true',
    auto_reply_check_minutes:  +document.getElementById('s-arc').value,
    watchdog_enabled:          document.getElementById('s-wd').value==='true',
    it_ai_keywords_extra:  extraIt,
    loker_keywords_extra:  extraLk,
  };
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.ok){
      toast('Pengaturan disimpan!');
      ['sm1','sm2','sm3','sm4','sm5','sm6','sm7'].forEach(id=>{const e=document.getElementById(id);if(e){e.textContent='Tersimpan ✓';setTimeout(()=>e.textContent='',3000);}});
    } else toast('Gagal: '+d.msg,'err');
  }catch(e){toast('Error: '+e,'err');}
}

// ── init ──────────────────────────────────────────────────────────────────
connectSSE();
pollStatus();loadComments();loadChart();
setInterval(pollStatus,5000);
setInterval(()=>{loadComments();loadChart();},20000);
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

    def _body(self):
        n = int(self.headers.get("Content-Length",0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        elif path == "/api/status":
            cfg = config_loader.load()
            stats = _get_stats()
            try:
                sys.path.insert(0, _DIR)
                import auto_commenter as ac
                it_kw = len(ac._IT_AI_KEYWORDS) + len(cfg.get("it_ai_keywords_extra",[]))
                lk_kw = len(ac._LOKER_KEYWORDS) + len(cfg.get("loker_keywords_extra",[]))
            except Exception:
                it_kw = lk_kw = 0
            self._json(200, {
                "running":         _is_alive(BOT_PID),
                "poster_running":  _is_alive(POSTER_PID),
                "watcher_running": _is_alive(WATCHER_PID),
                "pid":             _read_pid(BOT_PID),
                "poster_pid":      _read_pid(POSTER_PID),
                "watcher_pid":     _read_pid(WATCHER_PID),
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

        elif path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream; charset=utf-8")
            self.send_header("Cache-Control","no-cache")
            self.send_header("X-Accel-Buffering","no")
            self.end_headers()
            if not os.path.exists(BOT_LOG): open(BOT_LOG,"w").close()
            try:
                with open(BOT_LOG,"r",encoding="utf-8",errors="replace") as f:
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
        path = urlparse(self.path).path

        if   path == "/api/bot/start":     self._json(200, _launch(BOT_SCRIPT, BOT_LOG, BOT_PID))
        elif path == "/api/bot/stop":      self._json(200, _terminate(BOT_PID))
        elif path == "/api/poster/start":  self._json(200, _launch(POSTER_SCRIPT, POSTER_LOG, POSTER_PID))
        elif path == "/api/poster/stop":   self._json(200, _terminate(POSTER_PID))
        elif path == "/api/watcher/start": self._json(200, _launch(WATCHER_SCRIPT, WATCHER_LOG, WATCHER_PID))
        elif path == "/api/watcher/stop":  self._json(200, _terminate(WATCHER_PID))

        elif path == "/api/settings":
            data = self._body()
            if data.get("qoder_bin"):   os.environ["QODER_BIN"]   = data["qoder_bin"]
            if data.get("qoder_model"): os.environ["QODER_MODEL"] = data["qoder_model"]
            if data.get("app_mode"):    os.environ["APP_MODE"]    = data["app_mode"]
            self._json(200, {"ok": config_loader.save(data)})

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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"ThreadScope Dashboard  →  http://localhost:{PORT}")
    print("Ctrl+C untuk berhenti.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMenghentikan semua proses…")
        _terminate(BOT_PID)
        _terminate(POSTER_PID)
