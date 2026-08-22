#!/usr/bin/env python3
"""
HiFi Dashboard — a local web control panel for the iMac PA system.

Talks to the minidsp daemon (via the `minidsp` CLI) and Apple Music (via osascript).
No third-party dependencies; uses the Python stdlib only.

  Run:   python3 server.py            # binds 0.0.0.0:8765 (reachable from the phone on LAN)
  Open:  http://<this-machine>.local:8765   # from the iPhone / any LAN device

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
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import nexia  # Nexia PM sub-bus control (telnet, stdlib) — see nexia.py

PORT = int(os.environ.get("DSP_WEB_PORT", "8765"))
# The SPL meter needs the phone mic, and getUserMedia demands a secure
# context — iOS Safari blocks it over plain http. So we ALSO listen on TLS.
# http stays up unchanged so existing bookmarks and the QR codes keep working.
TLS_PORT = int(os.environ.get("DSP_WEB_TLS_PORT", "8766"))
TOKEN = os.environ.get("DSP_WEB_TOKEN", "")
APP_VERSION = "v18"  # bump on any served-page change; stale clients auto-reload on mismatch (see poll())
MINIDSP = "/usr/local/bin/minidsp"
BINDIR = "/usr/local/bin"
HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(HERE, "certs")
CERTFILE = os.path.join(CERT_DIR, "server.crt")
KEYFILE = os.path.join(CERT_DIR, "server.key")
CAFILE = os.path.join(CERT_DIR, "ca.crt")

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
# The Nexia output block rejects boost (>0 dB). To give the slider headroom in
# both directions, the input block carries +6 dB static makeup gain (INPLVLPML
# inst 7, both ch — set 2026-07-06) and the app translates: UI/trims speak the
# original scale, the device output runs SUB_MAKEUP lower. UI +6 = device 0.
SUB_MAKEUP = 6.0
PRESET_SUB_TRIM = {0: 0.0, 1: 3.0, 2: 2.0, 3: -8.0}  # Flat / EDM / Movies / Late Night, dB (UI scale)

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


# minidspd (already a LaunchAgent, com.minidsp.minidspd) serves levels over
# HTTP in ~7ms — the fast path for meters. The CLI stays for writes/status;
# it talks through the same daemon, so the two coexist.
DSPD_URL = os.environ.get("DSPD_URL", "http://127.0.0.1:5380/devices/0")


def dspd_levels():
    """Input/output meters from minidspd's HTTP API (fast path, ~7ms)."""
    try:
        import urllib.request
        with urllib.request.urlopen(DSPD_URL, timeout=0.8) as r:
            d = json.load(r)
        return {"ok": True,
                "inputs": d.get("input_levels") or [],
                "outputs": d.get("output_levels") or []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


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
    set fav to false
    try
        set t to name of current track
        set a to artist of current track
        set al to album of current track
        set dur to duration of current track
        set fav to favorited of current track
    end try
    try
        set pp to player position
        if pp is not missing value then set pos to pp
    end try
    return ps & "|" & t & "|" & a & "|" & al & "|" & pos & "|" & dur & "|" & fav
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
             "position": 0.0, "duration": 0.0, "faved": False}
    if not music_running():
        return empty
    rc, out, err = osa(NP_SCRIPT, timeout=10)
    if rc != 0:
        return dict(empty, state="unknown")
    parts = (out.strip().split("|") + [""] * 7)[:7]
    return {"state": parts[0], "title": parts[1], "artist": parts[2], "album": parts[3],
            "position": _num(parts[4]), "duration": _num(parts[5]),
            "faved": parts[6].strip() == "true"}


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
        nexia.set_level(PRESET_SUB_TRIM[preset] - SUB_MAKEUP, now,
                        src="couple preset %s->%s" % (old_preset, preset))
    if mute is not None and mute != old_mute:
        nexia.set_mute(mute, now,
                       src="couple mute %s->%s" % (old_mute, mute))


def full_status():
    d = dsp_status()
    d["now"] = now_playing()
    d["volume"] = music_volume()
    couple_to_dsp(d)
    sub = nexia.status(time.time())
    sub["level"] = round(sub["level"] + SUB_MAKEUP, 1)  # device dB -> UI scale
    d["sub"] = sub
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


PREV_SCRIPT = '''tell application "Music"
    set player position to 0
    back track
    delay 0.3
    if player state is not playing then play
end tell'''


