#!/usr/bin/env python3
"""Session sources — the pluggable backend that hosts the Claude sessions the
glasses drive. Two implementations behind ONE small interface:

  • TmuxSource  — the original: shells to `tmux` (delegates to tmux_conductor).
  • HerdrSource — shells to the `herdr` CLI (herdr.dev), an agent-aware tmux
                  replacement that reports agent state (idle/working/blocked/done)
                  natively instead of us inferring it from title glyphs.

conductor_api.py resolves a source PER REQUEST (default tmux) and calls it for
every multiplexer-touching operation. The Claude transcript layer
(resolve_session / transcript_candidates / read_conversation in tmux_conductor)
is multiplexer-agnostic — it only needs a pane's pid + cwd — so BOTH sources
reuse it unchanged.

Design rule: the tmux path must behave EXACTLY as before. TmuxSource is a thin
delegator to the unchanged tmux_conductor functions, and an unconfigured backend
(no ?source=, no CONDUCTOR_SOURCE) always resolves to tmux.
"""
import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict

import tmux_conductor as tc

# tmux topology config (moved here with the plumbing that uses it — same env vars).
TMUX_SESSION = os.environ.get("CONDUCTOR_TMUX_SESSION", "0")
LAUNCH_CMD = os.environ.get("CONDUCTOR_LAUNCH_CMD", "claude")

# default source when the request doesn't pick one — tmux keeps today's behavior.
DEFAULT_SOURCE = os.environ.get("CONDUCTOR_SOURCE", "tmux")


# =========================================================================
# tmux
# =========================================================================

def _tmux_list_windows():
    """All windows (project tags) in the conductor's tmux session."""
    out = tc._run(["list-windows", "-t", TMUX_SESSION,
                   "-F", "#{window_index}\t#{window_name}\t#{window_panes}"])
    wins = []
    for line in out.stdout.splitlines():
        p = line.split("\t")
        if len(p) >= 3 and p[0].isdigit() and p[2].isdigit():  # skip malformed rows
            wins.append({"index": int(p[0]), "name": p[1], "panes": int(p[2])})
    return wins


def _tmux_session_exists():
    return tc._run(["has-session", "-t", TMUX_SESSION]).returncode == 0


def _tmux_create_session(folder, tag=None):
    """Open a Claude session in an EXISTING `folder` under a project `tag` (tmux
    window): reuse the window named `tag` if present, else a new window; on a
    fresh machine create the session itself. Returns (pane_id, how). The folder
    is created/validated by the caller BEFORE this runs (base confinement)."""
    have = _tmux_session_exists()
    idx = None
    if tag and have:
        for w in _tmux_list_windows():
            if w["name"] == tag:
                idx = w["index"]
                break
    out, how = None, ""
    if idx is not None:
        out = tc._run(["split-window", "-t", f"{TMUX_SESSION}:{idx}", "-c", folder, "-P", "-F", "#{pane_id}"])
        how = f"pane in '{tag}'"
    if out is None or out.returncode != 0:  # no existing tag, that window is full, or no server yet
        name = (tag or os.path.basename(folder.rstrip("/")) or "claude")[:20]
        if not have:
            # fresh machine: no tmux session -> create it with this as the first window
            out = tc._run(["new-session", "-d", "-s", TMUX_SESSION, "-n", name, "-c", folder, "-P", "-F", "#{pane_id}"])
            how = f"new session '{name}'"
        else:
            # NOTE: target "0:" (trailing colon = the SESSION) so tmux appends at the next
            # free index. Bare "0" is read as window-index 0 -> "index 0 in use" / wrong slot.
            out = tc._run(["new-window", "-t", f"{TMUX_SESSION}:", "-c", folder, "-n", name, "-P", "-F", "#{pane_id}"])
            how = f"new window '{name}'"
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip() or "could not create pane or window")
    pane = out.stdout.strip()
    # start the session (CONDUCTOR_LAUNCH_CMD, default `claude`; the new pane's
    # interactive shell resolves shell functions/aliases like a custom profile launcher).
    tc._run(["send-keys", "-t", pane, LAUNCH_CMD, "Enter"])
    return pane, how


