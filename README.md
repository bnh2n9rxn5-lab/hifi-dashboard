# MB Hi-Fi

A **phone-friendly web remote for a whole listening room** — Apple Music transport, miniDSP presets and meters, Biamp Nexia sub level, and an SPL meter that uses the phone's own microphone. One dependency-free Python file, served to your phone over the LAN.

> **Read this first:** this is a personal system published as-is, not a product. It assumes one particular rig — a Mac running Apple Music, a miniDSP driven by [minidsp-rs](https://github.com/mrene/minidsp-rs), and a Biamp Nexia whose block instance IDs are hardcoded in `nexia.py`. It will not work out of the box on your rig, and parts of it (sub channel mapping, output block instance, preset trims) need editing for yours. It's here because the Apple Music AppleScript findings and the SPL meter maths are worth reading even if you never run it. If you only want DSP control, the sibling project [minidsp-dash](https://github.com/av-dsp-tools/minidsp-dash) is the general-purpose, hardware-agnostic one.

## What it does

- **Now Playing** — title/artist/album, album artwork, scrubbing, a ♥ favourite button, and transport that can actually jump *back* a track (harder than it sounds — see below)
- **Library** — search, plus playlist / artist / genre pickers. Playlists play in their own order; artist picks fall back to album context when an artist is too thin to fill a queue
- **Volume** — Apple Music's own volume, with quiet/medium/loud presets
- **Subs** — Biamp Nexia sub bus level and mute, on a UI scale that hides the device's input-makeup/output-trim arrangement
- **Presets** — miniDSP config 1–4
- **Meters** — miniDSP input/output levels at 8 Hz with proper ballistics and a numeric dB readout
- **Room level** — a real SPL meter running on the phone's mic, with A/C/Z weighting and IEC fast/slow response
- **Fix Audio** — one tap to rebuild a wedged USB audio path (see Troubleshooting)

## Requirements

- macOS with **Music.app** (the Apple Music side is all AppleScript)
- [**minidsp-rs**](https://github.com/mrene/minidsp-rs) at `/usr/local/bin/minidsp`, and `minidspd` running for the fast meter path
- [**biamp-ntp**](https://pypi.org/project/biamp-ntp/) (`pip install biamp-ntp`) for the Nexia sub bus
- Python 3.8+ — stdlib only, no other dependencies
- The `dsp-*` helper scripts from `helpers/` deployed to `/usr/local/bin/`

## Setup

```bash
cp helpers/dsp-* /usr/local/bin/ && chmod 755 /usr/local/bin/dsp-*

NEXIA_HOST=192.168.1.50 \
DSP_WEB_TOKEN=some-secret \
python3 server.py
# then open http://<this-machine>.local:8765/?t=some-secret from your phone
```

| Variable | Default | Meaning |
|---|---|---|
| `NEXIA_HOST` | *(required)* | Address of the Biamp Nexia. No default — the app refuses to start without it |
| `NEXIA_PORT` | `23` | Nexia telnet port |
| `DSP_WEB_PORT` | `8765` | HTTP listen port |
| `DSP_WEB_TLS_PORT` | `8766` | HTTPS listen port (only needed for the SPL meter) |
| `DSP_WEB_TOKEN` | *(off)* | If set, every request needs `?t=<token>` or an `X-Token` header |

> **Auth note:** the token gates control of your system but travels in plain HTTP on your LAN. Fine for a home network; don't port-forward it.

### Run at login (macOS)

Save a LaunchAgent at `~/Library/LaunchAgents/com.hifi.dashboard.plist` with `RunAtLoad`, `KeepAlive`, and your environment variables, then `launchctl bootstrap gui/$(id -u) <plist>`.

**Two traps worth knowing before they cost you an afternoon:**

```bash
# after editing server.py — KeepAlive restarts on CRASH, not on edit
launchctl kickstart -k gui/$(id -u)/com.hifi.dashboard

# after editing the plist — kickstart does NOT re-read it
launchctl bootout    gui/$(id -u)/com.hifi.dashboard
launchctl bootstrap  gui/$(id -u) ~/Library/LaunchAgents/com.hifi.dashboard.plist
```

A stale process is a genuinely nasty bug to chase: the source on disk shows a fix the running server has never loaded. If a symptom contradicts the code, compare `ps -Ao pid,lstart | grep server.py` against the file's mtime *first*.

Also bump `APP_VERSION` on any change to the served page. The client self-reloads on a version mismatch, so an un-bumped edit leaves phones on the cached old UI indefinitely.

## The SPL meter

The phone is the thing sitting at the listening position, so the phone's mic is the useful sensor. `getUserMedia` requires a secure context and iOS blocks it over plain HTTP, so the server also listens on TLS:

```bash
./make-cert.sh        # root CA + the leaf it signs
```

Then on the phone, open `http://<host>:8765/ca.crt` in Safari, install the profile, and — the step everyone misses — enable it under **Settings → General → About → Certificate Trust Settings**. Installing alone is not enough. Re-add the home screen app from the `https://` URL.

Two certificates rather than one, so re-issuing the leaf after a DHCP change needs no re-install on the phone. `certs/` is gitignored.

**On accuracy, honestly:** a mic reports dBFS, not SPL, so the reading is relative until you calibrate it against a reference meter — the card says so until you do. The A/C weighting filters measure within **0.11 dB of the published curves from 31.5 Hz to 2 kHz**, drifting to −3.1 dB at 16 kHz from bilinear warping near Nyquist (immaterial on a broadband reading). Time weighting is an exponential average of *power*; averaging dB skews every reading low. Expect soft numbers at the extremes regardless — iPhone mics roll off bass and limit around 100–105 dB SPL, so **dBC** is the more informative reading with subs running. iOS may also ignore the AGC/noise-suppression constraints, so the app re-reads the track settings after the grant and warns you if the OS is still processing the signal.

## Apple Music via AppleScript — findings

Most of the difficulty in this project was Music.app, not the DSPs. Verified live on macOS 15:

- **Favourite** is `favorited of current track`; the old `loved` property errors.
- **Artwork** can't be returned by osascript, so the script writes `raw data of artwork 1` to a temp file and the server serves it.
- **Never queue an anonymous track list** (`play (every track whose ...)`). With no `current playlist`, `back track` can only ever restart the current song. Build a real playlist instead — that's what the `HiFi Queue` you'll see in the sidebar is for.
- **`duplicate` needs a specifier, not an evaluated list** — `duplicate (every track ... whose album is x) to user playlist "…"` works; a list variable fails with `-10006`. Persistent IDs survive duplication.
- **Don't play a playlist you just built.** Music accepts the current-playlist change but ignores the track reference and opens at track 1. Settle first, then confirm you landed and re-issue.
- **`whose name is (name of t)` errors `-1728`** — the inner property isn't evaluated inside the filter. Assign it to a variable first.
- **Smart playlists mutate under the player.** A smart playlist's membership is
  recomputed live, so playing one directly means the queue changes as you listen:
  a track that stops matching the rule is yanked mid-play (un-favouriting while
  playing a favourites-based list does exactly this, and reads as the track
  ending early), and every index after it shifts. Since `back track` steps by
  playlist POSITION and not play history, prev then lands on something you never
  heard. `dsp-play` snapshots smart playlists into `HiFi Queue` and plays that;
  ordinary playlists are still played directly, since they don't move.
- **`play` does not evict a running Autoplay queue.** If Music is already on an
  anonymous continuation, starting a playlist plays its first track and then the
  old Up Next reclaims playback at the next boundary — first track yours, second
  a stranger. `stop` before `play` tears the continuation down. Every queue start
  in the helpers does this.
- **A short queue drains straight into Autoplay.** Artist and genre picks set
  `song repeat to all` so the queue loops instead. Album context is not enough on
  its own: it can only pull tracks already in the library, so an artist owned as
  singles off EPs still yields a 4-track queue.
- **Music can wedge against Apple Events entirely** (`-1712` on even `player state`, indefinitely). Only restarting Music clears it. Worse, a timed-out `osascript` is killed while Music keeps executing the event it already accepted — so a half-built queue plays on.

## Troubleshooting

- **Device healthy but silent, inputs at the noise floor (~−86 dB)?** A USB DAC feeding your analog inputs can wedge invisibly to the OS — streaming happily, nothing coming out. Toggle the DSP source away and back, and bounce the Mac's default output device to rebuild the USB stream. That's what **Fix Audio** does in one tap.
- **Meters read the noise floor with nothing playing.** That's silence, not a fault. The numeric readout shows `−∞` rather than a number pinned to the bar floor.
- **Mic button says it needs a secure context.** You're on the HTTP origin — open the `https://` URL instead.

## Related

- [minidsp-dash](https://github.com/av-dsp-tools/minidsp-dash) — the general-purpose DSP-only sibling, no Apple Music or Nexia
- [minidsp-rs](https://github.com/mrene/minidsp-rs) — the CLI/daemon this sits on
- [biamp-ntp](https://github.com/av-dsp-tools/biamp-ntp) — the Nexia/Audia transport, this app's own dependency

## License

MIT.