def act_transport(cmd):
    # "prev" always jumps BETWEEN tracks: rewind to 0 first so `back track`
    # can't interpret the press as "restart current". Needs a real current
    # playlist to land on — the dsp-play* helpers provide one ("HiFi Queue");
    # an anonymous track-list queue has no previous to go to at all.
    if cmd == "prev":
        return osa(PREV_SCRIPT)
    mp = {
        "play": "play", "pause": "pause", "playpause": "playpause",
        "next": "next track",
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
    # -s = sequential: the playlist plays in its stored order. dsp-play
    # defaults to shuffle, so every playlist launched from here arrived
    # scrambled; shuffle stays explicit (Favourites button, artist/genre).
    return run([os.path.join(BINDIR, "dsp-play"), "-s", name], timeout=30)


def act_play_genre(name):
    return run([os.path.join(BINDIR, "dsp-play-genre"), name], timeout=30)


# ---- library search --------------------------------------------------------
SEARCH_SCRIPT = '''on run argv
  set q to item 1 of argv
  tell application "Music"
    set res to (search library playlist 1 for q only songs)
    set outLines to {}
    set n to 0
    repeat with t in res
      set n to n + 1
      if n > 25 then exit repeat
      set end of outLines to (persistent ID of t) & "|" & (name of t) & "|" & (artist of t) & "|" & (album of t)
    end repeat
    set AppleScript's text item delimiters to linefeed
    return outLines as text
  end tell
end run'''

# Plays the picked track IN ITS ALBUM CONTEXT via the HiFi Queue (shuffle off,
# album order continues after it) — a 1-track queue would hand playback to
# Autoplay's random picks, the exact failure the artist fix killed.
PLAY_TRACK_SCRIPT = '''on run argv
  set pid to item 1 of argv
  tell application "Music"
    set t to (first track of library playlist 1 whose persistent ID is pid)
    set alb to album of t
    if not (exists user playlist "HiFi Queue") then
      make new user playlist with properties {name:"HiFi Queue"}
    end if
    delete every track of user playlist "HiFi Queue"
    if alb is not "" then
      try
        duplicate (every track of library playlist 1 whose album is alb) to user playlist "HiFi Queue"
      end try
    end if
    if (count tracks of user playlist "HiFi Queue") is 0 then
      duplicate t to user playlist "HiFi Queue"
    end if
    set shuffle enabled to false
    set nm to name of t
    -- Same two traps as dsp-play-artist: playing a playlist the instant it is
    -- built opens on track 1 despite a good track reference, and `whose name is
    -- (name of t)` errors -1728 unevaluated. Settle, then confirm the landing.
    delay 0.5
    set qt to missing value
    try
      set qt to (first track of user playlist "HiFi Queue" whose persistent ID is pid)
    end try
    if qt is missing value then
      try
        set qt to (first track of user playlist "HiFi Queue" whose name is nm)
      end try
    end if
    if qt is missing value then
      play user playlist "HiFi Queue"
    else
      play qt
      delay 0.4
      try
        if (persistent ID of current track) is not pid then play qt
      end try
    end if
    return (name of t) & " / " & (artist of t)
  end tell
end run'''


def act_search(q):
    q = (q or "").strip()
    if len(q) < 2:
        return {"ok": True, "results": []}
    rc, out, err = run(["osascript", "-e", SEARCH_SCRIPT, q], timeout=20)
    if rc != 0:
        return {"ok": False, "error": (err or "search failed").strip()[:120]}
    results = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4 and parts[0].strip():
            results.append({"pid": parts[0].strip(), "title": parts[1],
                            "artist": parts[2], "album": parts[3]})
    return {"ok": True, "results": results}


def act_play_track(pid):
    if not re.fullmatch(r"[0-9A-F]{16}", (pid or "").strip()):
        return (2, "", "bad track id")
    rc, out, err = run(["osascript", "-e", PLAY_TRACK_SCRIPT, pid.strip()], timeout=30)
    if rc != 0:
        return (rc, "", (err or "track not found").strip()[:120])
    return (0, "playing %s (album follows)" % out.strip(), "")


def act_play_artist(name):
    # -a = the artist's whole library catalogue (shuffled), not just favourites.
    # Favourites-only queues could be a single track, after which Music's
    # Autoplay wandered off to random picks — the "one song then chaos" bug.
    return run([os.path.join(BINDIR, "dsp-play-artist"), "-a", name], timeout=60)


def act_faves():
    return run([os.path.join(BINDIR, "dsp-faves")], timeout=30)


# ---- album artwork --------------------------------------------------------
# Exported on demand (once per track — the page only refetches when the track
# text changes), via a temp file since osascript can't return binary cleanly.
ART_TMP = "/tmp/hifi-art.bin"
ART_SCRIPT = '''tell application "Music"
    set d to raw data of artwork 1 of current track
end tell
set f to open for access POSIX file "%s" with write permission
set eof f to 0
write d to f
close access f
return "ok"''' % ART_TMP

_art_lock = threading.Lock()
_art_cache = {"ts": 0.0, "data": b"", "mime": ""}


def current_artwork():
    """Return (data, mime) for the current track's artwork, or (b"", "")."""
    with _art_lock:
        if _art_cache["data"] and time.time() - _art_cache["ts"] < 2.0:
            return _art_cache["data"], _art_cache["mime"]   # double-request guard
        rc, out, err = osa(ART_SCRIPT, timeout=15)
        if rc != 0:
            return b"", ""
        try:
            with open(ART_TMP, "rb") as f:
                data = f.read()
        except OSError:
            return b"", ""
        if data.startswith(b"\xff\xd8"):
            mime = "image/jpeg"
        elif data.startswith(b"\x89PNG"):
            mime = "image/png"
        else:
            return b"", ""
        _art_cache.update(ts=time.time(), data=data, mime=mime)
        return data, mime


# ---- audio-path recovery ---------------------------------------------------
# 2026-07-08 incident: the E70's USB audio path wedged (silence at the miniDSP
# analog inputs while macOS streamed happily into the DAC; E70 panel normal).
# Recovery that worked: toggle the miniDSP source away/back + bounce the
# system default output device to force CoreAudio to tear down and rebuild
# the USB stream. This runs both, then reports the input meters.

def _bounce_output_device():
    """Switch the default output to another device and back (CoreAudio kick).

    Generic: remembers the current default, hops to any other output device
    (preferring Built-in), and restores. Returns (ok, message).
    """
    import ctypes.util
    import struct
    try:
        ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]

        def fourcc(s):
            return struct.unpack(">I", s)[0]

        def addr(sel):
            return struct.pack("III", fourcc(sel), fourcc(b"glob"), 0)

        def get_prop(obj, a, size):
            buf = ctypes.create_string_buffer(size)
            sz = ctypes.c_uint32(size)
            rc = ca.AudioObjectGetPropertyData(obj, a, 0, None, ctypes.byref(sz), buf)
            return rc, buf.raw[:sz.value]

        def name_of(dev):
            rc, raw = get_prop(dev, addr(b"lnam"), 8)
            if rc != 0 or len(raw) < 8:
                return "?"
            ref = struct.unpack("Q", raw[:8])[0]
            out = ctypes.create_string_buffer(256)
            cf.CFStringGetCString(ctypes.c_void_p(ref), out, 256, 0x08000100)
            return out.value.decode("utf-8", "replace")

        def has_output(dev):
            # stream configuration on the output scope: any streams -> output-capable
            a = struct.pack("III", fourcc(b"slay"), fourcc(b"outp"), 0)
            sz = ctypes.c_uint32(0)
            rc = ca.AudioObjectGetPropertyDataSize(dev, a, 0, None, ctypes.byref(sz))
            return rc == 0 and sz.value > 8

        def set_default(dev):
            return ca.AudioObjectSetPropertyData(
                1, addr(b"dOut"), 0, None, 4, struct.pack("I", dev))

        rc, raw = get_prop(1, addr(b"dev#"), 4 * 64)
        devs = [d for d in struct.unpack("%dI" % (len(raw) // 4), raw) if d]
        rc, raw = get_prop(1, addr(b"dOut"), 4)
        cur = struct.unpack("I", raw[:4])[0]
        outs = [d for d in devs if d != cur and has_output(d)]
        if not outs:
            return False, "no alternate output device to bounce through"
        alt = next((d for d in outs if "Built-in" in name_of(d)), outs[0])
        if set_default(alt) != 0:
            return False, "could not switch output device"
        time.sleep(1.2)
        if set_default(cur) != 0:
            return False, "SWITCHED BUT COULD NOT RESTORE %s — set output manually!" % name_of(cur)
        return True, "bounced %s -> %s -> back" % (name_of(cur), name_of(alt))
    except Exception as e:  # ctypes failures shouldn't kill the request
        return False, "bounce failed: %s" % e


NOISE_FLOOR_DB = -70.0   # meters above this = real signal (floor sits ~-86)


def music_player_state():
    """Apple Music player state: playing/paused/stopped, or 'unknown'."""
    if not music_running():
        return "notrunning"
    rc, out, _ = osa('tell application "Music" to (get player state) as string', timeout=8)
    return out.strip() if rc == 0 and out.strip() else "unknown"


def act_fix_audio():
    """Run the audio-path recovery sequence; verify only against a live source.

    Meters at noise floor mean nothing when nothing is playing, so the verdict
    is three-state: VERIFIED_OK (playing + signal), VERIFIED_FAIL (playing but
    still at noise floor), UNVERIFIED (no playback — reset ran, can't judge).
    Never auto-starts playback: a repair button must not surprise-blast the PA.
    """
    state0 = music_player_state()
    st = dsp_status()
    before = st.get("inputs") or []

    # reset sequence
    run([MINIDSP, "source", "usb"], timeout=10)
    time.sleep(1.0)
    run([MINIDSP, "source", "analog"], timeout=10)
    ok, msg = _bounce_output_device()

    # invariant: a reset can never strand the source selection. Should already
    # be analog; note it in the report if this actually corrected anything.
    src_check = dsp_status().get("source", "?")
    run([MINIDSP, "source", "analog"], timeout=10)
    if str(src_check).lower() != "analog":
        msg += "; source was %s — corrected to analog" % src_check

    if state0 == "playing":
        # resume in case the device bounce stalled the stream, let the
        # pipeline refill, then judge the meters against a real signal.
        osa('tell application "Music" to play', timeout=8)
        time.sleep(2.5)
        st2 = dsp_status()
        after = st2.get("inputs") or []
        diag = "in %s -> %s dB; src %s; Music %s; %s" % (
            [round(x, 1) for x in before], [round(x, 1) for x in after],
            st2.get("source", "?"), music_player_state(), msg)
        if any(x > NOISE_FLOOR_DB for x in after):
            return (0, "VERIFIED_OK — audio path reset, signal present (%s)" % diag, "")
        return (1, "", "VERIFIED_FAIL — playback running but inputs still at noise floor (%s) — check the E70 panel/cables" % diag)

    # nothing playing: meters would read noise floor either way — not an error
    diag = "src %s; Music %s; %s" % (dsp_status().get("source", "?"), state0, msg)
    return (0, "UNVERIFIED — reset done, nothing playing so meters can't confirm. Press play to check. (%s)" % diag, "")


def act_love(on):
    """Mark/unmark the current track as a Favorite in Apple Music."""
    val = "true" if on else "false"
    rc, out, err = osa('tell application "Music" to set favorited of current track to %s' % val,
                       timeout=10)
    if rc != 0:
        return (rc, "", (err or "no current track").strip())
    return (0, "track %s" % ("favorited" if on else "unfavorited"), "")


def act_sub_level(db):
    # UI speaks the original trim scale; the device runs SUB_MAKEUP lower.
    try:
        dev = float(db) - SUB_MAKEUP
    except (TypeError, ValueError):
        return (2, "", "bad sub level")
    return nexia.set_level(dev, time.time())


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
        body = (PAGE.replace("__APP_VERSION__", APP_VERSION)
                    .replace("__TLS_PORT__", str(TLS_PORT))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _ca_cert(self):
        # Serving the root CA so the phone can install it by visiting a URL —
        # far easier than AirDropping a file. Public material: the CA *key*
        # never leaves certs/ and is gitignored.
        try:
            with open(CAFILE, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"ok": False, "error": "no CA cert — run make-cert.sh"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Disposition", 'attachment; filename="mb-hifi-ca.crt"')
        self.send_header("Content-Length", str(len(body)))
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
        if u.path == "/ca.crt":
            return self._ca_cert()
        if not self._authed(q):
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        if u.path == "/api/status":
            return self._json(full_status())
        if u.path == "/api/meters":
            return self._json(dspd_levels())
        if u.path == "/api/search":
            return self._json(act_search(q.get("q", [""])[0]))
        if u.path == "/api/art":
            data, mime = current_artwork()
            if not data:
                return self._json({"ok": False, "error": "no artwork"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/lists":
            return self._json({
                "playlists": helper_lines("dsp-playlists"),
                "genres": helper_lines("dsp-genres"),
                "artists": helper_lines("dsp-artists"),
            })
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        # A handler crash must answer with JSON, not a dropped connection —
        # an empty reply reads as a generic "network error" on the client and
        # hides the real fault (learned from the act_love NameError, v11-v13).
        try:
            return self._do_post()
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"ok": False, "err": "server error: %s: %s"
                               % (type(e).__name__, e)}, 500)

    def _do_post(self):
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
        elif p == "/api/love":
            rc, out, err = act_love(arg("on", "1") == "1")
        elif p == "/api/fix-audio":
            rc, out, err = act_fix_audio()
        elif p == "/api/play":
            rc, out, err = act_play(arg("name", ""))
        elif p == "/api/play-genre":
            rc, out, err = act_play_genre(arg("name", ""))
        elif p == "/api/play-artist":
            rc, out, err = act_play_artist(arg("name", ""))
        elif p == "/api/play-track":
            rc, out, err = act_play_track(arg("pid", ""))
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
.fixbtn{font-size:11px;padding:5px 10px;border-radius:9px;border:1px solid #2a2f3d;background:#171b25;
  color:var(--dim);font-weight:600;cursor:pointer;-webkit-tap-highlight-color:transparent}
.srch{width:100%;font-size:16px;padding:12px 14px;border-radius:12px;border:1px solid #2a2f3d;
  background:#171b25;color:var(--ink);outline:none;-webkit-appearance:none}
.srch::placeholder{color:var(--dim)}
.srchres{max-height:280px;overflow-y:auto;border-radius:12px;margin-top:6px}
.srchrow{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.05);cursor:pointer;
  background:#141821;-webkit-tap-highlight-color:transparent}
.srchrow:active{background:#1d2330}
.srchrow .t{font-size:15px;font-weight:650}
.srchrow .a{font-size:12px;color:var(--dim);margin-top:1px}
.srchempty{padding:10px 12px;color:var(--dim);font-size:13px;background:#141821;border-radius:12px}
.fixbtn:disabled{opacity:.5}
.np-main{display:flex;align-items:center;gap:14px;margin-top:6px}
.np-meta{flex:1;min-width:0}
.np-art{width:86px;height:86px;border-radius:12px;object-fit:cover;background:#20242e;
  display:none;flex:none;box-shadow:0 4px 14px rgba(0,0,0,.35)}
.np-art.show{display:block}
.np-titlerow{display:flex;align-items:center;gap:10px}
.np-titlerow .np-title{flex:1;min-width:0}
.heart{background:none;border:none;font-size:26px;line-height:1;padding:6px 4px;cursor:pointer;
  color:#3a3f4d;transition:color .15s,transform .1s;-webkit-tap-highlight-color:transparent}
.heart.on{color:#ff4d6d}
.heart:active{transform:scale(1.25)}
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
.meter .mdb{width:50px;text-align:right;font-size:11px;color:var(--dim);font-weight:700;
  font-variant-numeric:tabular-nums;letter-spacing:-.2px}
.bar{flex:1;height:9px;border-radius:6px;background:rgba(255,255,255,.07);overflow:hidden;position:relative}
.bar .peak{position:absolute;top:0;bottom:0;width:2px;background:rgba(255,255,255,.85);left:0;opacity:0}
.bar .peak.on{opacity:.9}
.fill{height:100%;width:0;border-radius:6px;
  background:linear-gradient(90deg,#22d3ee,#34d399 55%,#fbbf24 80%,#fb7185);transition:width .12s linear}
.splmain{display:flex;align-items:flex-end;gap:14px;margin-top:4px}
.splnum{font-size:46px;font-weight:800;line-height:1;letter-spacing:-1.5px;font-variant-numeric:tabular-nums}
.splunit{font-size:12px;color:var(--dim);font-weight:700}
.splside{flex:1;display:flex;align-items:center;justify-content:flex-end;gap:8px}
.splmax{font-size:12px;color:var(--dim);font-weight:700;font-variant-numeric:tabular-nums}
.splwarn{font-size:12px;color:#fbbf24;margin-top:8px;line-height:1.35}
.splwarn a{color:#22d3ee}
.splopts{display:flex;gap:8px;margin-top:10px}
.seg{flex:1;display:flex;background:rgba(255,255,255,.05);border-radius:11px;padding:3px;gap:3px}
.seg button{flex:1;padding:8px 0;border:none;border-radius:9px;background:transparent;color:var(--dim);
  font-weight:700;font-size:12px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.seg button.on{background:rgba(255,255,255,.12);color:var(--txt)}
.splcal{display:flex;gap:8px;align-items:center;margin-top:10px}
.splcal input{flex:1;min-width:0;font-size:16px;padding:9px 11px;border-radius:10px;border:1px solid #2a2f3d;
  background:#171b25;color:var(--ink);outline:none;-webkit-appearance:none}
select{width:100%;padding:13px;border-radius:13px;background:rgba(255,255,255,.05);color:var(--txt);
  border:1px solid var(--line);font-size:15px;appearance:none;font-weight:600}
.muted{color:var(--dim);font-size:12px;margin-top:8px;text-align:center}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(120%);
  background:rgba(20,24,33,.95);border:1px solid var(--line);padding:11px 18px;border-radius:14px;
  font-size:14px;font-weight:600;transition:.3s;backdrop-filter:blur(12px);max-width:90%;z-index:9;
  overflow-wrap:break-word;text-align:center;line-height:1.35}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.err{border-color:var(--bad);color:var(--bad)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><div class="logo"></div>MB Hi-Fi <span style="font-size:11px;color:var(--dim);font-weight:400;align-self:flex-end;padding-bottom:2px">__APP_VERSION__</span></div>
    <div class="row"><button id="fixBtn" class="fixbtn" onclick="fixAudio()" title="Reset the USB/DAC audio path">Fix Audio</button><span id="srcLbl" class="muted" style="margin:0"></span><span id="dot" class="dot"></span></div>
  </header>

  <div class="card">
    <div id="npState" class="np-state">—</div>
    <div class="np-main">
      <img id="npArt" class="np-art" alt="" onload="this.classList.add('show')"
        onerror="this.classList.remove('show')">
      <div class="np-meta">
        <div class="np-titlerow">
          <div id="npTitle" class="np-title">Nothing playing</div>
          <button id="loveBtn" class="heart" title="Favorite this track" onclick="toggleLove()">♥</button>
        </div>
        <div id="npArtist" class="np-artist"></div>
      </div>
    </div>
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
    <input class="vol" id="sub" type="range" min="-18" max="6" step="0.5" value="0" oninput="subLive(this.value)" onchange="subSet(this.value)">
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
    <div class="row between"><div class="label" style="margin:0">Room level</div>
      <div class="splunit" id="splUnit">dB(A) fast</div></div>
    <div class="splmain">
      <div class="splnum" id="splNum">—</div>
      <div class="splside">
        <div class="splmax"><span id="splMax">—</span> max</div>
        <button class="fixbtn" onclick="splResetMax()">reset</button>
      </div>
    </div>
    <div class="splwarn" id="splWarn"></div>
    <button class="mute" id="splBtn" style="width:100%;margin-top:10px" onclick="splToggle()">Start mic</button>
    <div class="splopts">
      <div class="seg" id="splWtSeg">
        <button data-v="A" onclick="splSetWt('A')">A</button>
        <button data-v="C" onclick="splSetWt('C')">C</button>
        <button data-v="Z" onclick="splSetWt('Z')">Z</button>
      </div>
      <div class="seg" id="splRespSeg">
        <button data-v="F" onclick="splSetResp('F')">Fast</button>
        <button data-v="S" onclick="splSetResp('S')">Slow</button>
      </div>
    </div>
    <div class="splcal">
      <input id="splRef" type="number" inputmode="decimal" placeholder="reference dB">
      <button class="fixbtn" onclick="splCalibrate()">Calibrate</button>
    </div>
    <div class="muted" id="splOff">uncalibrated — relative only</div>
  </div>

  <div class="card">
    <div class="label">Music</div>
    <div class="grid2" style="margin-bottom:10px">
      <button class="act start" onclick="post('/api/start')">Start</button>
      <button class="act stop" onclick="post('/api/stop')">Stop</button>
    </div>
    <button class="pbtn" style="width:100%;margin-bottom:10px" onclick="post('/api/faves')">★ Favourites — shuffle</button>
    <input id="srch" class="srch" type="search" placeholder="Search library…" autocomplete="off"
      oninput="srchInput()" onfocus="srchInput()">
    <div id="srchRes" class="srchres"></div>
    <div style="height:10px"></div>
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
function toast(msg,err,ms){const t=$('toast');t.textContent=msg;t.className='toast show'+(err?' err':'');
  clearTimeout(t._t);t._t=setTimeout(()=>t.className='toast',ms||1900)}

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
    m.innerHTML=rows.map(r=>`<div class="meter"><div class="mlab">${r[0]}</div><div class="bar"><div class="fill"></div><div class="peak"></div></div><div class="mdb">—</div></div>`).join('');
    m.dataset.built='1';
  }
  if(Date.now()-mLastOk<1200) return;   // fast meter loop owns the bars and numbers
  const fills=m.querySelectorAll('.fill'), dbs=m.querySelectorAll('.mdb');
  const raw=[...(s.inputs||[]).slice(0,2),...(s.outputs||[]).slice(0,4)];
  rows.forEach((r,i)=>{
    fills[i].style.width=(r[1]||0)+'%';
    if(dbs[i])dbs[i].textContent=fmtDb(raw[i]);
  });
}
// Meters read the noise floor (~-86) when nothing plays; that is silence, not a
// fault, so show it as such rather than as a number pinned to the -60 bar floor.
const fmtDb=v=>(v==null||!isFinite(v))?'—':(v<=-80?'−∞':(v>0?'+':'')+v.toFixed(1));

// Header readout. Source is worth watching — a flipped input is a real failure
// mode and the reason Fix Audio exists. Master gain is not: it is cut-only and
// nothing in this app writes it, so it read 0.0 dB permanently. Show the live
// input level in that slot instead, and surface gain ONLY when it is non-zero,
// which would mean something outside this app (IR remote, miniDSP app) moved it.
let hdrSrc='', hdrGain=null;
function hdrPaint(inDb){
  if(!hdrSrc){$('srcLbl').textContent='';return}
  let t=hdrSrc+' · in '+fmtDb(inDb);
  if(hdrGain!=null&&Math.abs(hdrGain)>0.05)t+=' · gain '+hdrGain.toFixed(1)+'dB';
  $('srcLbl').textContent=t;
}
const inPeak=a=>{const v=(a||[]).slice(0,2).filter(x=>x!=null&&isFinite(x));
  return v.length?Math.max(...v):null};

// ---- fast meters: 8Hz poll of minidspd via /api/meters + ballistics ----
// instant attack, ~1.5s full-scale decay, peak-hold marker (1.5s hold, then falls)
let mTarget=[0,0,0,0,0,0], mDisp=[0,0,0,0,0,0], mPeak=[0,0,0,0,0,0],
    mPeakAt=[0,0,0,0,0,0], mLastOk=0, mLastFrame=0;
// raw dB for the numeric column: same instant-attack/timed-decay shape as the
// bars, but held in dB so the reading stays honest below the -60 bar floor
let mDbT=[-99,-99,-99,-99,-99,-99], mDbD=[-99,-99,-99,-99,-99,-99];
const dbPct=db=>Math.max(0,Math.min(100,(db+60)/60*100));
async function pollMeters(){
  try{
    const r=await fetch('/api/meters',{headers:hdrs()});
    const j=await r.json();
    if(j.ok){
      mLastOk=Date.now();
      const raw=[...(j.inputs||[]).slice(0,2),...(j.outputs||[]).slice(0,4)];
      mTarget=raw.map(dbPct);
      mDbT=raw.map(v=>(v==null||!isFinite(v))?-99:v);
    }
  }catch(e){}
}
function meterFrame(ts){
  requestAnimationFrame(meterFrame);
  const m=$('meters');
  if(!m||!m.dataset.built||Date.now()-mLastOk>1200) return;  // fast path dead -> slow render owns bars
  const dt=Math.min(0.1,(ts-mLastFrame)/1000||0.016); mLastFrame=ts;
  const DECAY=100/1.5, PEAK_HOLD=1500, PEAK_FALL=100/1.0, DB_DECAY=60/1.5;
  const fills=m.querySelectorAll('.fill'), peaks=m.querySelectorAll('.peak'),
        dbs=m.querySelectorAll('.mdb');
  for(let i=0;i<6;i++){
    const t=mTarget[i]||0;
    mDisp[i]=t>=mDisp[i]?t:Math.max(t,mDisp[i]-DECAY*dt);          // instant attack, timed decay
    if(t>=mPeak[i]){mPeak[i]=t;mPeakAt[i]=ts;}
    else if(ts-mPeakAt[i]>PEAK_HOLD){mPeak[i]=Math.max(t,mPeak[i]-PEAK_FALL*dt);}
    if(fills[i])fills[i].style.width=mDisp[i]+'%';
    if(peaks[i]){peaks[i].style.left='calc('+mPeak[i]+'% - 2px)';
      peaks[i].classList.toggle('on',mPeak[i]>1);}
    const d=mDbT[i];
    mDbD[i]=d>=mDbD[i]?d:Math.max(d,mDbD[i]-DB_DECAY*dt);
    if(dbs[i])dbs[i].textContent=fmtDb(mDbD[i]);
  }
  hdrPaint(Math.max(mDbD[0],mDbD[1]));
}
setInterval(pollMeters,125);
requestAnimationFrame(meterFrame);

// ---- SPL meter -------------------------------------------------------------
// The phone is the thing sitting at the listening position, so the phone's mic
// is the useful sensor. Three caveats are engineered around rather than hidden:
//   1. getUserMedia needs a secure context. iOS blocks it over plain http, so
//      this card only works on the https listener (see /ca.crt).
//   2. iOS applies AGC/NS/AEC by default, which actively fights the measurement.
//      Constraints turn them off, and we re-read the track settings to check the
//      constraints were actually honoured rather than trusting they were.
//   3. A mic reports dBFS, not SPL. Absolute numbers mean nothing until you
//      calibrate against a known reference; until then this says so plainly.
const TLS_PORT="__TLS_PORT__";
const lsGet=(k,d)=>{try{const v=localStorage.getItem(k);return v==null?d:v}catch(e){return d}};
const lsSet=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
let splCtx=null,splStream=null,splAn=null,splBuf=null,splRaf=0,splLast=0,splSrc=null,splNodes=[],
    splMs=0,splNorm=0,splRaw=-Infinity,splMax=-Infinity,
    splWt=lsGet('splWt','A'),splResp=lsGet('splResp','F'),
    splOffset=parseFloat(lsGet('splOffset','0'))||0;

function splSegs(){
  document.querySelectorAll('#splWtSeg button').forEach(b=>b.classList.toggle('on',b.dataset.v===splWt));
  document.querySelectorAll('#splRespSeg button').forEach(b=>b.classList.toggle('on',b.dataset.v===splResp));
  $('splUnit').textContent=(splWt==='Z'?'dB(Z)':'dB('+splWt+')')+(splResp==='F'?' fast':' slow');
  $('splOff').textContent=splOffset?('calibrated — offset '+splOffset.toFixed(1)+' dB'):'uncalibrated — relative only';
}
// A- and C-weighting as biquad sections. C is a double real pole at 20.6 Hz and
// another at 12194 Hz (Q=0.5 is exactly critical damping = repeated real pole);
// A adds the 107.65/737.86 Hz pair, which is one section at their geometric mean
// with Q = sqrt(p1*p2)/(p1+p2). Z is the unweighted signal.
function splChain(){
  const c=splCtx,mk=(t,f,q)=>{const b=c.createBiquadFilter();b.type=t;b.frequency.value=f;b.Q.value=q;return b};
  // Rewire only. Changing weighting must NEVER touch the MediaStream: track.stop()
  // is permanent, and a rebuilt graph on an ended track feeds silence, which the
  // exponential average then walks steadily downward instead of failing loudly.
  try{splSrc.disconnect()}catch(e){}
  splNodes.forEach(n=>{try{n.disconnect()}catch(e){}});
  const nodes=[];
  if(splWt!=='Z'){nodes.push(mk('highpass',20.598997,0.5),mk('lowpass',12194.217,0.5))}
  if(splWt==='A'){nodes.push(mk('highpass',281.78,0.33333))}
  splNodes=nodes;
  let n=splSrc;nodes.forEach(f=>{n.connect(f);n=f});n.connect(splAn);
  // Both weightings are defined as 0 dB at 1 kHz. Measure the chain there rather
  // than hardcoding the +2.0/+0.06 dB book constants — self-checking, and it
  // stays correct if the sections above are ever changed.
  const f=new Float32Array([1000]),m=new Float32Array(1),ph=new Float32Array(1);
  let g=1;nodes.forEach(b=>{b.getFrequencyResponse(f,m,ph);g*=m[0]});
  splNorm=g>0?-20*Math.log10(g):0;
  // Measured against the published curves at 48 kHz: within 0.11 dB from 31.5 Hz
  // to 2 kHz, +0.65 dB at 8 kHz, -3.1 dB at 16 kHz. The top-end error is
  // bilinear-transform warping near Nyquist, inherent to biquads at this rate.
  // It is immaterial here: the top octave carries a few percent of the energy in
  // music and A-weighting already cuts it ~7 dB, so the effect on a broadband
  // reading is under 0.2 dB — far inside the error of an uncalibrated phone mic.
}
function splFrame(ts){
  splRaf=requestAnimationFrame(splFrame);
  if(!splAn)return;
  splAn.getFloatTimeDomainData(splBuf);
  let sum=0;for(let i=0;i<splBuf.length;i++)sum+=splBuf[i]*splBuf[i];
  const ms=sum/splBuf.length;
  const dt=splLast?Math.min(.25,(ts-splLast)/1000):.05;splLast=ts;
  // IEC time weighting is an exponential average of POWER (125 ms fast, 1 s
  // slow) — averaging dB instead would skew every reading low.
  const a=1-Math.exp(-dt/(splResp==='F'?.125:1));
  splMs=splMs?splMs+(ms-splMs)*a:ms;
  splRaw=splMs>0?10*Math.log10(splMs)+splNorm:-Infinity;
  const spl=splRaw+splOffset;
  if(isFinite(spl)&&spl>splMax)splMax=spl;
  $('splNum').textContent=isFinite(spl)?spl.toFixed(1):'—';
  $('splMax').textContent=isFinite(splMax)?splMax.toFixed(1):'—';
}
async function splStart(){
  const W=$('splWarn');W.innerHTML='';
  if(!window.isSecureContext||!navigator.mediaDevices){
    const u='https://'+location.hostname+':'+TLS_PORT+'/'+(TOKEN?('?t='+encodeURIComponent(TOKEN)):'');
    W.innerHTML='The mic needs a secure context — iOS blocks it over http. Open '+
      '<a href="'+u+'">this page over https</a> instead (install the certificate first: '+
      '<a href="/ca.crt">/ca.crt</a>).';
    return;
  }
  try{
    splStream=await navigator.mediaDevices.getUserMedia({audio:{
      echoCancellation:false,noiseSuppression:false,autoGainControl:false,channelCount:1}});
  }catch(e){W.textContent='Mic unavailable: '+(e.name||e.message||e);return}
  // trust nothing: iOS may ignore the constraints, and AGC would quietly ruin it
  try{
    const st=splStream.getAudioTracks()[0].getSettings()||{};
    const on=['autoGainControl','noiseSuppression','echoCancellation'].filter(k=>st[k]===true);
    if(on.length)W.textContent='Warning: '+on.join(', ')+' still active — iOS ignored the constraint, so levels are being processed and readings will compress.';
  }catch(e){}
  splCtx=new (window.AudioContext||window.webkitAudioContext)();
  try{await splCtx.resume()}catch(e){}
  splAn=splCtx.createAnalyser();splAn.fftSize=2048;
  splBuf=new Float32Array(splAn.fftSize);
  splSrc=splCtx.createMediaStreamSource(splStream);
  splChain();
  splMs=0;splLast=0;splMax=-Infinity;
  $('splBtn').textContent='Stop mic';$('splBtn').classList.add('on');
  splRaf=requestAnimationFrame(splFrame);
}
function splStop(){
  if(splRaf)cancelAnimationFrame(splRaf);splRaf=0;
  if(splStream)splStream.getTracks().forEach(t=>t.stop());
  if(splCtx){try{splCtx.close()}catch(e){}}
  splStream=null;splCtx=null;splAn=null;splSrc=null;splNodes=[];splMs=0;splRaw=-Infinity;
  $('splBtn').textContent='Start mic';$('splBtn').classList.remove('on');
  $('splNum').textContent='—';
}
const splToggle=()=>splCtx?splStop():splStart();
function splSetWt(v){
  splWt=v;lsSet('splWt',v);splSegs();
  if(splCtx&&splSrc){splChain();splMs=0;splLast=0}   // rewire in place, mic untouched
}
function splSetResp(v){splResp=v;lsSet('splResp',v);splSegs()}
function splResetMax(){splMax=-Infinity;$('splMax').textContent='—'}
function splCalibrate(){
  const ref=parseFloat($('splRef').value);
  if(!isFinite(ref)||!isFinite(splRaw)){
    $('splWarn').textContent='Start the mic and type the reading from a reference meter first.';return}
  splOffset=ref-splRaw;lsSet('splOffset',String(splOffset));
  splMax=-Infinity;$('splRef').value='';$('splRef').blur();splSegs();
}
// iOS suspends capture in the background anyway; release it cleanly so the
// orange mic indicator does not linger and the stream is not held open.
document.addEventListener('visibilitychange',()=>{if(document.hidden&&splCtx)splStop()});
splSegs();

function render(s){
  if(!s){$('dot').classList.remove('live');return}
  $('dot').classList.add('live');
  const n=s.now||{};
  const st=(n.state||'').toLowerCase();
  $('npState').textContent = st==='playing'?'Now Playing':(st==='paused'?'Paused':(st==='notrunning'?'Music closed':(st||'—')));
  $('npTitle').textContent = n.title||'Nothing playing';
  $('npArtist').textContent = [n.artist,n.album].filter(Boolean).join(' · ');
  $('pp').textContent = st==='playing'?'⏸':'▶';
  if(lovePending!==null && (!!n.faved===lovePending || Date.now()-lovePendingAt>4000)) lovePending=null;
  const fav = lovePending!==null ? lovePending : !!n.faved;
  $('loveBtn').classList.toggle('on', fav);
  $('loveBtn').style.visibility = n.title ? 'visible' : 'hidden';
  // album artwork: refetch only when the track changes
  const ak=(n.title||'')+'|'+(n.artist||'')+'|'+(n.album||'');
  if(ak!==artKey){
    artKey=ak;
    const img=$('npArt');
    img.classList.remove('show');
    if(n.title){img.src='/api/art?t='+encodeURIComponent(TOKEN)+'&r='+Date.now();}
    else{img.removeAttribute('src');}
  }
  // seek/timeline: re-anchor to the server's truth each poll; the local ticker
  // advances the bar smoothly in between (poll is only ~1.4s).
  if(!scrubbing){
    npDur = n.duration||0; npPos = n.position||0; posAt = Date.now();
    npPlaying = (st==='playing');
    paintSeek();
  }
  if(s.volume!=null && !dragging){$('vol').value=s.volume; volLive(s.volume,true)}
  hdrSrc=s.source||''; hdrGain=(s.master_gain!=null?s.master_gain:null);
  if(Date.now()-mLastOk>=1200) hdrPaint(inPeak(s.inputs));
  // mute
  const mb=$('muteBtn');mb.classList.toggle('on',!!s.mute);mb.textContent=s.mute?'Muted':'Mute';
  // subs (Nexia)
  const sub=s.sub||{};
  const smb=$('subMuteBtn');
  if(sub.ok){
    // optimistic: hold at the user's just-set value until the device confirms it (slow ~1.2s write) or 6s timeout
    if(subPending!==null && (Math.abs((sub.level||0)-subPending)<0.01 || Date.now()-subPendingAt>3000)) subPending=null;
    if(subMutePending!==null && (!!sub.muted===subMutePending || Date.now()-subPendingAt>3000)) subMutePending=null;
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
let lovePending=null, lovePendingAt=0, artKey=null;
// ---- library search: debounced, tap a result to play it (album follows) ----
let srchTimer=null, srchSeq=0;
function srchInput(){
  clearTimeout(srchTimer);
  const q=$('srch').value.trim();
  if(q.length<2){$('srchRes').innerHTML='';return;}
  srchTimer=setTimeout(async()=>{
    const my=++srchSeq;
    try{
      const r=await fetch('/api/search?q='+encodeURIComponent(q),{headers:hdrs()});
      const j=await r.json();
      if(my!==srchSeq)return;                       // stale response, newer query in flight
      const R=$('srchRes');
      if(!j.ok){R.innerHTML='<div class="srchempty">search error</div>';return;}
      if(!j.results.length){R.innerHTML='<div class="srchempty">no matches</div>';return;}
      R.innerHTML=j.results.map(t=>
        '<div class="srchrow" data-pid="'+t.pid+'"><div class="t">'+esc(t.title)+'</div>'+
        '<div class="a">'+esc(t.artist)+(t.album?' · '+esc(t.album):'')+'</div></div>').join('');
      R.querySelectorAll('.srchrow').forEach(row=>row.onclick=()=>{
        post('/api/play-track?pid='+row.dataset.pid);
        $('srch').value='';R.innerHTML='';$('srch').blur();
      });
    }catch(e){}
  },350);
}
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function fixAudio(){
  const b=$('fixBtn');b.disabled=true;const t=b.textContent;b.textContent='Fixing…';
  try{
    const r=await fetch('/api/fix-audio',{method:'POST',headers:hdrs()});
    const j=await r.json();
    if(j.status)render(j.status);
    toast(j.ok?(j.out||'audio path reset'):(j.err||'reset failed'),!j.ok,8000); // full text, wraps, 8s to read
  }catch(e){toast('network error',true)}
  b.disabled=false;b.textContent=t;
}
async function toggleLove(){
  const on=!$('loveBtn').classList.contains('on');
  lovePending=on; lovePendingAt=Date.now();
  $('loveBtn').classList.toggle('on',on);
  if(await post('/api/love?on='+(on?1:0))===false){lovePending=null;}
}
function subPct(v){return Math.max(0,Math.min(100,(v+18)/24*100));} // -18..+6 UI scale -> 0..100 (device = UI - 6)
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


def serve_tls():
    """HTTPS listener, for the one feature that cannot work without it.

    Missing certs are not fatal: a fresh clone has no certs/ (gitignored), and
    everything except the SPL meter works fine over plain http. Log and skip.
    """
    if not (os.path.exists(CERTFILE) and os.path.exists(KEYFILE)):
        print("TLS off — no certs/server.crt; run ./make-cert.sh to enable the SPL meter")
        return
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERTFILE, KEYFILE)
        httpsd = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
        httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
    except Exception as e:
        print("TLS off — %s" % e)
        return
    print("HiFi Dashboard TLS on https://0.0.0.0:%d  (mic/SPL meter needs this one)" % TLS_PORT)
    httpsd.serve_forever()


def main():
    threading.Thread(target=serve_tls, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    host = (socket.gethostname() or "localhost").split(".")[0]
    print("HiFi Dashboard on http://0.0.0.0:%d  (open http://%s.local:%d from the phone)"
          % (PORT, host, PORT))
    if TOKEN:
        print("Token auth ENABLED — append ?t=%s" % TOKEN)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