class TmuxSource:
    """The original backend. Every method delegates to the unchanged
    tmux_conductor functions, so the tmux path is byte-identical to before."""
    name = "tmux"
    native_status = False  # status is inferred (glyph + on-screen prompt regex)

    def list_panes(self, claude_only=False):
        return tc.list_panes(claude_only=claude_only)

    def capture_pane(self, target, lines=200):
        return tc.capture_pane(target, lines)

    def send_text(self, target, text, submit=True):
        return tc.send_text(target, text, submit=submit)

    def send_keys(self, target, keys):
        return tc.send_keys(target, keys)

    def session_status(self, p):
        return tc.session_status(p)

    def session_label(self, p):
        return tc.session_label(p)

    def window_tag(self, p):
        return tc.window_tag(p)

    def pane_n(self, p):
        # URL token for a pane: tmux '%29' -> '29' (matches today's numeric routes).
        return p["pane_id"].lstrip("%")

    def resolve_session(self, p):
        return tc.resolve_session(p)

    def transcript_candidates(self, p):
        # The cwd heuristic, which cannot tell two agents apart when they share a
        # directory. It is only reached when `resolve_session` fails; that one
        # walks the process tree and gives the exact binding, which is what this
        # fork used to ask ai-composer for.
        return tc.transcript_candidates(p["path"], p.get("pid"))

    def list_windows(self):
        return _tmux_list_windows()

    def create_session(self, folder, tag=None):
        return _tmux_create_session(folder, tag)


# =========================================================================
# herdr  (herdr.dev — CLI over its Unix-socket server)
# =========================================================================

# glasses/tmux key name -> herdr key name (herdr 0.7.1, verified accepted live).
_HERDR_KEYMAP = {
    "Enter": "enter", "Escape": "escape", "Up": "up", "Down": "down",
    "Left": "left", "Right": "right", "BTab": "shift+tab", "Tab": "tab",
    "Space": "space", "C-c": "ctrl+c", "C-C": "ctrl+c",
}
# herdr agent_status -> our API status vocab. 'blocked' = needs input (better than
# our regex heuristic); 'done' -> idle so the client done-band (working->idle) still fires.
_HERDR_STATUS = {"working": "working", "idle": "idle", "blocked": "waiting",
                 "done": "idle", "unknown": "idle"}


# herdr panes carry no Claude task title (unlike a tmux pane_title), so the fleet fell back to
# the cwd folder name (e.g. every session in one project showed the folder name). The real
# session name lives in Claude's runtime record sessions/<pid>.json {sessionId, name, pid}.
# A pane resolves to it two ways: (1) cheaply via agent_session.value (its sessionId), or
# (2) via `pane process-info` -> the claude foreground pid -> the pid keyed record. Both maps
# are built from those small files, cached; the per-pane pid lookup is itself cached.
_sname_cache = {"t": 0.0, "sid": {}, "pid": {}}
_SNAME_TTL = 20.0
_hlabel_cache = {}   # herdr pane_id -> (name, ts): caches the process-info resolution
_HLABEL_TTL = 120.0


def _session_names():
    now = time.time()
    if (_sname_cache["sid"] or _sname_cache["pid"]) and now - _sname_cache["t"] < _SNAME_TTL:
        return _sname_cache
    sid, pid = {}, {}
    for d in tc.SESSION_DIRS:
        try:
            for f in d.glob("*.json"):
                try:
                    r = json.loads(f.read_text())
                except Exception:
                    continue
                name = r.get("name")
                if not name:
                    continue
                if r.get("sessionId"):
                    sid[r["sessionId"]] = name
                if r.get("pid") is not None:
                    pid[str(r["pid"])] = name
        except OSError:
            pass
    _sname_cache.update(sid=sid, pid=pid, t=now)
    return _sname_cache


def _resolve_herdr_bin():
    """Locate the herdr binary. The systemd --user service PATH usually omits
    ~/.local/bin (where herdr installs), so PATH lookup alone finds nothing in the
    service context. Order: explicit override -> PATH -> the standard install dir."""
    env = os.environ.get("CONDUCTOR_HERDR_BIN")
    if env:
        return env
    found = shutil.which("herdr")
    if found:
        return found
    cand = os.path.expanduser("~/.local/bin/herdr")
    return cand if os.path.exists(cand) else None


