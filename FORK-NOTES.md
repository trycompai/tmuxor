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

**7. launchd gives agents a minimal PATH.** Found by installing it: the backend
started, authenticated correctly, and then failed every pane call with
`[Errno 2] No such file or directory: 'tmux'` — `/opt/homebrew/bin` and
`~/.local/bin` are simply not on an agent's PATH. `run-backend.sh` now rebuilds
one. This is invisible in a dry run and invisible from a shell, because both
have a normal PATH; only the installed service sees it.

**8. It bound the wrong tmux session.** `CONDUCTOR_TMUX_SESSION` defaults to
`"0"` upstream. This machine has three tmux sessions and the agent floor lives
in one named `ai-composer`, so tmuxor attached to an unrelated session and
created panes nobody was watching — which is exactly the failure mode this whole
evening has been about. The installer now prefers `ai-composer` when it exists,
falls back to `"0"`, and honours `TMUXOR_TMUX_SESSION`.

**9. Dry-run no longer has side effects.** It was writing a real plist into
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

## Installed, 2026-08-26

It is running. `ai.comp.tmuxor` is loaded under launchd, the backend listens on
**`127.0.0.1:8790` only**, `/api/health` and `/api/panes` return 200 with a
token and 401 without, and it can see all 9 panes.

**One consequence to know:** `tailscale serve` routes a single root path, so
pointing it at 8790 **replaced** the earlier mapping to even-terminal's 3456.
The tailnet HTTPS URL now reaches tmuxor. even-terminal is still listening on
`*:3456` and still reachable directly at
`http://comp-mac-mini.tail36f46a.ts.net:3456`, which is the path that was
actually being used.

## Not done
- Upstream discovers tmux panes directly rather than through `ai-composer`'s
  session model. Binding the right tmux *session* (change 8) fixes where panes
  land, but a pane created this way is still **invisible to the control plane**:
  no session record, no Slack thread, no close proposal. `CONDUCTOR_LAUNCH_CMD`
  defaults to `claude`; pointing it at `ai-composer control new` instead is the
  change that would make created sessions first-class. That is the next real
  piece of work.
- The glasses app is the published Even Hub build; the `glasses/` source here is
  unbuilt and untouched.
- Port **8790**, so it does not collide with even-terminal's 3456.
