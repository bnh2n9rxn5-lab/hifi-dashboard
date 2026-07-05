#!/usr/bin/env python3
"""
HiFi Dashboard — a local web control panel for the iMac PA system.

Talks to the minidsp daemon (via the `minidsp` CLI) and Apple Music (via osascript).
No third-party dependencies; uses the Python stdlib only.

  Run:   python3 server.py            # binds 0.0.0.0:8765 (reachable from the phone on LAN)
  Open:  http://27-iMac.local:8765    # from the iPhone / any LAN device

Env:
  DSP_WEB_PORT   override port (default 8765)
  DSP_WEB_TOKEN  if set, every request must include ?t=<token> (or X-Token header)

Design notes / constraints honoured:
  - Master VOLUME = Apple Music app volume (0-100), NOT miniDSP gain. The miniDSP master
    gain is a fixed mains/sub balance trim and is never touched here.
  - All shell-outs use arg lists (no shell=True), so playlist/artist names are injection-safe.
  - The Meross plug is HomeKit-only and not controllable from here; "Stop" mutes + pauses,
    the full power-down stays a phone Shortcut.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import nexia  # Nexia PM sub-bus control (telnet, stdlib) — see nexia.py

PORT = int(os.environ.get("DSP_WEB_PORT", "8765"))
TOKEN = os.environ.get("DSP_WEB_TOKEN", "")
APP_VERSION = "v5"  # bump on any served-page change; stale clients auto-reload on mismatch (see poll())
MINIDSP = "/usr/local/bin/minidsp"
BINDIR = "/usr/local/bin"
HERE = os.path.dirname(os.path.abspath(__file__))

# Quick-volume targets (match the dsp-loud/-medium/-quiet helpers)
VOL_QUICK = {"quiet": 35, "medium": 60, "loud": 85}

# ---- sub / DSP coupling --------------------------------------------------
# The subs live on the Nexia PM (separate DSP), so they aren't part of the
# miniDSP preset. To make them "responsive to the miniDSP UI", we mirror two
# things onto the Nexia whenever the miniDSP state changes (from the dashboard,
# the phone, or the physical unit):
#   - a global mute mutes the subs too;
#   - switching preset recalls that preset's sub level (a scene trim).
# Manual sub-slider moves hold until the next preset change. Tune these freely.
SUB_FOLLOW_PRESET = True
# The Nexia output block rejects boost (>0 dB), so trims are anchored with the
# hottest preset (EDM) at 0 and the rest cut relative to it — same spacing as
# the old {0,+3,+2,-8} intent, shifted -3 to fit the device's cut-only range.
PRESET_SUB_TRIM = {0: -3.0, 1: 0.0, 2: -1.0, 3: -11.0}  # Flat / EDM / Movies / Late Night, dB

_coupled = {"preset": None, "mute": None}  # last miniDSP state we mirrored
_couple_lock = threading.Lock()

# ---- shell helpers -------------------------------------------------------

def run(args, timeout=20):
    """Run an arg list, return (rc, stdout, stderr). Never raises on non-zero."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def osa(script, timeout=20):
    return run(["osascript", "-e", script], timeout=timeout)


# ---- status parsing ------------------------------------------------------

STATUS_RE = re.compile(
    r"preset:\s*(\d+).*?source:\s*(\w+).*?volume:\s*Gain\(([-\d.]+)\).*?mute:\s*(true|false)",
    re.S,
)


def db_to_pct(db):
    # map -60..0 dB -> 0..100 (anything below -60 reads as silence)
    return max(0.0, min(100.0, (db + 60.0) / 60.0 * 100.0))


def dsp_status():
    rc, out, err = run([MINIDSP], timeout=8)
    if rc != 0:
        return {"ok": False, "error": (err or out or "minidsp failed").strip()}
    m = STATUS_RE.search(out)
    data = {"ok": True, "inputs": [], "outputs": []}
    if m:
        data["preset"] = int(m.group(1))
        data["source"] = m.group(2)
        data["master_gain"] = float(m.group(3))
        data["mute"] = (m.group(4) == "true")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Input levels:"):
            data["inputs"] = [float(x) for x in re.findall(r"-?\d+\.?\d*", line)]
        elif line.startswith("Output levels:"):
            data["outputs"] = [float(x) for x in re.findall(r"-?\d+\.?\d*", line)]
    data["input_pct"] = [db_to_pct(x) for x in data["inputs"]]
    data["output_pct"] = [db_to_pct(x) for x in data["outputs"]]
    return data