HERDR_BIN = _resolve_herdr_bin()


class HerdrSource:
    """Backend for herdr. Shells to the `herdr` CLI (same stdlib-subprocess model
    as tmux). Reuses tmux_conductor's Claude-transcript layer via the pane's
    shell pid, and maps herdr's native agent_status onto our status vocab."""
    name = "herdr"
    native_status = True  # herdr reports agent state directly; no regex inference

    # --- CLI plumbing ---
    # herdr's CLI is mixed: metadata commands (list/get/create/close) print a JSON
    # envelope on stdout; `pane read` prints RAW pane text; send/run print nothing.
    # Failures set a non-zero exit and put a JSON error on stderr (a few also echo an
    # {"error":...} envelope on stdout). So: always check returncode first (this is
    # what stops a failed send-keys from being read as success), then parse per-shape.
    def _run(self, args, timeout=15):
        if not HERDR_BIN:
            raise RuntimeError("herdr binary not found (set CONDUCTOR_HERDR_BIN)")
        r = subprocess.run([HERDR_BIN, *args], capture_output=True, timeout=timeout)
        out = r.stdout.decode(errors="replace")
        err = r.stderr.decode(errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"herdr {' '.join(args[:2])}: {(err or out).strip()[:200]}")
        return out, err

    def _json(self, args, timeout=15):
        out, err = self._run(args, timeout)
        try:
            d = json.loads(out or "{}")   # send/run print nothing -> {} (success, rc already checked)
        except Exception:
            raise RuntimeError(f"herdr {' '.join(args[:2])}: {(out or err).strip()[:200]}")
        if isinstance(d, dict) and "error" in d and "result" not in d:
            e = d["error"]
            raise RuntimeError(f"herdr {' '.join(args[:2])}: {e.get('message', e) if isinstance(e, dict) else e}")
        return d.get("result", d) if isinstance(d, dict) else d

    def _text(self, args, timeout=15):
        out, _ = self._run(args, timeout)   # `pane read` returns the pane content as plain text
        return out

    def _map_key(self, k):
        if k in _HERDR_KEYMAP:
            return _HERDR_KEYMAP[k]
        if re.fullmatch(r"\d", k):
            return k                 # digit hotkey passes through
        return k.lower()             # best-effort for anything else

    def _shell_pid(self, pane_id):
        try:
            info = self._json(["pane", "process-info", "--pane", pane_id]).get("process_info", {})
            return info.get("shell_pid")
        except Exception:
            return None

    def _ensure_pid(self, p):
        if p.get("pid") is None:  # filled lazily — one process-info call, only on the transcript path
            p["pid"] = self._shell_pid(p["pane_id"])
        return p

    # --- fleet ---
    def list_panes(self, claude_only=False):
        raw = self._json(["pane", "list"]).get("panes", [])
        wsmap = {}
        try:
            for w in self._json(["workspace", "list"]).get("workspaces", []):
                wsmap[w["workspace_id"]] = w
        except Exception:
            pass
        out = []
        for rp in raw:
            ws = rp.get("workspace_id", "")
            wmeta = wsmap.get(ws, {})
            is_claude = rp.get("agent") == "claude"
            out.append({
                "pane_id": rp["pane_id"],
                "session": ws,
                "window_index": wmeta.get("number", 0),      # workspace 'number' is a clean 1-based int
                "window_name": wmeta.get("label", "") or ws,
                "pane_index": 0,                              # assigned below
                "pane_active": bool(rp.get("focused")),
                "window_active": bool(wmeta.get("focused")),
                "command": rp.get("agent") or "",
                "pid": None,                                  # lazy (process-info) — see _ensure_pid
                "path": rp.get("cwd", "") or rp.get("foreground_cwd", ""),
                "title": "",                                  # herdr pane list carries no task title
                "is_conductor": False,                        # the API runs as a service, not inside a pane
                "is_claude": is_claude,
                "_agent_status": rp.get("agent_status", "unknown"),
                "_sid": (rp.get("agent_session") or {}).get("value"),  # Claude sessionId -> real name
            })
        # stable pane_index within each window (workspace) for a deterministic sort tiebreak
        counter = defaultdict(int)
        for p in sorted(out, key=lambda p: (p["window_index"], p["pane_id"])):
            p["pane_index"] = counter[p["window_index"]]
            counter[p["window_index"]] += 1
        if claude_only:
            out = [p for p in out if p["is_claude"]]
        return out

    def capture_pane(self, target, lines=200):
        # `pane read` prints raw pane text (rc!=0 on a missing pane -> _text raises).
        # Floor at 100: the SSE/menu path asks for 40, but a long permission diff needs the
        # scrollback ABOVE the live prompt to be readable in the glasses READ view (tmux gets
        # this from its own -S scrollback; herdr returns exactly --lines).
        return self._text(["pane", "read", target, "--source", "recent-unwrapped",
                           "--lines", str(max(int(lines), 100))])

    def send_text(self, target, text, submit=True):
        if "\n" in text:
            # herdr's send-text emits an embedded newline as Enter (it submits early), so a
            # multi-line prompt would fragment into several submissions. Type each line and put
            # a soft newline (shift+enter, Claude's composer newline) BETWEEN them so the whole
            # thing lands as ONE prompt — this is the tmux bracketed-paste path's intent.
            # NOTE: shift+enter as the composer soft-newline is doc-grounded but UNVERIFIED on a
            # live herdr Claude pane; if a line still submits early, tune this key on-device.
            lines = text.split("\n")
            for i, ln in enumerate(lines):
                if ln:
                    self._json(["pane", "send-text", target, ln])
                if i < len(lines) - 1:
                    self._json(["pane", "send-keys", target, "shift+enter"])
        else:
            self._json(["pane", "send-text", target, text])
        if submit:
            time.sleep(0.15)  # let the composer settle before Enter (mirrors tmux send_text)
            self._json(["pane", "send-keys", target, "enter"])
        tc._audit("send_text", target, {"submit": submit, "text": text, "source": "herdr"})
        return {"pane_id": target, "submitted": submit, "chars": len(text)}

    def send_keys(self, target, keys):
        keylist = keys if isinstance(keys, list) else [keys]
        self._json(["pane", "send-keys", target, *[self._map_key(k) for k in keylist]])
        tc._audit("send_keys", target, {"keys": keylist, "source": "herdr"})
        return {"pane_id": target, "keys": keylist}

    def session_status(self, p):
        if not p.get("is_claude"):
            return "other"
        return _HERDR_STATUS.get(p.get("_agent_status", "unknown"), "idle")

    def session_label(self, p):
        # Prefer the real Claude session name (its /rename or auto title); fall back to the
        # project folder name when the pane has no resolvable session.
        if p.get("is_claude"):
            nm = self._claude_name(p)
            if nm:
                return nm
        base = os.path.basename((p.get("path") or "").rstrip("/"))
        return base or p.get("command") or "session"

    def _claude_name(self, p):
        names = _session_names()
        sid = p.get("_sid")
        if sid and sid in names["sid"]:
            return names["sid"][sid]                     # cheap: herdr gave us the sessionId
        pane_id = p["pane_id"]
        now = time.time()
        hit = _hlabel_cache.get(pane_id)
        if hit and now - hit[1] < _HLABEL_TTL:
            return hit[0]
        try:  # resolve via the pane's claude foreground pid -> the pid-keyed session name
            info = self._json(["pane", "process-info", "--pane", pane_id]).get("process_info", {})
            for c in info.get("foreground_processes", []):
                if c.get("name") == "claude":
                    nm = names["pid"].get(str(c.get("pid")))
                    if nm:
                        if len(_hlabel_cache) > 256:
                            _hlabel_cache.pop(next(iter(_hlabel_cache)))
                        _hlabel_cache[pane_id] = (nm, now)
                        return nm
        except Exception:
            pass
        return None

    def window_tag(self, p):
        return tc.window_tag(p)  # generic: reads window_name / window_index

    def pane_n(self, p):
        return p["pane_id"]  # e.g. 'w3:p6' — opaque token echoed back in URLs

    def resolve_session(self, p):
        self._ensure_pid(p)
        return tc.resolve_session(p)

    def transcript_candidates(self, p):
        self._ensure_pid(p)
        return tc.transcript_candidates(p["path"], p.get("pid"))

    def list_windows(self):
        out = []
        for w in self._json(["workspace", "list"]).get("workspaces", []):
            out.append({"index": w.get("number", 0),
                        "name": w.get("label", "") or w["workspace_id"],
                        "panes": w.get("pane_count", 0)})
        return out

    def create_session(self, folder, tag=None):
        """New Claude session in `folder` under a project `tag`: reuse the herdr workspace
        whose label == tag by splitting a pane into it (mirrors tmux window reuse); else a
        new workspace. (herdr reaps an idle empty pane, so the launcher starts immediately.)"""
        pane, how = None, ""
        if tag:
            try:
                ws = next((w for w in self._json(["workspace", "list"]).get("workspaces", [])
                           if (w.get("label") or "") == tag), None)
                if ws:
                    anchor = self._json(["pane", "list", "--workspace", ws["workspace_id"]]).get("panes", [])
                    if anchor:
                        res = self._json(["pane", "split", anchor[-1]["pane_id"],
                                          "--direction", "down", "--cwd", folder, "--focus"])
                        pane = (res.get("pane") or {}).get("pane_id")
                        how = f"pane in '{tag}'"
            except Exception:
                pane = None  # workspace full / split refused -> fall through to a new workspace
        if not pane:
            label = (tag or os.path.basename(folder.rstrip("/")) or "claude")[:20]
            res = self._json(["workspace", "create", "--cwd", folder, "--label", label, "--focus"])
            pane = (res.get("root_pane") or {}).get("pane_id")
            how = f"herdr workspace '{label}'"
        if not pane:
            raise RuntimeError("herdr returned no pane for the new session")
        self._json(["pane", "run", pane, LAUNCH_CMD])  # types the launcher + Enter
        return pane, how


