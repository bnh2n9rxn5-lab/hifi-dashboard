#!/usr/bin/env python3
"""
Nexia sub-bus control for the HiFi Dashboard.

Domain logic (sub level/mute, optimistic cache, write-vs-read race guard,
audit log) lives here; the telnet transport is the published biamp-ntp
library (persistent connection, response-driven framing, command pacing,
-ERR:# 0x16 defense) — this dashboard is its first real-world consumer.
Install/update: /usr/bin/python3 -m pip install --user biamp-ntp

Signal facts (verified live 2026-07-05 — see the nexia-text-protocol-control memo):
  - Device number 1; PM Stereo Line Output block instance = 8 (new sub config).
  - The subs are the stereo pair on output channels 5 & 6, driven ganged.
  - OUTLVLPM  = output level; OUTMUTEPM = mute (0/1). Index = channel.
  - The output block REJECTS boost: SET > 0 dB -> -ERR:XACTION ERROR. The
    +6 dB makeup gain lives on the input block (INPLVLPML inst 7); server.py
    translates UI scale <-> device scale (SUB_MAKEUP).

The unit's instance IDs can renumber if the Nexia design is ever recompiled
and re-pushed from the Windows software. If control starts erroring, re-scan
(`biamp-ntp --host NEXIA_HOST scan OUTLVLPM`) and update OUT_INST below.
"""
import threading
import time

from biamp_ntp import BiampNTP, BiampError

HOST = "NEXIA_HOST"
PORT = 23
DEV = 1              # Nexia device number (GET 0 DEVID -> 1)
OUT_INST = 8         # PM Stereo Line Output block instance (new sub config, sent 2026-07-05)
SUB_CHANS = (5, 6)   # sub amp is fed from output channels 5 & 6 (stereo)

# Usable range for the device-side clamp. The deployed design's output block
# hard-rejects anything above 0 dB (verified live 2026-07-05); raising that
# ceiling would need a design change pushed from the Windows Nexia software.
# Cut-only is also kinder to the drivers.
LVL_MIN = -40.0
LVL_MAX = 0.0

# Persistent, thread-safe (command() serializes internally), auto-reconnects
# if the device drops the idle session.
_dsp = BiampNTP(HOST, device=DEV, port=PORT, timeout=3.0)

# Write/read coordination. BiampNTP serializes single commands, but a write
# is 2 SETs and a read is 2 GETs, and locks aren't FIFO — a poll's GETs can
# interleave a write and read the old value (caught by the concurrency
# regression test when adopting the library). Scheme: writes are atomic only
# against other writes (_write_lock); reads never block anything, but any
# read that overlaps a write — _pending > 0 or _write_ts moved — is simply
# discarded, since the optimistic cache is authoritative during a write.
_write_lock = threading.Lock()   # writer-vs-writer batch atomicity
_state = threading.Lock()        # guards _pending/_write_ts transitions
_pending = 0                     # writes queued or in flight

_cache = {"ok": False, "level": 0.0, "muted": False, "ts": 0.0, "error": "not polled yet"}
_write_ts = 0.0   # timestamp of the last write; a concurrent read must not clobber a newer write

LOG = "/tmp/nexia-sub.log"   # write/read audit trail (kept from the spring-back hunt)


def _log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


# ---- reads ---------------------------------------------------------------

def read_sub():
    """Return {ok, level, muted[, error]} read straight from the unit (ch5)."""
    try:
        lv = _dsp.get_float("OUTLVLPM", OUT_INST, SUB_CHANS[0])
        mu = _dsp.get_bool("OUTMUTEPM", OUT_INST, SUB_CHANS[0])
        return {"ok": True, "level": lv, "muted": mu}
    except (BiampError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def status(now, max_age=4.0):
    """Cached sub state. `now` is a monotonic-ish timestamp (time.time()).

    Cheap on the hot poll path: only touches the device when the cache is
    older than max_age (fast transport allows 4s; was 8s on the old one).
    A fresh read is skipped/discarded whenever it overlaps a write — the
    optimistic cache is authoritative until the write completes.
    """
    if _cache["ok"] and (now - _cache["ts"]) < max_age:
        return dict(_cache)
    if _pending:
        return dict(_cache)      # write queued or in flight — don't even read
    started = _write_ts
    fresh = read_sub()
    if _pending or _write_ts != started:
        # a write overlapped our read — its value is authoritative, don't clobber it
        return dict(_cache)
    if fresh["ok"]:
        if abs(fresh["level"] - _cache["level"]) > 0.01 or fresh["muted"] != _cache["muted"]:
            _log("READ device=%s/%s differs from cache=%s/%s -> cache updated"
                 % (fresh["level"], fresh["muted"], _cache["level"], _cache["muted"]))
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

def set_level(db, now=None, src="api"):
    try:
        db = max(LVL_MIN, min(LVL_MAX, float(db)))
    except (TypeError, ValueError):
        return (2, "", "bad sub level")
    _log("WRITE level=%.1f src=%s" % (db, src))
    global _pending, _write_ts
    with _state:
        _pending += 1
        if now is not None:
            _write_ts = now
            _note(now, level=db)  # optimistic: cache reflects intent NOW
    try:
        with _write_lock:
            for ch in SUB_CHANS:
                _dsp.set("OUTLVLPM", OUT_INST, ch, db)
    except BiampError as e:
        _log("FAIL level=%.1f: %s" % (db, e))
        return (1, "", "nexia: %s" % e)
    except OSError as e:
        _log("FAIL level=%.1f OSError: %s" % (db, e))
        return (1, "", "nexia unreachable: %s" % e)
    finally:
        with _state:
            _pending -= 1
    return (0, "sub level %.1f dB" % db, "")


def set_mute(on, now=None, src="api"):
    val = 1 if on else 0
    _log("WRITE mute=%s src=%s" % (val, src))
    global _pending, _write_ts
    with _state:
        _pending += 1
        if now is not None:
            _write_ts = now
            _note(now, muted=bool(on))  # optimistic (see set_level)
    try:
        with _write_lock:
            for ch in SUB_CHANS:
                _dsp.set("OUTMUTEPM", OUT_INST, ch, val)
    except BiampError as e:
        _log("FAIL mute=%s: %s" % (val, e))
        return (1, "", "nexia: %s" % e)
    except OSError as e:
        _log("FAIL mute=%s OSError: %s" % (val, e))
        return (1, "", "nexia unreachable: %s" % e)
    finally:
        with _state:
            _pending -= 1
    return (0, "sub %s" % ("muted" if on else "unmuted"), "")


if __name__ == "__main__":
    # Self-test: read, nudge -3, read back, restore, mute cycle. Non-destructive.
    def show(tag):
        print("%-10s %s" % (tag, read_sub()))
    t0 = time.monotonic()
    show("before")
    print("read took %.2fs (first op includes connect)" % (time.monotonic() - t0))
    orig = read_sub()
    if not orig["ok"]:
        raise SystemExit("device unreachable")
    t0 = time.monotonic()
    print("set -3 ->", set_level(orig["level"] - 3.0))
    print("write took %.2fs" % (time.monotonic() - t0))
    time.sleep(0.2); show("after")
    print("restore ->", set_level(orig["level"]))
    time.sleep(0.2); show("restored")
    print("mute on ->", set_mute(True)); time.sleep(0.2); show("muted")
    print("mute off ->", set_mute(False)); time.sleep(0.2); show("unmuted")