# Kept in two pieces on purpose: the "is Music running" guard lives in its own
# osascript call (via System Events) so we never auto-launch Music just to poll
# status, and so the Music tell-block below stays simple enough to compile cleanly
# (combining the two tell-contexts + an `as text` coercion broke the parser).
NP_SCRIPT = '''tell application "Music"
    set ps to "stopped"
    if player state is playing then set ps to "playing"
    if player state is paused then set ps to "paused"
    set t to ""
    set a to ""
    set al to ""
    set pos to 0
    set dur to 0
    try
        set t to name of current track
        set a to artist of current track
        set al to album of current track
        set dur to duration of current track
    end try
    try
        set pp to player position
        if pp is not missing value then set pos to pp
    end try
    return ps & "|" & t & "|" & a & "|" & al & "|" & pos & "|" & dur
end tell'''


def music_running():
    rc, out, _ = osa('tell application "System Events" to (exists process "Music")', timeout=8)
    return out.strip() == "true"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def now_playing():
    empty = {"state": "notrunning", "title": "", "artist": "", "album": "",
             "position": 0.0, "duration": 0.0}
    if not music_running():
        return empty
    rc, out, err = osa(NP_SCRIPT, timeout=10)
    if rc != 0:
        return dict(empty, state="unknown")
    parts = (out.strip().split("|") + [""] * 6)[:6]
    return {"state": parts[0], "title": parts[1], "artist": parts[2], "album": parts[3],
            "position": _num(parts[4]), "duration": _num(parts[5])}


def music_volume():
    rc, out, _ = osa('tell application "Music" to get sound volume', timeout=8)
    try:
        return int(out.strip())
    except ValueError:
        return None


def couple_to_dsp(d):
    """Mirror miniDSP preset/mute transitions onto the Nexia subs.

    Transition-based: only writes to the Nexia when the observed miniDSP value
    actually changes, so a steady-state poll costs nothing and a manual sub move
    is only overridden by a genuine preset change. The first observation after
    start just records state (no sub jump when the server restarts).
    """
    if not d.get("ok"):
        return
    now = time.time()
    preset, mute = d.get("preset"), d.get("mute")
    # Claim the transition under a lock BEFORE the slow (~2.4s) Nexia write —
    # otherwise every poll thread that piles up meanwhile sees the same stale
    # _coupled and fires a duplicate write (observed: 4x for one change).
    with _couple_lock:
        first = _coupled["preset"] is None and _coupled["mute"] is None
        old_preset, old_mute = _coupled["preset"], _coupled["mute"]
        if preset is not None:
            _coupled["preset"] = preset
        if mute is not None:
            _coupled["mute"] = mute
    if first:
        return
    if (SUB_FOLLOW_PRESET and preset is not None and preset != old_preset
            and preset in PRESET_SUB_TRIM):
        nexia.set_level(PRESET_SUB_TRIM[preset], now,
                        src="couple preset %s->%s" % (old_preset, preset))
    if mute is not None and mute != old_mute:
        nexia.set_mute(mute, now,
                       src="couple mute %s->%s" % (old_mute, mute))


def full_status():
    d = dsp_status()
    d["now"] = now_playing()
    d["volume"] = music_volume()
    couple_to_dsp(d)
    d["sub"] = nexia.status(time.time())
    d["version"] = APP_VERSION
    return d


# ---- music lists (for the pickers) --------------------------------------

_LIST_CACHE = {}


def helper_lines(name):
    if name in _LIST_CACHE:
        return _LIST_CACHE[name]
    rc, out, _ = run([os.path.join(BINDIR, name)], timeout=30)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    _LIST_CACHE[name] = lines
    return lines


# ---- actions -------------------------------------------------------------