# =========================================================================
# registry / availability / selection
# =========================================================================

_SOURCES = {"tmux": TmuxSource, "herdr": HerdrSource}
_avail_cache = {"t": 0.0, "v": None}
_AVAIL_TTL = 30.0  # cache availability so the fleet poll never spawns a probe subprocess


def _herdr_ok():
    if not HERDR_BIN or not os.path.exists(HERDR_BIN):
        return False
    try:  # binary present AND its server reachable (socket up) -> herdr can actually serve
        r = subprocess.run([HERDR_BIN, "workspace", "list"], capture_output=True, text=True, timeout=4)
        return r.returncode == 0 and '"workspaces"' in r.stdout
    except Exception:
        return False


def available_sources():
    """Which sources this machine can actually serve (cached). The phone gates its
    picker on this, so it can't offer a backend that isn't installed/running."""
    now = time.time()
    if _avail_cache["v"] is not None and now - _avail_cache["t"] < _AVAIL_TTL:
        return _avail_cache["v"]
    names = []
    if shutil.which("tmux"):
        names.append("tmux")
    if _herdr_ok():
        names.append("herdr")
    if not names:
        names = ["tmux"]  # tmux can create its own server on a fresh box
    _avail_cache["v"], _avail_cache["t"] = names, now
    return names


def _default_present():
    """Is the default source's binary on this box? Stat-level (no subprocess)."""
    return bool(HERDR_BIN) if DEFAULT_SOURCE == "herdr" else bool(shutil.which("tmux"))


def resolve_source_name(requested):
    """Pick the source for a request. No selection — or an explicit pick of the configured
    default — resolves to DEFAULT_SOURCE with no subprocess probing (binary presence is a
    stat-level check), so the hot fleet poll stays as cheap as before. If the default's
    binary is MISSING (e.g. a herdr-only box left on the tmux default), or a NON-default
    source is requested, fall through to full availability resolution (cached 30s) instead
    of letting every request fail against a multiplexer that isn't there."""
    req = (requested or "").strip().lower()
    if (not req or req == DEFAULT_SOURCE) and _default_present():
        return DEFAULT_SOURCE
    if req not in _SOURCES:
        req = DEFAULT_SOURCE
    avail = available_sources()
    if req in avail:
        return req
    return DEFAULT_SOURCE if DEFAULT_SOURCE in avail else (avail[0] if avail else DEFAULT_SOURCE)


def get_source(name):
    return _SOURCES.get(name, TmuxSource)()
