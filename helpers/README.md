# Helper scripts

The dashboard shells out to these; they live on the machine at **`/usr/local/bin/`**
and this directory is the tracked copy. They were unversioned until 2026-08-22,
which meant the app's actual playback behaviour wasn't reviewable from the repo.

Deploy after editing here:

    cp helpers/dsp-* /usr/local/bin/ && chmod 755 /usr/local/bin/dsp-*

`server.py` calls them via `BINDIR = "/usr/local/bin"`, so the copies in this
directory are inert until deployed. Editing `server.py` also needs a restart:

    launchctl kickstart -k gui/$(id -u)/com.hifi.dashboard