def act_preset(n):
    return run([MINIDSP, "config", str(n)])


def act_mute(on):
    return run([MINIDSP, "mute", "on" if on else "off"])


def act_volume(v):
    v = max(0, min(100, int(v)))
    return osa('tell application "Music" to set sound volume to %d' % v)


def act_transport(cmd):
    mp = {
        "play": "play", "pause": "pause", "playpause": "playpause",
        "next": "next track", "prev": "previous track",
    }
    if cmd not in mp:
        return (2, "", "bad transport cmd")
    return osa('tell application "Music" to %s' % mp[cmd])


def act_seek(pos):
    # Absolute scrub: set Apple Music's player position (seconds from track start).
    try:
        v = max(0, int(float(pos)))
    except (TypeError, ValueError):
        return (2, "", "bad seek position")
    return osa('tell application "Music" to set player position to %d' % v)


def act_start():
    # dsp-start-listening retries config + unmutes (~25s worst case); run detached.
    try:
        subprocess.Popen([os.path.join(BINDIR, "dsp-start-listening")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (0, "starting", "")
    except Exception as e:
        return (1, "", str(e))


def act_stop():
    osa('tell application "Music" to pause')
    return act_mute(True)


def act_play(name):
    return run([os.path.join(BINDIR, "dsp-play"), name], timeout=30)


def act_play_genre(name):
    return run([os.path.join(BINDIR, "dsp-play-genre"), name], timeout=30)


def act_play_artist(name):
    return run([os.path.join(BINDIR, "dsp-play-artist"), name], timeout=30)


def act_faves():
    return run([os.path.join(BINDIR, "dsp-faves")], timeout=30)


def act_sub_level(db):
    return nexia.set_level(db, time.time())


def act_sub_mute(on):
    return nexia.set_mute(on, time.time())


# ---- HTTP ----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HiFiDash/1.0"

    def log_message(self, *a):
        pass  # quiet (flip to a sys.stderr.write if you ever need to debug requests)

    def _authed(self, q):
        if not TOKEN:
            return True
        return q.get("t", [""])[0] == TOKEN or self.headers.get("X-Token") == TOKEN

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = PAGE.replace("__APP_VERSION__", APP_VERSION).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _png(self, name):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"ok": False, "error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._html()
        if u.path in ("/icon-180.png", "/icon-512.png"):
            return self._png(u.path.lstrip("/"))
        if not self._authed(q):
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        if u.path == "/api/status":
            return self._json(full_status())
        if u.path == "/api/lists":
            return self._json({
                "playlists": helper_lines("dsp-playlists"),
                "genres": helper_lines("dsp-genres"),
                "artists": helper_lines("dsp-artists"),
            })
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._json({"ok": False, "error": "unauthorized"}, 401)

        def arg(name, default=None):
            return q.get(name, [default])[0]

        rc, out, err = 0, "", ""
        p = u.path
        if p == "/api/preset":
            rc, out, err = act_preset(int(arg("n", "0")))
        elif p == "/api/mute":
            rc, out, err = act_mute(arg("on", "1") == "1")
        elif p == "/api/volume":
            rc, out, err = act_volume(int(arg("v", "60")))
        elif p == "/api/transport":
            rc, out, err = act_transport(arg("cmd", "playpause"))
        elif p == "/api/seek":
            rc, out, err = act_seek(arg("to", "0"))
        elif p == "/api/start":
            rc, out, err = act_start()
        elif p == "/api/stop":
            rc, out, err = act_stop()
        elif p == "/api/faves":
            rc, out, err = act_faves()
        elif p == "/api/sub-level":
            rc, out, err = act_sub_level(arg("db", "0"))
        elif p == "/api/sub-mute":
            rc, out, err = act_sub_mute(arg("on", "1") == "1")
        elif p == "/api/play":
            rc, out, err = act_play(arg("name", ""))
        elif p == "/api/play-genre":
            rc, out, err = act_play_genre(arg("name", ""))
        elif p == "/api/play-artist":
            rc, out, err = act_play_artist(arg("name", ""))
        else:
            return self._json({"ok": False, "error": "not found"}, 404)

        ok = (rc == 0)
        return self._json({"ok": ok, "rc": rc, "out": out.strip(), "err": err.strip(),
                           "status": full_status()})


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MB Hi-Fi">
<meta name="theme-color" content="#0b0d12">
<link rel="apple-touch-icon" sizes="180x180" href="/icon-180.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="icon" type="image/png" href="/icon-512.png">
<title>MB Hi-Fi</title>
<style>
:root{
  --bg:#0b0d12; --card:rgba(255,255,255,.045); --line:rgba(255,255,255,.09);
  --txt:#eef1f7; --dim:#8b93a7; --accent:#7c5cff; --accent2:#22d3ee;
  --ok:#34d399; --warn:#fbbf24; --bad:#fb7185;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--txt);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif}
body{background:
  radial-gradient(1200px 600px at 80% -10%,rgba(124,92,255,.18),transparent 60%),
  radial-gradient(900px 500px at -10% 110%,rgba(34,211,238,.13),transparent 55%),
  var(--bg);
  min-height:100vh;padding:max(16px,env(safe-area-inset-top)) 16px 40px;}
.wrap{max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
header{display:flex;align-items:center;justify-content:space-between;padding:4px 2px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:.3px;font-size:20px}
.logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  box-shadow:0 6px 20px rgba(124,92,255,.45)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad);box-shadow:0 0 10px var(--bad);transition:.3s}
.dot.live{background:var(--ok);box-shadow:0 0 10px var(--ok)}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px;
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  box-shadow:0 10px 40px rgba(0,0,0,.35)}
.np-state{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--accent2);font-weight:700}
.np-title{font-size:23px;font-weight:750;margin:6px 0 2px;line-height:1.15}
.np-artist{color:var(--dim);font-size:15px}
.seek{margin-top:16px}
.scrub{appearance:none;-webkit-appearance:none;width:100%;height:6px;border-radius:6px;outline:none;cursor:pointer;
  background:linear-gradient(90deg,var(--accent),var(--accent2)) no-repeat;background-size:0% 100%;
  background-color:rgba(255,255,255,.12)}
