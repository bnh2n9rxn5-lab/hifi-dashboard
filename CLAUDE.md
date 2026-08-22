# MB Hi-Fi — hifi-dashboard

The **"MB Hi-Fi"** app: a phone-friendly web remote for the listening room. One
dependency-free Python file (`server.py`, stdlib only) serving a dark PWA that
drives three things — **Apple Music** (transport, Now Playing, artwork, favourite,
search, playlist/artist/genre pickers) via AppleScript, the **miniDSP** via the
`minidsp` CLI, and the **Biamp Nexia** sub bus via `nexia.py`. Sibling project
`~/minidsp-dash/minidsp-dash` is the public, DSP-only cousin; this one stays
unpublished (`origin` is a private backup remote — push after commits).

Also listens on **TLS at 8766** (`DSP_WEB_TLS_PORT`). That exists for one reason: the
SPL meter needs the phone mic, `getUserMedia` needs a secure context, and iOS blocks
it over plain http. Plain http stays up unchanged so bookmarks and QR codes keep
working, and missing certs are non-fatal — everything but the SPL meter works without
them. `./make-cert.sh` issues a root CA plus the leaf it signs (SAN entries, SHA-256,
serverAuth EKU, <=398 days — all four required by iOS 13+). Two certs, not one, so a
DHCP change needs only a new leaf and no re-install on the phone. `GET /ca.crt` serves
the root so the phone installs it by visiting a URL; `certs/` is gitignored.

Runs as LaunchAgent `com.hifi.dashboard` on port 8765, token in the plist at
`~/Library/LaunchAgents/com.hifi.dashboard.plist`. Log: `~/Library/Logs/hifi-dashboard.log`
(client-disconnect tracebacks in there are normal — phones closing sockets).

## Two rules that will waste your afternoon if you skip them

**1. Restart after every `server.py` edit.** There is no watch-path and no
auto-reload; the process runs whatever it loaded at launch.

    launchctl kickstart -k gui/$(id -u)/com.hifi.dashboard

`kickstart` re-runs the process but does **not** re-read the plist. Changing
`EnvironmentVariables` (e.g. `NEXIA_HOST`) needs a full reload:

    launchctl bootout gui/$(id -u)/com.hifi.dashboard
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hifi.dashboard.plist

On 2026-08-22 the live process was six weeks stale — older than the commit fixing
the very bug being reported — and presented as Apple Music misbehaving. When a
symptom contradicts the source, check `ps -Ao pid,lstart | grep server.py`
against the file mtime and `git log` **before** debugging the code.

**2. Bump `APP_VERSION` on any served-page change.** Phones cache the page hard;
the client self-reloads only when its stamp differs from `/api/status`. A page
change without a bump means phones keep the old UI forever.

## Required environment

`NEXIA_HOST` must point at the Biamp Nexia (there is no default — `nexia.py` raises
at import if it is unset, and `server.py` imports it, so the whole app refuses to
start). It lives in the LaunchAgent plist alongside `DSP_WEB_TOKEN`. The address
was hardcoded until 2026-08-22; it came out because the repo is going public and an
install-specific address is a fact about one house, not about the code.

## Helpers

`server.py` shells out to `/usr/local/bin/dsp-*` (`BINDIR`). Tracked copies live
in `helpers/` — they are inert until deployed:

    cp helpers/dsp-* /usr/local/bin/ && chmod 755 /usr/local/bin/dsp-*

## Apple Music AppleScript, learned the hard way

- **Favourite** is `favorited of current track`. The old `loved` property errors.
- **Artwork** can't be returned by osascript; the script writes `raw data of
  artwork 1` to a temp file and `/api/art` serves it.
- **Never queue an anonymous track list** (`play (every track whose ...)`) — there
  is no `current playlist`, so `back track` can only ever restart the current
  song. Build a real "HiFi Queue" user playlist instead. `dsp-faves` is still on
  the old pattern and still has the broken prev button.
- **`duplicate` needs a SPECIFIER**, not an evaluated list: `duplicate (every
  track ... whose album is x) to user playlist "HiFi Queue"` works; a list
  variable fails with -10006. Persistent IDs *do* survive duplication.
- **Don't play a playlist you just built.** Music takes the current-playlist
  change but ignores the track reference and opens at track 1. Settle first, then
  confirm you landed and re-issue. Cost a whole debugging round on 2026-08-22.
- **`whose name is (name of t)` errors -1728** — the inner property isn't
  evaluated inside the filter. Assign it to a variable first.
- **Music can wedge against Apple Events entirely** (-1712 on even `player state`,
  every call, indefinitely). Only a Music restart clears it. `run()` kills
  osascript on timeout but Music keeps executing the event it already accepted,
  so a timed-out queue build carries on and plays half-built.
- **UI-scripting Music's menus is unavailable** — osascript has no assistive
  access and granting Accessibility is the user's call.

## SPL meter

Weighting is A/C/Z as biquad sections, normalised to 0 dB at 1 kHz by measuring the
chain's own response there rather than hardcoding the book constants. Time weighting
is an exponential average of **power** (IEC fast 125 ms / slow 1 s) — averaging dB
skews every reading low. Verified against the published curves at 48 kHz: within
0.11 dB from 31.5 Hz to 2 kHz, +0.65 at 8 kHz, -3.1 at 16 kHz (bilinear warping near
Nyquist, immaterial on a broadband reading).

Two traps it handles rather than hides: a mic reports **dBFS, not SPL**, so it reads
"uncalibrated - relative only" until calibrated against a reference; and **iOS may
ignore the AGC/NS/AEC constraints**, so the track settings are re-read after the grant
and a warning shown if the OS is still processing the signal. Expect soft numbers at
the extremes regardless — iPhone mics roll off bass and limit around 100-105 dB SPL,
so dBC is the more informative reading with subs running.

## Library shape (2026-08-22)

7505 tracks, 1566 artists, and a very long tail: **64.7% of artists own exactly
one track, 79.8% own two or fewer.** This is why artist picks need album context
(`MINQ`/`MAXALB` in `dsp-play-artist`) — an artist-pure queue drains in a song
and Autoplay takes over. Assume any per-artist feature hits this distribution.
