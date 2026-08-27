# Fork notes — Comp AI

Branch `compai/macos-support`. Upstream: `liyiyuian/tmuxor` (MIT, 17 commits).
**Nothing is pushed.** These are the changes needed to run it on Daniel's Mac
mini alongside the existing floor.

## Why fork rather than depend

The valuable part is already done and is tedious to redo: a **published Even Hub
app**, and a backend that binds loopback only, requires a token on every request,
compares it in constant time, and **refuses to start without one or to bind
`0.0.0.0` at all**. That posture is better than `@evenrealities/even-terminal`,
which binds `0.0.0.0` with no host flag and passes its token in a URL query
string — where its own request log then records it.

At 17 commits and 2 stars it is a starting point, not a dependency.

## What was changed, and why

**1. `sources.py` was never installed.** `conductor_api.py` imports it, but the
installer only fetched `conductor_api.py` and `tmux_conductor.py`. A fresh
install crashes on import. Genuine upstream bug — worth reporting.

**2. Service management assumed Linux.** The installer wrote a `systemd --user`
unit unconditionally, with no OS check. On macOS that leaves the backend
installed and completely unsupervised, with no error. Now detects `uname -s` and
writes a **launchd plist** (`ai.comp.tmuxor`) on Darwin, the systemd unit on
Linux, and warns rather than pretending on anything else.

**3. Both platforms now go through one launcher.** launchd has no
`EnvironmentFile` equivalent, so `run-backend.sh` sources the env file and execs
the backend. The token stays in **one** file with one set of permissions instead
of being duplicated into a plist.

**4. `python3` is not necessarily new enough.** macOS ships 3.9 via Command Line
Tools; the backend needs 3.10+. Upstream checked `python3` only and died. Now
probes `python3.14 … python3.10, python3` for the first that satisfies the
requirement, honours `TMUXOR_PYTHON`, and bakes the resolved path into the
launcher. Resolves to `~/.local/bin/python3.13` here.

**5. The Tailscale CLI hardcodes `/var/run/tailscaled.socket`** and ignores
`TS_SOCKET`, so a userspace daemon on a private socket is invisible to it — the
same wall that made `even-terminal --tailscale` fail. Added a `ts()` helper
honouring `TMUXOR_TS_SOCKET`; unset, it behaves exactly as upstream. Also skips
`sudo tailscale set --operator` when using a private socket, since that only
applies to a root daemon.

**6. The token is no longer printed.** Upstream prints the token *and* renders
the config blob as a QR in the terminal. On this machine any pane's scrollback is
readable by every agent on the floor, so that publishes the secret to everything
on the box. The blob and QR now go to `~/.config/tmuxor/` at `0600`, and the
installer prints **paths**, not secrets.

**7. Dry-run no longer has side effects.** It was writing a real plist into
`~/Library/LaunchAgents` even under `TMUXOR_DRYRUN=1`. Dry-run output is parked
next to the install instead.

## Verified

- `bash -n install.sh` clean.
- Full dry run on macOS: detects python 3.13, writes a launchd plist, resolves
  the tailnet name through our userspace socket, writes `0600` outputs.
- `plutil -lint` accepts the generated plist.
- `conductor_api`, `sources`, `tmux_conductor` all import on 3.13.
- Config blob decodes to the right base URL and token.
- `~/Library/LaunchAgents` untouched by dry runs.

## Not done

- **Not installed and not run.** No launchd agent loaded, no `tailscale serve`
  configured for 8790, nothing listening.
- Upstream discovers tmux panes directly rather than through `ai-composer`'s
  session model. That boundary is where this evening's failure happened, and it
  is the change worth making next.
- The glasses app is the published Even Hub build; the `glasses/` source here is
  unbuilt and untouched.
- Port **8790**, so it does not collide with even-terminal's 3456.