.scrub::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;
  box-shadow:0 2px 8px rgba(0,0,0,.55);cursor:pointer}
.scrub:active::-webkit-slider-thumb{transform:scale(1.25)}
.times{margin-top:7px}
.times span{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums;font-weight:650;letter-spacing:.02em}
.transport{display:flex;gap:10px;justify-content:center;margin-top:14px}
.tbtn{width:50px;height:50px;border-radius:50%;border:1px solid var(--line);background:rgba(255,255,255,.05);
  color:var(--txt);font-size:18px;display:grid;place-items:center;cursor:pointer;transition:.15s}
.tbtn:active{transform:scale(.9)}
.tbtn.main{width:60px;height:60px;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;
  box-shadow:0 8px 26px rgba(124,92,255,.5);font-size:23px}
.tbtn.seekbtn{font-size:13px;font-weight:750;font-variant-numeric:tabular-nums}
.row{display:flex;align-items:center;gap:12px}
.between{justify-content:space-between}
.label{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);font-weight:700;margin-bottom:12px}
.vol{appearance:none;-webkit-appearance:none;width:100%;height:8px;border-radius:6px;outline:none;
  background:linear-gradient(90deg,var(--accent),var(--accent2)) no-repeat;background-size:60% 100%;
  background-color:rgba(255,255,255,.1)}
.vol::-webkit-slider-thumb{-webkit-appearance:none;width:26px;height:26px;border-radius:50%;background:#fff;
  box-shadow:0 3px 12px rgba(0,0,0,.5);cursor:pointer}
.volval{font-variant-numeric:tabular-nums;font-weight:750;font-size:18px;min-width:46px;text-align:right}
.chips{display:flex;gap:8px;margin-top:14px}
.chip{flex:1;padding:9px;border-radius:12px;border:1px solid var(--line);background:rgba(255,255,255,.04);
  color:var(--dim);font-weight:650;font-size:13px;text-align:center;cursor:pointer;transition:.15s}
