#!/usr/bin/env python3
"""
Nexia sub-bus control for the HiFi Dashboard.

Speaks the Biamp Nexia Text Protocol (NTP) to the Nexia PM over telnet (port 23,
no auth) and exposes just the handful of operations the dashboard needs: read /
set the sub level and mute. Stdlib only (socket), same as server.py.

Signal facts (verified live 2026-07-05 — see the nexia-text-protocol-control memo):
  - Device number 1; PM Stereo Line Output block instance = 103.
  - The subs are the stereo pair on output channels 5 & 6, driven ganged.
  - OUTLVLPM  = output level (-100..+12 dB); OUTMUTEPM = mute (0/1). Index = channel.

The unit's instance IDs can renumber if the Nexia design is ever recompiled and
re-pushed from the Windows software. If control starts erroring, re-scan for the
output block (GET 1 OUTLVLPM <n> 1 sweeps) and update OUT_INST below.
"""
import socket
import threading
import time

HOST = "NEXIA_HOST"
PORT = 23
DEV = 1              # Nexia device number (GET 0 DEVID -> 1)
OUT_INST = 8         # PM Stereo Line Output block instance (new sub config, sent 2026-07-05)
SUB_CHANS = (5, 6)   # sub amp is fed from output channels 5 & 6 (stereo)

# Usable slider range for the UI/clamp. The block allows +12 but boosting subs
# hard is asking for trouble; cap the boost and let the level pull well down.
LVL_MIN = -40.0
LVL_MAX = 6.0

_lock = threading.Lock()
_cache = {"ok": False, "level": 0.0, "muted": False, "ts": 0.0, "error": "not polled yet"}
_write_ts = 0.0   # timestamp of the last write; a concurrent read must not clobber a newer write


def _strip_iac(b):
    """Drop telnet IAC negotiation triples (0xFF x y) so they don't corrupt replies."""
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i] == 0xFF:
            i += 3
            continue
        out.append(b[i])
        i += 1
    return bytes(out)


def _reply(raw):
    lines = [l for l in _strip_iac(raw).replace(b"\r", b"").split(b"\n") if l.strip()]
    return lines[-1].decode("latin-1", "replace").strip() if lines else ""


def _drain(s, settle=0.25):
    """Read whatever's buffered (banner / IAC / reply), letting it settle first.

    The Nexia dribbles its reply out just after the echo; a single immediate
    recv() catches a partial frame and splits IAC triples. A short settle plus a
    brief non-blocking mop-up gets the whole frame in one piece.
    """
    time.sleep(settle)
    chunks = bytearray()
    s.settimeout(0.15)
    while True:
        try:
            b = s.recv(4096)
            if not b:
                break
            chunks += b
        except socket.timeout:
            break
    return bytes(chunks)


def _run(cmds, timeout=3.0):
    """Open one telnet session, send each command, return list of reply strings.

    Serialized via _lock — the Nexia telnet server is happiest with one client.
    Raises OSError on connect/socket failure; callers translate to (rc,out,err).
    """
    with _lock:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        try:
            _drain(s)  # swallow banner + IAC negotiation
            replies = []
            for c in cmds:
                s.settimeout(timeout)
                s.sendall((c + "\n").encode())
                replies.append(_reply(_drain(s)) or "<timeout>")
            return replies
        finally:
            s.close()


def _ok(reply):
    return reply == "+OK"


# ---- reads ---------------------------------------------------------------

def read_sub():
    """Return {ok, level, muted[, error]} read straight from the unit (ch5)."""
    try:
        lv, mu = _run([
            "GET %d OUTLVLPM %d %d" % (DEV, OUT_INST, SUB_CHANS[0]),
            "GET %d OUTMUTEPM %d %d" % (DEV, OUT_INST, SUB_CHANS[0]),
        ])
        if "ERR" in lv or "ERR" in mu or "timeout" in (lv + mu):
            return {"ok": False, "error": "device: %s / %s" % (lv, mu)}
        return {"ok": True, "level": float(lv), "muted": (mu.strip() == "1")}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def status(now, max_age=8.0):
    """Cached sub state. `now` is a monotonic-ish timestamp (time.time()).

    Cheap on the hot poll path: only touches the device when the cache is older
    than max_age. Callers that just changed state should use note_* to refresh.
    """
    if _cache["ok"] and (now - _cache["ts"]) < max_age:
        return dict(_cache)
    started = _write_ts
    fresh = read_sub()
    if _write_ts != started:
        # a write landed while we were reading — its value is authoritative, don't clobber it
        return dict(_cache)
    if fresh["ok"]:
        _cache.update(fresh, ts=now, error="")
    else:
        _cache.update(ok=False, ts=now, error=fresh.get("error", "read failed"))
    return dict(_cache)


def _note(now, **kw):
    _cache.update(kw)
    _cache["ok"] = True
    _cache["ts"] = now
    _cache["error"] = ""


# ---- writes (return (rc, out, err) to match server.py's act_* convention) --

def set_level(db, now=None):
    try:
        db = max(LVL_MIN, min(LVL_MAX, float(db)))
    except (TypeError, ValueError):
        return (2, "", "bad sub level")
    if now is not None:
        global _write_ts
        _write_ts = now
        _note(now, level=db)  # optimistic: cache reflects intent NOW so polls don't lag the ~1.2s write
    try:
        rep = _run(["SET %d OUTLVLPM %d %d %.2f" % (DEV, OUT_INST, ch, db) for ch in SUB_CHANS])
    except OSError as e:
        return (1, "", "nexia unreachable: %s" % e)
    if all(_ok(r) for r in rep):
        return (0, "sub level %.1f dB" % db, "")
    return (1, "", "nexia: %s" % " / ".join(rep))


def set_mute(on, now=None):
    val = 1 if on else 0
    if now is not None:
        global _write_ts
        _write_ts = now
        _note(now, muted=bool(on))  # optimistic (see set_level)
    try:
        rep = _run(["SET %d OUTMUTEPM %d %d %d" % (DEV, OUT_INST, ch, val) for ch in SUB_CHANS])
    except OSError as e:
        return (1, "", "nexia unreachable: %s" % e)
    if all(_ok(r) for r in rep):
        return (0, "sub %s" % ("muted" if on else "unmuted"), "")
    return (1, "", "nexia: %s" % " / ".join(rep))


if __name__ == "__main__":
    # Self-test: read, nudge -3, read back, restore, read back. Non-destructive.
    import time
    def show(tag):
        print("%-10s %s" % (tag, read_sub()))
    show("before")
    print("set -3 ->", set_level(-3.0))
    time.sleep(0.2); show("after")
    print("restore ->", set_level(0.0))
    time.sleep(0.2); show("restored")
    print("mute on ->", set_mute(True)); time.sleep(0.2); show("muted")
    print("mute off ->", set_mute(False)); time.sleep(0.2); show("unmuted")