.chip:active{transform:scale(.96)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid4{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.pbtn{padding:16px 10px;border-radius:14px;border:1px solid var(--line);background:rgba(255,255,255,.04);
  color:var(--txt);font-weight:700;font-size:15px;cursor:pointer;transition:.15s;text-align:center}
.pbtn:active{transform:scale(.97)}
.pbtn.on{background:linear-gradient(135deg,rgba(124,92,255,.9),rgba(34,211,238,.85));border:none;
  box-shadow:0 8px 24px rgba(124,92,255,.45)}
.pbtn small{display:block;font-weight:500;font-size:11px;color:var(--dim);margin-top:3px}
.pbtn.on small{color:rgba(255,255,255,.85)}
.act{padding:15px;border-radius:14px;border:none;font-weight:750;font-size:15px;cursor:pointer;transition:.15s;color:#fff}
.act:active{transform:scale(.97)}
.start{background:linear-gradient(135deg,#10b981,#22d3ee)}
.stop{background:linear-gradient(135deg,#f43f5e,#fb7185)}
.mute{flex:1;padding:13px;border-radius:14px;border:1px solid var(--line);background:rgba(255,255,255,.04);
  color:var(--txt);font-weight:700;cursor:pointer;transition:.15s}
.mute.on{background:linear-gradient(135deg,#f59e0b,#fbbf24);border:none;color:#1a1205}
.meters{display:flex;flex-direction:column;gap:9px}
.meter{display:flex;align-items:center;gap:10px}
.meter .mlab{width:34px;font-size:11px;color:var(--dim);font-weight:700}
.bar{flex:1;height:9px;border-radius:6px;background:rgba(255,255,255,.07);overflow:hidden}
.fill{height:100%;width:0;border-radius:6px;
  background:linear-gradient(90deg,#22d3ee,#34d399 55%,#fbbf24 80%,#fb7185);transition:width .12s linear}
select{width:100%;padding:13px;border-radius:13px;background:rgba(255,255,255,.05);color:var(--txt);
  border:1px solid var(--line);font-size:15px;appearance:none;font-weight:600}
.muted{color:var(--dim);font-size:12px;margin-top:8px;text-align:center}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(120%);
  background:rgba(20,24,33,.95);border:1px solid var(--line);padding:11px 18px;border-radius:14px;
  font-size:14px;font-weight:600;transition:.3s;backdrop-filter:blur(12px);max-width:90%;z-index:9}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.err{border-color:var(--bad);color:var(--bad)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><div class="logo"></div>MB Hi-Fi <span style="font-size:11px;color:var(--dim);font-weight:400;align-self:flex-end;padding-bottom:2px">__APP_VERSION__</span></div>
    <div class="row"><span id="srcLbl" class="muted" style="margin:0"></span><span id="dot" class="dot"></span></div>
  </header>

  <div class="card">
    <div id="npState" class="np-state">—</div>
    <div id="npTitle" class="np-title">Nothing playing</div>
    <div id="npArtist" class="np-artist"></div>
    <div class="seek">
      <input class="scrub" id="scrub" type="range" min="0" max="1000" value="0"
        oninput="scrubLive()" onchange="scrubSet()">
      <div class="row between times">
        <span id="curTime">0:00</span>
        <span id="durTime">—</span>
      </div>
    </div>
    <div class="transport">
      <button class="tbtn" title="Previous track" onclick="post('/api/transport?cmd=prev')">⏮</button>
      <button class="tbtn seekbtn" title="Back 15s" onclick="skip(-15)">⏪</button>
      <button class="tbtn main" id="pp" onclick="post('/api/transport?cmd=playpause')">▶</button>
      <button class="tbtn seekbtn" title="Forward 15s" onclick="skip(15)">⏩</button>
      <button class="tbtn" title="Next track" onclick="post('/api/transport?cmd=next')">⏭</button>
    </div>
  </div>

  <div class="card">
    <div class="row between"><div class="label" style="margin:0">Volume</div><div class="volval" id="volVal">—</div></div>
    <input class="vol" id="vol" type="range" min="0" max="100" value="60" oninput="volLive(this.value)" onchange="volSet(this.value)">
    <div class="chips">
      <div class="chip" onclick="volSet(35)">Quiet</div>
      <div class="chip" onclick="volSet(60)">Medium</div>
      <div class="chip" onclick="volSet(85)">Loud</div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="mute" id="muteBtn" onclick="toggleMute()">Mute</button>
    </div>
  </div>

  <div class="card">
    <div class="row between"><div class="label" style="margin:0">Subs</div><div class="volval" id="subVal">—</div></div>
    <input class="vol" id="sub" type="range" min="-24" max="0" step="0.5" value="0" oninput="subLive(this.value)" onchange="subSet(this.value)">
    <div class="row" style="margin-top:14px">
      <button class="mute" id="subMuteBtn" onclick="toggleSubMute()">Mute Subs</button>
    </div>
  </div>

  <div class="card">
    <div class="label">Presets</div>
    <div class="grid4" id="presets"></div>
  </div>

  <div class="card">
    <div class="label">Levels</div>
    <div class="meters" id="meters"></div>
  </div>

  <div class="card">
    <div class="label">Music</div>
    <div class="grid2" style="margin-bottom:10px">
      <button class="act start" onclick="post('/api/start')">Start</button>
      <button class="act stop" onclick="post('/api/stop')">Stop</button>
    </div>
    <button class="pbtn" style="width:100%;margin-bottom:10px" onclick="post('/api/faves')">★ Favourites — shuffle</button>
    <select id="plSel" onchange="if(this.value)post('/api/play?name='+encodeURIComponent(this.value))">
      <option value="">Playlists…</option></select>
    <div style="height:10px"></div>
    <select id="arSel" onchange="if(this.value)post('/api/play-artist?name='+encodeURIComponent(this.value))">
      <option value="">Artists…</option></select>
    <div style="height:10px"></div>
    <select id="gnSel" onchange="if(this.value)post('/api/play-genre?name='+encodeURIComponent(this.value))">
      <option value="">Genres…</option></select>
    <div class="muted">Power-off (Meross plug) stays a phone Shortcut.</div>
  </div>
</div>
<div id="toast" class="toast"></div>

<script>
const PRESETS = ["Flat","EDM","Movies","Late Night"]; // config 0..3
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const hdrs = () => TOKEN ? {'X-Token': TOKEN} : {};
const APP_VERSION = "__APP_VERSION__";
let dragging = false, lastPreset = -1;

function $(id){return document.getElementById(id)}
function toast(msg,err){const t=$('toast');t.textContent=msg;t.className='toast show'+(err?' err':'');
  clearTimeout(t._t);t._t=setTimeout(()=>t.className='toast',1900)}

function buildPresets(){
  const g=$('presets');g.innerHTML='';
  PRESETS.forEach((nm,i)=>{
    const b=document.createElement('button');b.className='pbtn';b.dataset.i=i;
    b.innerHTML=nm+'<small>config '+i+'</small>';
    b.onclick=()=>post('/api/preset?n='+i);
    g.appendChild(b);
  });
}
function buildMeters(s){
  const m=$('meters');
  const rows=[['IN L',s.input_pct&&s.input_pct[0]],['IN R',s.input_pct&&s.input_pct[1]],
    ['1',s.output_pct&&s.output_pct[0]],['2',s.output_pct&&s.output_pct[1]],
    ['3',s.output_pct&&s.output_pct[2]],['4',s.output_pct&&s.output_pct[3]]];
  if(!m.dataset.built){
    m.innerHTML=rows.map(r=>`<div class="meter"><div class="mlab">${r[0]}</div><div class="bar"><div class="fill"></div></div></div>`).join('');
    m.dataset.built='1';
  }
  const fills=m.querySelectorAll('.fill');
  rows.forEach((r,i)=>{fills[i].style.width=(r[1]||0)+'%'});
}

function render(s){
  if(!s){$('dot').classList.remove('live');return}
  $('dot').classList.add('live');
  const n=s.now||{};
  const st=(n.state||'').toLowerCase();
  $('npState').textContent = st==='playing'?'Now Playing':(st==='paused'?'Paused':(st==='notrunning'?'Music closed':(st||'—')));
  $('npTitle').textContent = n.title||'Nothing playing';
  $('npArtist').textContent = [n.artist,n.album].filter(Boolean).join(' · ');
  $('pp').textContent = st==='playing'?'⏸':'▶';
  // seek/timeline: re-anchor to the server's truth each poll; the local ticker
  // advances the bar smoothly in between (poll is only ~1.4s).
  if(!scrubbing){
    npDur = n.duration||0; npPos = n.position||0; posAt = Date.now();
    npPlaying = (st==='playing');
    paintSeek();
  }
  if(s.volume!=null && !dragging){$('vol').value=s.volume; volLive(s.volume,true)}
  $('srcLbl').textContent = s.source? (s.source+' · '+(s.master_gain!=null?s.master_gain.toFixed(1)+'dB':'')) : '';
  // mute
  const mb=$('muteBtn');mb.classList.toggle('on',!!s.mute);mb.textContent=s.mute?'Muted':'Mute';
  // subs (Nexia)
  const sub=s.sub||{};
  const smb=$('subMuteBtn');
  if(sub.ok){
    // optimistic: hold at the user's just-set value until the device confirms it (slow ~1.2s write) or 6s timeout
    if(subPending!==null && (Math.abs((sub.level||0)-subPending)<0.01 || Date.now()-subPendingAt>6000)) subPending=null;
    if(subMutePending!==null && (!!sub.muted===subMutePending || Date.now()-subPendingAt>6000)) subMutePending=null;
    if(!subDragging){const lv=subPending!==null?subPending:sub.level;$('sub').value=lv;subLive(lv,true);}
    const mu=subMutePending!==null?subMutePending:!!sub.muted;
    smb.classList.toggle('on',mu);smb.textContent=mu?'Subs Muted':'Mute Subs';
    smb.disabled=false;
  } else {
    $('subVal').textContent='offline';smb.disabled=true;
  }
  // preset highlight
  if(s.preset!=null && s.preset!==lastPreset){
    document.querySelectorAll('.pbtn').forEach(b=>b.classList.toggle('on',+b.dataset.i===s.preset));
    lastPreset=s.preset;
  }
  buildMeters(s);
}

function volLive(v,silent){$('vol').style.backgroundSize=v+'% 100%';$('volVal').textContent=v;}
let volTimer=null;
function volSet(v){v=Math.round(v);$('vol').value=v;volLive(v);
  clearTimeout(volTimer);volTimer=setTimeout(()=>post('/api/volume?v='+v,true),120)}
$('vol').addEventListener('touchstart',()=>dragging=true);
$('vol').addEventListener('touchend',()=>{dragging=false});
$('vol').addEventListener('mousedown',()=>dragging=true);
$('vol').addEventListener('mouseup',()=>{dragging=false});

// ---- subs (Nexia, dB not %) ----
let subDragging=false, subTimer=null, subPending=null, subMutePending=null, subPendingAt=0;
function subPct(v){return Math.max(0,Math.min(100,(v+24)/24*100));} // -24..0 -> 0..100 (device rejects boost)
function subLive(v,silent){v=+v;$('sub').style.backgroundSize=subPct(v)+'% 100%';
  $('subVal').textContent=(v>0?'+':'')+v.toFixed(1)+' dB';}
function subSet(v){v=Math.round(v*2)/2;$('sub').value=v;subLive(v);subPending=v;subPendingAt=Date.now();
  clearTimeout(subTimer);subTimer=setTimeout(async()=>{ // not quiet: surface write failures
    if(await post('/api/sub-level?db='+v)===false){subPending=null;} // failed -> snap to truth now
  },150)}
function toggleSubMute(){const on=!$('subMuteBtn').classList.contains('on');const smb=$('subMuteBtn');
  smb.classList.toggle('on',on);smb.textContent=on?'Subs Muted':'Mute Subs';subMutePending=on;subPendingAt=Date.now();
  post('/api/sub-mute?on='+(on?1:0),true)}
$('sub').addEventListener('touchstart',()=>subDragging=true);
$('sub').addEventListener('touchend',()=>{subDragging=false});
$('sub').addEventListener('mousedown',()=>subDragging=true);
$('sub').addEventListener('mouseup',()=>{subDragging=false});

// ---- seek / timeline ----
let scrubbing=false, npPos=0, npDur=0, npPlaying=false, posAt=0;
function fmt(s){s=Math.max(0,Math.floor(s));const m=Math.floor(s/60),x=s%60;return m+':'+(x<10?'0':'')+x;}
function curPos(){
  if(npPlaying && npDur>0 && !scrubbing) return Math.min(npPos+(Date.now()-posAt)/1000, npDur);
  return Math.min(npPos, npDur||npPos);
}
function paintSeek(p){
  if(p==null) p=curPos();
  const frac = npDur>0 ? Math.max(0,Math.min(1,p/npDur)) : 0;
  const sc=$('scrub');
  if(!scrubbing) sc.value=Math.round(frac*1000);
  sc.style.backgroundSize=(frac*100)+'% 100%';
  $('curTime').textContent=fmt(p);
  $('durTime').textContent=npDur>0?fmt(npDur):'—';
}
function scrubLive(){ // dragging: show the would-be position live, don't seek yet
  scrubbing=true;
  const frac=$('scrub').value/1000, p=frac*(npDur||0);
  $('scrub').style.backgroundSize=(frac*100)+'% 100%';
  $('curTime').textContent=fmt(p);
}
function scrubSet(){ // released: commit the seek
  const p=Math.round(($('scrub').value/1000)*(npDur||0));
  npPos=p; posAt=Date.now(); scrubbing=false;
  paintSeek(p);
  post('/api/seek?to='+p,true);
}
function skip(d){
  if(npDur<=0) return;
  const p=Math.max(0,Math.min(npDur, Math.round(curPos()+d)));
  npPos=p; posAt=Date.now(); paintSeek(p);
  post('/api/seek?to='+p,true);
}
$('scrub').addEventListener('touchstart',()=>scrubbing=true);
$('scrub').addEventListener('mousedown',()=>scrubbing=true);
setInterval(()=>{ if(!scrubbing && npPlaying && npDur>0) paintSeek(); }, 500);

function toggleMute(){const on=!$('muteBtn').classList.contains('on');post('/api/mute?on='+(on?1:0),true)}

function locked(){$('dot').classList.remove('live');$('npState').textContent='Locked';
  $('npTitle').textContent='Add ?t=token to the URL';$('npArtist').textContent='';}
async function post(path,quiet){
  try{
    const r=await fetch(path,{method:'POST',headers:hdrs()});
    if(r.status===401){toast('locked — add ?t=token',true);return}
    const j=await r.json();
    if(j.status)render(j.status);
    if(!j.ok && !quiet)toast((j.err||j.out||'failed').slice(0,80),true);
    return !!j.ok;
  }catch(e){toast('network error',true);return false}
}
async function poll(){
  try{
    const r=await fetch('/api/status',{headers:hdrs()});
    if(r.status===401){locked();return}
    const s=await r.json();
    if(s && s.version && s.version!==APP_VERSION && !sessionStorage.getItem('vreload')){
      sessionStorage.setItem('vreload', s.version); location.reload(true); return;
    }
    render(s);
  }catch(e){$('dot').classList.remove('live')}
}
async function loadLists(){
  try{
    const j=await (await fetch('/api/lists',{headers:hdrs()})).json();
    fill('plSel',j.playlists,'Playlists');fill('arSel',j.artists,'Artists');fill('gnSel',j.genres,'Genres');
  }catch(e){}
}
function fill(id,items,label){
  const s=$(id);s.innerHTML='<option value="">'+label+'…</option>'+
    (items||[]).map(x=>`<option value="${x.replace(/"/g,'&quot;')}">${x}</option>`).join('');
}

buildPresets();poll();loadLists();
setInterval(poll,1400);
</script>
</body>
</html>
"""


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("HiFi Dashboard on http://0.0.0.0:%d  (open http://27-iMac.local:%d from the phone)" % (PORT, PORT))
    if TOKEN:
        print("Token auth ENABLED — append ?t=%s" % TOKEN)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
