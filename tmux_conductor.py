#!/usr/bin/env python3
"""tmux conductor — control a tmux session's windows/panes and the Claude Code
sessions living inside them, exposed as MCP tools so a "conductor" Claude can
survey the fleet, switch focus, read any pane, and type into it.

Verified send pattern (de-risk experiment, Claude Code v2.1.190):
    tmux send-keys -t <pane> -l -- "<text>"   # literal text into the TUI composer
    tmux send-keys -t <pane> Enter            # submits
Multi-line text uses bracketed paste via a *named* buffer (set-buffer -b /
paste-buffer -b -p -d) so embedded newlines don't submit early and the user's
own tmux paste buffer is never clobbered.

Usage:
    python tmux_conductor.py            # run as MCP server (stdio)
    python tmux_conductor.py selftest   # read-only checks against the live tmux

Safety: never sends to the conductor's own pane ($TMUX_PANE); every send is
appended to an audit log (~/.tmux-conductor-audit.log, override TMUX_CONDUCTOR_AUDIT).
"""
import json
import os
import re
import subprocess
import sys
import calendar
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

TMUX = "tmux"
SELF_PANE = os.environ.get("TMUX_PANE", "")  # conductor's own pane id; never send here
PASTE_BUF = "tmuxcond"  # dedicated buffer name so we don't touch the user's clipboard
AUDIT = Path(os.environ.get("TMUX_CONDUCTOR_AUDIT", str(Path.home() / ".tmux-conductor-audit.log")))
# The audit log records every keystroke/text sent into panes — keep it private (0600), matching the
# chmod-600 convention for the credentials env file. Tighten any pre-existing world-readable log here;
# new writes create it 0600 (see _audit).
try:
    if AUDIT.exists():
        os.chmod(AUDIT, 0o600)
except OSError:
    pass

# Claude Code stores per-project session transcripts under these roots. This
# machine uses profiles, so sessions live under several of them.
PROJECT_ROOTS = [
    Path.home() / ".claude" / "projects",
    *sorted((Path.home() / ".config" / "claude-code" / "profiles").glob("*/projects")),
]

# Tab-separated. `title` is last so split(maxsplit) lets it absorb stray tabs
# without breaking the earlier fixed fields. NOTE: tmux does NOT escape control
# bytes in window/pane names — a name with a literal tab/newline would shift the
# columns, so list_panes() guards its int() casts and skips malformed rows.
PANE_FMT = "\t".join([
    "#{pane_id}", "#{session_name}", "#{window_index}", "#{window_name}",
    "#{pane_index}", "#{pane_active}", "#{window_active}",
    "#{pane_current_command}", "#{pane_pid}", "#{pane_current_path}", "#{pane_title}",
])


# --- tmux plumbing ----------------------------------------------------------

def _run(args, input_text=None):
    return subprocess.run([TMUX, *args], input=input_text, capture_output=True, text=True)


def _check(args, input_text=None):
    r = _run(args, input_text)
    if r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def _audit(action, pane_id, detail):
    try:
        fd = os.open(str(AUDIT), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "action": action,
                                "pane": pane_id, **detail}) + "\n")
    except Exception:
        pass


# --- core operations --------------------------------------------------------

# Claude Code renames its process to its own version string, so `pane_current_command`
# reads "2.1.238", not "claude". Matching the literal name recognises no pane at all on a
# machine where Claude Code is actually running — the fleet list then filters everything
# out and the glasses show an empty floor. Matching a bare version is what works.
# CONDUCTOR_CLAUDE_COMMANDS can extend the set; see is_codex_pane for the other harness.
_PS_CACHE = {"at": 0.0, "map": {}}
_PS_TTL = 2.0  # a poll interval; the process tree does not move faster than this

_CLAUDE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_CLAUDE_EXTRA = {c.strip() for c in os.environ.get("CONDUCTOR_CLAUDE_COMMANDS", "").split(",") if c.strip()}


def _is_claude_command(cmd):
    return cmd == "claude" or bool(_CLAUDE_VERSION_RE.match(cmd or "")) or cmd in _CLAUDE_EXTRA


def list_panes(claude_only: bool = False):
    """All panes across the tmux server, as dicts. pane_id (e.g. '%29') is the
    stable target for every other operation. Returns [] when no tmux server is
    running yet (fresh machine) instead of erroring."""
    panes = []
    r = _run(["list-panes", "-a", "-F", PANE_FMT])
    if r.returncode != 0:  # "no server running" -> no sessions yet
        return panes
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        f = line.split("\t", 10)
        if len(f) < 11 or not (f[2].isdigit() and f[4].isdigit()):
            continue  # malformed/column-shifted row (e.g. a name with a stray tab) — skip
        p = {
            "pane_id": f[0], "session": f[1], "window_index": int(f[2]),
            "window_name": f[3], "pane_index": int(f[4]),
            "pane_active": f[5] == "1", "window_active": f[6] == "1",
            "command": f[7], "pid": int(f[8]) if f[8].isdigit() else None,
            "path": f[9], "title": f[10],
            "is_conductor": f[0] == SELF_PANE,
            "is_claude": _is_claude_command(f[7]),
        }
        # `is_claude` is what the glasses filter the fleet on, so it has to mean
        # "an agent lives here" rather than "Claude Code lives here" -- otherwise
        # a Codex agent is simply absent from the floor, which is how @doctor and
        # @harbour were invisible for a day. `harness` carries the finer answer
        # for anything that needs it.
        p["harness"] = "claude" if p["is_claude"] else None
        if not p["is_claude"] and p["pid"] and not p["is_conductor"] \
                and is_codex_pane(p["pid"]):
            p["is_claude"] = True
            p["harness"] = "codex"
        if claude_only and not p["is_claude"]:
            continue
        panes.append(p)
    # Order the fleet the way the operator arranged it.
    #
    # This sorted by `pane_id` until 2026-09-04, for a real reason: tmux reuses a
    # window index when a window closes, so closing one session silently moved an
    # unrelated one up the list and anyone selecting by position got a different
    # agent than they meant. pane_id never changes, so it could not mislead.
    #
    # It also could not *inform*. Daniel curates window order to say something --
    # `surveyor | billing | harbour` sit together because that is the order the
    # work moves through them -- and a fleet sorted by creation time throws that
    # away. On the glasses, where the list is the whole interface, position is
    # most of the meaning.
    #
    # The original hazard is handled where it actually lives: every operation in
    # this API targets a **pane id**, never an index, so a list that reorders can
    # no longer send a message to the wrong agent. What moves is what a person
    # reads, not what anything acts on. `pane_id` remains the last tiebreak so
    # two panes in one window keep a fixed order.
    def _order(p):
        return (p.get("window_index", 1 << 30), p.get("pane_index", 0),
                int(str(p.get("pane_id", "")).lstrip("%") or 1 << 30))
    panes.sort(key=_order)
    return panes


def capture_pane(target: str, lines: int = 200):
    """Rendered text currently on a pane plus up to `lines` of scrollback."""
    return _check(["capture-pane", "-p", "-J", "-t", target, "-S", f"-{int(lines)}"])


def select_target(window: Optional[str] = None, pane: Optional[str] = None):
    if window is not None:
        _check(["select-window", "-t", str(window)])
    if pane is not None:
        _check(["select-pane", "-t", str(pane)])
    return True


def _resolve_pane_id(target: str) -> str:
    return _check(["display-message", "-p", "-t", target, "#{pane_id}"]).strip()


def _assert_sendable(target: str) -> str:
    pane_id = _resolve_pane_id(target)
    if pane_id and pane_id == SELF_PANE:
        raise RuntimeError(f"refusing to send to the conductor's own pane ({pane_id})")
    return pane_id


def send_text(target: str, text: str, submit: bool = True):
    """Type free text into a pane's program (e.g. a prompt into a Claude session),
    then press Enter if submit. Single-line uses send-keys -l; multi-line uses
    bracketed paste so newlines don't submit early."""
    pane_id = _assert_sendable(target)
    if "\n" in text:
        _check(["set-buffer", "-b", PASTE_BUF, "--", text])
        _check(["paste-buffer", "-b", PASTE_BUF, "-d", "-p", "-t", target])
    else:
        _check(["send-keys", "-t", target, "-l", "--", text])
    if submit:
        time.sleep(0.15)  # let the composer settle before the Enter key event
        _check(["send-keys", "-t", target, "Enter"])
    _audit("send_text", pane_id, {"submit": submit, "text": text})
    return {"pane_id": pane_id, "submitted": submit, "chars": len(text)}


def send_keys(target: str, keys):
    """Send raw tmux key name(s) — 'Enter', 'Escape', 'C-c', 'Up' — for control
    keys / interrupts, not free text."""
    pane_id = _assert_sendable(target)
    keylist = keys if isinstance(keys, list) else [keys]
    _check(["send-keys", "-t", target, *keylist])
    _audit("send_keys", pane_id, {"keys": keylist})
    return {"pane_id": pane_id, "keys": keylist}


# --- transcript mapping (pane cwd -> Claude session JSONL) -------------------

def _config_dir_for_pid(pid):
    """Best-effort: read CLAUDE_CONFIG_DIR from the pane process env to pick the
    right profile root. Falls back to scanning all roots."""
    if not pid:
        return None
    try:
        for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            if kv.startswith(b"CLAUDE_CONFIG_DIR="):
                return kv.split(b"=", 1)[1].decode()
    except Exception:
        pass
    return None


def _encode_project_dir(cwd: str) -> str:
    """Claude Code's on-disk encoding of a project path into projects/<dir>: it replaces BOTH '/' and
    '.' with '-' (verified on disk: '/home/u/proj.v2' -> '-home-u-proj-v2', '/.config' -> '--config').
    Replacing only '/' broke any dotted cwd (git worktrees under .claude/..., version-dotted dirs) ->
    a non-existent dir -> an empty conversation. Keep this the single source of truth for the mapping."""
    return cwd.replace("/", "-").replace(".", "-")


def _safe_mtime(p) -> float:
    try:
        return p.stat().st_mtime
    except OSError:  # file rotated/deleted between glob and sort -> don't 500 the request
        return 0.0


# ---------------------------------------------------------------------------
# Resolving a transcript from the pane's cwd is one-to-many: several agents
# started in one directory share a project directory, and breaking the tie by
# newest mtime shows whichever agent spoke most recently rather than the one you
# selected. Daniel opened the orchestrator and read another agent's conversation.
#
# This fork used to ask ai-composer, which recorded the verified pane->transcript
# binding. ai-composer was removed on 2026-08-31. The replacement is better and
# has no dependency: `resolve_session` walks the pane's process tree to the
# agent's own pid and reads the runtime record it keeps there. That is the exact
# binding, from the operating system rather than from a third party.
# ---------------------------------------------------------------------------


def transcript_candidates(cwd: str, pid=None):
    """All session JSONL files whose project dir matches `cwd`, newest first.
    cwd->transcript is one-to-many when several sessions share a directory."""
    enc = _encode_project_dir(cwd)
    roots = list(PROJECT_ROOTS)
    cfg = _config_dir_for_pid(pid)
    if cfg:
        roots.insert(0, Path(cfg) / "projects")
    seen, out = set(), []
    for root in roots:
        d = root / enc
        if d.is_dir():
            for j in d.glob("*.jsonl"):
                if j in seen:
                    continue
                seen.add(j)
                out.append(j)
    out.sort(key=_safe_mtime, reverse=True)
    return out


def _flatten_content(content):
    if isinstance(content, str):
        return content.strip()
    parts = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "tool_use":
                parts.append(f"[tool_use {b.get('name', '')}]")
            elif bt == "tool_result":
                parts.append("[tool_result]")
    return "\n".join(x for x in parts if x).strip()


def read_transcript(jsonl_path, last: int = 20):
    turns = []
    for line in Path(jsonl_path).read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        text = _flatten_content(rec.get("message", {}).get("content", ""))
        if text:
            turns.append({"role": rec["type"], "ts": rec.get("timestamp"), "text": text})
    return turns[-last:]


def _assistant_prose(content):
    """Only the assistant's prose text blocks — no thinking, tool_use, or tool_result."""
    if isinstance(content, str):
        return content.strip()
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
    return "\n".join(x for x in parts if x).strip()


def _strip_prose(t):
    """Flatten the markdown CONSTRUCTS in a prose segment (never run on code — see _strip_markdown)."""
    # tables: drop the |---|---| separator rows, render "| a | b |" as "a · b"
    rows = []
    for ln in t.split("\n"):
        s = ln.strip()
        if "|" in s and "-" in s and re.fullmatch(r"[\s:|-]+", s):
            continue  # separator row
        if s.startswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            rows.append(" · ".join(c for c in cells if c))
        else:
            rows.append(ln)
    t = "\n".join(rows)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", r"■ \1", t)  # heading -> kept + marked (not erased)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = re.sub(r"\*(.+?)\*", r"\1", t, flags=re.S)
    t = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", t, flags=re.S)
    t = re.sub(r"`([^`]+)`", r"\1", t)            # inline code
    t = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", t)  # image -> alt
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)   # link -> text
    t = re.sub(r"(?m)^\s{0,3}>\s?", "» ", t)      # blockquote -> kept marker (not erased)
    t = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1• ", t)  # bullets -> •
    t = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "", t)  # horizontal rules
    return t


def _strip_markdown(t):
    """Flatten markdown to plain text — the glasses can't render markdown. Code fences are kept
    VERBATIM with a '│ ' left rail; the markdown transforms run ONLY on the prose between fences,
    so code is never mutated (no more __init__->init, **kwargs->kwargs, `a * b`->`a  b`, `- x`->`• x`)."""
    segs = re.split(r"(?m)^[ \t]*`{3,}[^\n]*$", t)  # split on the ``` fence lines (open + close)
    out = []
    for i, seg in enumerate(segs):
        if i % 2 == 1:  # text between an opening and closing fence = code: keep raw, add a rail
            out.append("\n".join(("│ " + ln) if ln.strip() else "│" for ln in seg.strip("\n").split("\n")))
        else:
            out.append(_strip_prose(seg))
    return "\n".join(out).strip()


def _user_prompt(rec):
    """The real typed prompt from a user record, or '' for tool-results / meta /
    system-reminders / slash-command plumbing."""
    if rec.get("isMeta"):
        return ""
    c = rec.get("message", {}).get("content", "")
    if isinstance(c, list):
        if not any(isinstance(b, dict) and b.get("type") == "text" for b in c):
            return ""  # tool_result-only
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        return ""
    c = re.sub(r"<system-reminder>.*?</system-reminder>", "", c, flags=re.S)
    # background-task completions are injected as user-role messages, not typed prompts —
    # drop the whole block so its <task-id>/<status>/… tags don't get flattened into soup
    c = re.sub(r"<task-notification>.*?</task-notification>", "", c, flags=re.S)
    c = re.sub(r"<command-[a-z]+>.*?</command-[a-z]+>", "", c, flags=re.S)
    c = re.sub(r"<local-command-[a-z]+>.*?</local-command-[a-z]+>", "", c, flags=re.S)
    c = re.sub(r"</?[a-z-]+>", "", c)  # stray tags
    return c.strip()


_convo_cache = {}  # jsonl_path -> ((mtime_ns, size), turns) — skip re-read+re-parse when unchanged


def read_conversation(jsonl_path):
    """The real back-and-forth, oldest first: [{role:'user'|'assistant', text}].
    User prompts (typed only) interleaved with assistant prose; the in-between
    (thinking / tool calls / results / system noise) stripped; markdown flattened.
    Memoized by (mtime_ns, size) so a steady 2.5s poll on an unchanged transcript
    costs one stat() instead of a full read + json.loads + ~15 regex passes/turn.

    A Codex rollout is a different format in the same shape of file, so it is
    dispatched here rather than at every call site."""
    if "rollout-" in Path(jsonl_path).name:
        return read_codex_conversation(jsonl_path)
    try:
        st = Path(jsonl_path).stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None:
        hit = _convo_cache.get(jsonl_path)
        if hit and hit[0] == key:
            return hit[1]
    turns = []
    for line in Path(jsonl_path).read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = rec.get("type")
        if t == "assistant":
            text = _assistant_prose(rec.get("message", {}).get("content", ""))
            if text:
                turns.append({"role": "assistant", "text": _strip_markdown(text)})
        elif t == "user":
            text = _user_prompt(rec)
            if text:
                turns.append({"role": "user", "text": _strip_markdown(text)})
    if key is not None:
        _convo_cache[jsonl_path] = (key, turns)
        if len(_convo_cache) > 64:  # bound memory: transcripts rotate over the service's lifetime
            _convo_cache.pop(next(iter(_convo_cache)))
    return turns


# --- exact pane -> live Claude session via sessions/<pid>.json --------------
# Claude Code writes a runtime record per live session keyed by its OWN process
# pid: sessions/<claude_pid>.json = {sessionId, cwd, status, name, procStart,...}.
# This is the only reliable map when many panes share a cwd (cwd->jsonl is 1:many).
SESSION_DIRS = [
    Path.home() / ".claude" / "sessions",
    *sorted((Path.home() / ".config" / "claude-code" / "profiles").glob("*/sessions")),
]


def _ps_children():
    """{ppid: [pid, ...]} from one `ps` call, for systems without /proc.

    macOS has no /proc at all, so the walk below found no descendants there and
    every pane looked like a bare shell: no Claude session record was ever
    reached through it, and a Codex pane could not be recognised at all. One
    `ps -A` is ~1ms and is cached for a poll interval.
    """
    now = time.time()
    if _PS_CACHE["at"] > now - _PS_TTL:
        return _PS_CACHE["map"]
    tree = defaultdict(list)
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid="],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                tree[parts[1]].append(parts[0])
    except Exception:
        pass
    _PS_CACHE.update(at=now, map=tree)
    return tree


def _proc_descendants(pid):
    seen, stack = [], [str(pid)]
    have_proc = Path("/proc").is_dir()
    tree = None if have_proc else _ps_children()
    while stack:
        cur = stack.pop()
        children = []
        if have_proc:
            try:
                for t in (Path("/proc") / cur / "task").iterdir():
                    children.extend((t / "children").read_text().split())
            except OSError:
                pass
        else:
            children = tree.get(cur, [])
        for ch in children:
            if ch not in seen:
                seen.append(ch)
                stack.append(ch)
    return seen


def _start_epoch(text, utc):
    """Parse a `ps`/Claude start stamp to epoch seconds, or None."""
    try:
        parsed = time.strptime(" ".join(str(text).split()), "%a %b %d %H:%M:%S %Y")
    except (ValueError, TypeError):
        return None
    return calendar.timegm(parsed) if utc else time.mktime(parsed)


def _same_start(pid, recorded):
    """Is `pid` still the process that wrote `recorded`? Guards pid reuse.

    A plain string comparison works on Linux, where both sides are tick counts.
    On macOS the two sides disagree in *format*: Claude Code records
    'Mon Aug 31 16:10:23 2026' in UTC, while `ps -o lstart=` prints the same
    instant in local time. Compared as text they never match, so every Claude
    pane failed to resolve and fell back to "newest transcript in this folder" --
    right answer, wrong reason, and wrong outright once two sessions share a
    directory. Compare instants.
    """
    token = _proc_start_ticks(pid)
    if token is None or recorded is None:
        return False
    if str(token) == str(recorded):
        return True
    mine, theirs = _start_epoch(token, utc=False), _start_epoch(recorded, utc=True)
    return mine is not None and theirs is not None and abs(mine - theirs) < 2


def _proc_start_ticks(pid):
    """A token that changes when a pid is recycled. Ticks on Linux, start time
    elsewhere -- the value is never interpreted, only compared."""
    if not Path("/proc").is_dir():
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                 capture_output=True, text=True)
            return out.stdout.strip() or None
        except Exception:
            return None
    try:
        after = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return after[19]  # field 22: starttime in clock ticks (guards pid reuse)
    except OSError:
        return None


_resolve_cache = {}  # pane_pid -> (claude_pid, procStart, sd_path, sessionId, cwd)


def _live_jsonl(projects_dir, root_sid):
    """Follow the compaction chain from root_sid to the newest live transcript.
    A long-running session that compacts writes a NEW jsonl (new sessionId) referencing
    its parent in the head, but the runtime record keeps the ORIGINAL id — so the exact
    root jsonl freezes at compaction time. Walk root -> child -> ... and return the
    most-recently-modified jsonl in root_sid's chain (its own file if it never compacted)."""
    if not projects_dir.is_dir():
        return None
    files = list(projects_dir.glob("*.jsonl"))
    chain, changed = {root_sid}, True
    while changed:  # transitively add files whose head references anything already in the chain
        changed = False
        for f in files:
            if f.stem in chain:
                continue
            try:
                head = f.open(errors="replace").read(16384)
            except OSError:
                continue
            if any(sid in head for sid in chain):
                chain.add(f.stem); changed = True
    best, best_m = None, -1.0
    for f in files:
        if f.stem in chain:
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best_m:
                best_m, best = m, f
    return str(best) if best else None


def _session_record(sd, claude_pid, sessionId, cwd):
    """Re-read the live sessions/<pid>.json (fresh status) + resolve the live transcript
    (following compaction from the record's sessionId)."""
    try:
        info = json.loads((sd / f"{claude_pid}.json").read_text())
    except Exception:
        return None
    projects_dir = sd.parent / "projects" / _encode_project_dir(cwd)
    return {**info, "jsonl": _live_jsonl(projects_dir, sessionId)}


def resolve_session(pane):
    """The exact live session for a pane: its shell pid's `claude` descendant has
    a sessions/<pid>.json record. procStart guards against pid recycling. Returns
    that record plus the resolved transcript path (jsonl), or None.
    Caches the stable pane->claude resolution so a steady poll skips the /proc
    descendant walk; the per-call sessions/<pid>.json re-read keeps status/jsonl fresh.

    A Codex pane is answered first and separately. Codex keeps no per-pid runtime
    record, so there is nothing to look up by pid; what it does keep is a rollout
    stamped with the directory it started in. Returning it here rather than at the
    call site means both sources and every endpoint get Codex transcripts without
    knowing Codex exists."""
    pane_pid = pane["pid"]
    if pane.get("harness") == "codex" or (pane_pid and is_codex_pane(pane_pid)):
        rollout = codex_rollout_for(pane.get("path") or "")
        if not rollout:
            return None
        meta = _codex_meta(rollout) or {}
        return {
            "sessionId": meta.get("session_id") or rollout.stem,
            "cwd": meta.get("cwd", pane.get("path", "")),
            # 'busy' is the word the API tests for; keep the vocabulary.
            "status": "busy" if codex_status(rollout) == "working" else "idle",
            "harness": "codex",
            "jsonl": str(rollout),
        }
    c = _resolve_cache.get(pane_pid)
    if c:
        claude_pid, procStart, sd_str, sessionId, cwd = c
        if _same_start(claude_pid, procStart):  # same live process
            rec = _session_record(Path(sd_str), claude_pid, sessionId, cwd)
            if rec is not None:
                return rec
        _resolve_cache.pop(pane_pid, None)  # stale (process gone/recycled) -> re-walk
    for pid in [str(pane_pid), *_proc_descendants(pane_pid)]:
        for sd in SESSION_DIRS:
            f = sd / f"{pid}.json"
            if not f.is_file():
                continue
            try:
                info = json.loads(f.read_text())
            except Exception:
                continue
            if not _same_start(pid, info.get("procStart")):
                continue  # stale record from a recycled pid
            cwd = info.get("cwd", "")
            if len(_resolve_cache) > 256:  # bound: pane pids churn over the service's lifetime
                _resolve_cache.pop(next(iter(_resolve_cache)))
            _resolve_cache[pane_pid] = (pid, info.get("procStart"), str(sd), info.get("sessionId"), cwd)
            projects_dir = sd.parent / "projects" / _encode_project_dir(cwd)
            return {**info, "jsonl": _live_jsonl(projects_dir, info.get("sessionId"))}
    return None



# --- Codex panes -----------------------------------------------------------
# A Claude Code pane renames its process to its version string; a Codex pane does
# not rename itself at all and reads as plain `node`. Matching `node` is what the
# CONDUCTOR_CLAUDE_COMMANDS escape hatch is for, and it works — but it also marks
# every `npm run dev` pane as an agent, and it tells us nothing about which
# harness is running, so the transcript layer still finds nothing to read.
#
# The honest test is the process tree: a Codex pane has a descendant whose argv
# names the codex binary. That costs one `ps` per unrecognised pane, cached.
CODEX_SESSIONS = Path(os.environ.get("CONDUCTOR_CODEX_SESSIONS",
                                     str(Path.home() / ".codex" / "sessions")))
_CODEX_ARGV_RE = re.compile(r"(?:^|/)codex(?:\s|$)|@openai/codex")
_codex_pane_cache = {}   # pane_pid -> (procStart, bool)
_codex_rollout_cache = {}  # cwd -> (path, mtime_ns)


def _argv(pid):
    try:
        return subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def is_codex_pane(pane_pid):
    """True when this pane is running Codex, by looking at what it actually runs.

    Cached against the pane's process start, so a pid recycled into something
    else is not remembered as an agent.
    """
    start = str(_proc_start_ticks(pane_pid))
    hit = _codex_pane_cache.get(pane_pid)
    if hit and hit[0] == start:
        return hit[1]
    found = any(_CODEX_ARGV_RE.search(_argv(pid))
                for pid in _proc_descendants(pane_pid))
    if len(_codex_pane_cache) > 256:
        _codex_pane_cache.pop(next(iter(_codex_pane_cache)))
    _codex_pane_cache[pane_pid] = (start, found)
    return found


def _codex_meta(path):
    """A rollout's opening `session_meta`, or None. One line, not the whole file."""
    try:
        with open(path, errors="replace") as fh:
            first = fh.readline()
        rec = json.loads(first)
    except Exception:
        return None
    return rec.get("payload") if rec.get("type") == "session_meta" else None


def codex_rollout_for(cwd, days=7):
    """The newest Codex rollout recorded for `cwd`, or None.

    Codex has no per-pid runtime record the way Claude Code does, so there is no
    exact pane->session map to use. What a rollout *does* carry is the directory
    it was started in, and this floor gives every agent its own worktree — so cwd
    identifies the agent even though it would not identify a session on a machine
    where several run in one directory. That limit is real: with two Codex
    sessions in one directory, the newest wins and the older is invisible.

    Only the last `days` of rollout directories are scanned, newest first, and
    the answer is cached per cwd until the file changes.
    """
    hit = _codex_rollout_cache.get(cwd)
    if hit:
        path, mtime = hit
        try:
            if path.exists() and path.stat().st_mtime_ns >= mtime:
                _codex_rollout_cache[cwd] = (path, path.stat().st_mtime_ns)
                return path
        except OSError:
            pass
        _codex_rollout_cache.pop(cwd, None)

    if not CODEX_SESSIONS.is_dir():
        return None
    cutoff = time.time() - days * 86400
    files = [f for f in CODEX_SESSIONS.rglob("rollout-*.jsonl")
             if _safe_mtime(f) >= cutoff]
    for path in sorted(files, key=_safe_mtime, reverse=True):
        meta = _codex_meta(path)
        if meta and meta.get("cwd") == cwd:
            try:
                _codex_rollout_cache[cwd] = (path, path.stat().st_mtime_ns)
            except OSError:
                pass
            return path
    return None


# The first user message in a Codex rollout is the harness injecting AGENTS.md,
# not something a person typed. It is thousands of characters of instructions and
# would open the glasses on a wall of text.
_CODEX_INJECTED = re.compile(r"^#\s*AGENTS\.md instructions for\b|^<INSTRUCTIONS>")


def _codex_text(content):
    """Flatten a Codex content list.

    Deliberately not `_flatten_content`: that one speaks Anthropic's block shape
    (`type: "text"`), and Codex writes `input_text` / `output_text`. Passing one
    to the other returns an empty string for every turn -- a transcript that
    reads as a session with nothing in it rather than as an error.
    """
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text"):
            parts.append(block.get("text", ""))
    return "\n".join(x for x in parts if x).strip()


def read_codex_conversation(path):
    """A Codex rollout as [{role, text}], oldest first — same shape as Claude's.

    Memoized on (mtime, size) exactly like read_conversation, because the poll
    re-reads this every couple of seconds.
    """
    try:
        st = Path(path).stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None:
        hit = _convo_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]

    turns = []
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "response_item":
            continue
        payload = rec.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue  # 'developer' is harness scaffolding, never a turn
        text = _codex_text(payload.get("content") or [])
        if not text or (role == "user" and _CODEX_INJECTED.match(text)):
            continue
        turns.append({"role": role, "text": _strip_markdown(text)})

    if key is not None:
        _convo_cache[path] = (key, turns)
        if len(_convo_cache) > 64:
            _convo_cache.pop(next(iter(_convo_cache)))
    return turns


def codex_status(path):
    """'working' or 'idle', from the rollout's own turn events.

    Claude's status is read off the spinner glyph in the pane title. Codex draws
    a different spinner, so that inference silently reports every Codex pane as
    idle. The rollout is better evidence anyway: `task_started` without a later
    `task_complete` means a turn is in flight.
    """
    state = "idle"
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            if '"task_started"' not in line and '"task_complete"' not in line:
                continue  # cheap reject before parsing
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = (rec.get("payload") or {}).get("type")
            if kind == "task_started":
                state = "working"
            elif kind == "task_complete":
                state = "idle"
    except OSError:
        return "idle"
    return state

# --- fleet views (compact renderings for the glasses) -----------------------

_STAR = "✳"                  # ✳ = idle / awaiting input
_BR_LO, _BR_HI = 0x2800, 0x28FF  # braille range = Claude's working spinner
_GLYPH = {"working": "▶", "idle": "✳", "other": "·"}


def session_status(p):
    """'working' | 'idle' | 'other'(not an agent).

    Claude Code's spinner is a braille glyph in the pane title, so its state is
    read from there. Codex draws its own spinner, which that test does not
    recognise -- it would report every Codex pane idle forever. Its rollout says
    plainly whether a turn is in flight, so ask that instead.
    """
    # Same trap as is_claude: comparing against the literal "claude" marks every real
    # Claude Code pane 'other', so the fleet list sorts them last and shows no activity.
    if p.get("harness") == "codex":
        path = codex_rollout_for(p.get("path") or "")
        return codex_status(path) if path else "idle"
    if not _is_claude_command(p["command"]):
        return "other"
    t = (p["title"] or "").strip()
    if t and _BR_LO <= ord(t[0]) <= _BR_HI:
        return "working"
    return "idle"


def session_label(p):
    """The name a person would recognise for this pane.

    A pane title is written by the agent and drifts: Claude Code derives it from
    a session's FIRST message, so the orchestrator pane still read "Clone Comp AI
    repositories" days after it stopped doing that, and Daniel opened a pane by
    its label and got a different agent.

    This fork used to ask `ai-composer inspect sessions` for the real name. That
    binary is gone, so the lookup failed on every call and silently returned to
    the drifting title. Three better answers exist, in this order:

    1. **The tmux window name**, when someone renamed the window. A default
       window carries the running command, so a name that is not the command is
       a name a person typed — on an agent floor it is the agent's name, and it
       is what they already read on screen.
    2. **The session's own name.** The harness keeps this current, but derives
       it: Claude Code produced `drodriguez-b5` for the orchestrator, from the
       directory rather than the work. Correct, and less recognisable than what
       the person wrote on the window.
    3. **The pane title**, glyph stripped — upstream's behaviour, still the
       fallback when nobody has named anything.
    """
    window = (p.get("window_name") or "").strip()
    # A window still carrying its command name says nothing a title would not.
    if window and window not in ("", "zsh", "bash", "sh", "fish", p.get("command", "")):
        return window

    try:
        named = ((resolve_session(p) or {}).get("name") or "").strip()
    except Exception:
        named = ""
    if named:
        return named

    t = (p["title"] or "").strip()
    if t and (t[0] == _STAR or _BR_LO <= ord(t[0]) <= _BR_HI):
        t = t[1:].strip()
    return t or p["command"]


def window_tag(p, width=6):
    """Short window-name annotation; falls back to wN for the unnamed window."""
    name = (p["window_name"] or "").strip()
    return (name if name else f"w{p['window_index']}")[:width]


def _clip(s, n):
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def render_fleet_flat(rows=12, page=0, claude_only=True, width=30):
    """VIEW 1 — flat list, one row per session, window-tagged, working-first,
    paged. Returns ready-to-display monospace text."""
    panes = [p for p in list_panes() if (p["is_claude"] or not claude_only)]
    # Sort by pane id, which never changes. The previous key was
    # (status, window_index, pane_index): status flips every few seconds as
    # agents work and idle, and tmux REUSES window indices when a window closes,
    # so an entry moved for reasons having nothing to do with its own agent.
    # Anyone selecting by position got whoever happened to be in that slot.
    def _pane_num(p):
        raw = str(p.get("pane_id", "")).lstrip("%")
        return int(raw) if raw.isdigit() else 1 << 30
    panes.sort(key=_pane_num)
    total = len(panes)
    pages = max(1, (total + rows - 1) // rows)
    page = max(0, min(page, pages - 1))
    chunk = panes[page * rows:(page + 1) * rows]
    label_w = max(8, width - 12)
    out = [f"PANELS  {total} sessions   pg {page + 1}/{pages}", "-" * width]
    for i, p in enumerate(chunk, start=1 + page * rows):
        out.append(f"{i:>2} {window_tag(p):<6} "
                   f"{_clip(session_label(p), label_w):<{label_w}} {_GLYPH[session_status(p)]}")
    out += ["-" * width, 'say # or name · swipe · "more"']
    return "\n".join(out)


def render_fleet_dashboard(width=30, max_dots=10):
    """VIEW 3 — one row per window (name + a dot per session + count), working
    window pinned on top, shells collapsed. Returns monospace text."""
    wins = {}
    for p in list_panes():
        wins.setdefault((p["window_index"], p["window_name"]), []).append(p)
    claude_wins, shell_wins, n_work, n_idle = [], [], 0, 0
    for (wi, wn), ps in wins.items():
        cl = [p for p in ps if p["is_claude"]]
        if not cl:
            shell_wins.append((wi, wn))
            continue
        statuses = [session_status(p) for p in cl]
        n_work += statuses.count("working")
        n_idle += statuses.count("idle")
        claude_wins.append({"wi": wi, "wn": wn, "n": len(cl),
                            "work": statuses.count("working"), "statuses": statuses})
    claude_wins.sort(key=lambda d: (0 if d["work"] else 1, -d["n"], d["wi"]))
    out = [f"FLEET {n_work + n_idle}s · {n_work} ▶work · {n_idle} ✳", "-" * width]
    for d in claude_wins:
        dots = "".join("▶" if s == "working" else "•" for s in d["statuses"])
        if len(dots) > max_dots:
            dots = dots[:max_dots - 2] + f"+{d['n'] - (max_dots - 2)}"
        name = _clip(d["wn"] or "(unnamed)", 8)
        out.append(f"{d['wi']:>2} {name:<9} {dots:<{max_dots}} {d['n']:>2}"
                   f"{'  WORK' if d['work'] else ''}")
    if shell_wins:
        bits = " · ".join(f"{wi} {wn or 'w' + str(wi)}" for wi, wn in shell_wins)
        out.append(_clip("· " + bits + " (shells)", width))
    out += ["-" * width, "say a window #  → its panes"]
    return "\n".join(out)


# --- MCP server -------------------------------------------------------------

def build_mcp():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("tmux-conductor")

    @mcp.tool()
    def tmux_list_panes(claude_only: bool = False) -> list:
        """List tmux panes across the whole server. claude_only=True returns only
        panes running a Claude Code session. Use a pane's stable `pane_id`
        (e.g. '%29') as the target for the other tools. A Claude pane's `title`
        encodes the session's task (and a status glyph)."""
        return list_panes(claude_only=claude_only)

    @mcp.tool()
    def tmux_capture(target: str, lines: int = 200) -> str:
        """Rendered text of a pane right now + up to `lines` of scrollback. Best
        for seeing a session's current state / whether it's waiting on input."""
        return capture_pane(target, lines)

    @mcp.tool()
    def tmux_select(window: Optional[str] = None, pane: Optional[str] = None) -> dict:
        """Switch the attached client's focus. e.g. window='0:7', pane='%29'."""
        select_target(window, pane)
        return {"ok": True, "window": window, "pane": pane}

    @mcp.tool()
    def tmux_send_text(target: str, text: str, submit: bool = True) -> dict:
        """Type free text into a pane (e.g. a prompt into a Claude session) and
        press Enter if submit=True. Refuses to target the conductor's own pane."""
        return send_text(target, text, submit=submit)

    @mcp.tool()
    def tmux_send_keys(target: str, keys: str) -> dict:
        """Send a raw tmux key name to a pane — 'Enter', 'Escape', 'C-c', 'Up'.
        For control keys / interrupting a session, not free text."""
        return send_keys(target, keys)

    @mcp.tool()
    def tmux_read_transcript(target: Optional[str] = None,
                             jsonl_path: Optional[str] = None,
                             last: int = 20) -> dict:
        """Read a Claude session's saved transcript (cleaner than capture for
        history). Pass a pane `target` (its cwd maps to the session JSONL; if
        several sessions share that cwd the most-recent is used and the rest are
        listed in `alternatives`) or an explicit `jsonl_path`. Returns the last
        `last` user/assistant turns."""
        if jsonl_path:
            return {"jsonl_path": jsonl_path, "turns": read_transcript(jsonl_path, last)}
        if not target:
            return {"error": "pass either target (a pane_id) or jsonl_path"}
        pane = next((p for p in list_panes() if p["pane_id"] == target), None)
        if not pane:
            return {"error": f"unknown pane {target}"}
        cands = transcript_candidates(pane["path"], pane["pid"])
        if not cands:
            return {"error": f"no transcript found for cwd {pane['path']}", "cwd": pane["path"]}
        return {
            "jsonl_path": str(cands[0]),
            "alternatives": [str(c) for c in cands[1:]],
            "cwd": pane["path"],
            "turns": read_transcript(cands[0], last),
        }

    @mcp.tool()
    def tmux_fleet_flat(rows: int = 12, page: int = 0, claude_only: bool = True) -> str:
        """VIEW 1 — compact FLAT list for the glasses: one row per session, tagged
        with its window name, sorted working/attention-first, paged. Returns
        ready-to-display monospace text."""
        return render_fleet_flat(rows=rows, page=page, claude_only=claude_only)

    @mcp.tool()
    def tmux_fleet_dashboard() -> str:
        """VIEW 3 — compact one-screen DASHBOARD for the glasses: one row per
        window (name + a dot per session + count), working window pinned on top.
        Returns ready-to-display monospace text."""
        return render_fleet_dashboard()

    return mcp


# --- self-test (read-only) --------------------------------------------------

def selftest():
    print("import FastMCP:", end=" ")
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
        print("ok")
    except Exception as e:
        print("FAILED:", e)
    print("SELF_PANE:", SELF_PANE or "(unset)")
    print("project roots:", [str(r) for r in PROJECT_ROOTS if r.exists()])

    panes = list_panes()
    claude = [p for p in panes if p["is_claude"]]
    print(f"\npanes: {len(panes)} total, {len(claude)} claude")
    for p in claude[:6]:
        flag = " <conductor>" if p["is_conductor"] else ""
        print(f"  {p['pane_id']:>4} {p['session']}:{p['window_index']}.{p['pane_index']}"
              f" '{p['title']}' {p['path']}{flag}")

    sample = next((p for p in claude if not p["is_conductor"]), None)
    if not sample:
        print("\n(no non-conductor claude pane to sample)")
        return
    print(f"\n-- capture {sample['pane_id']} ({sample['title']}) tail --")
    cap = capture_pane(sample["pane_id"], 40).splitlines()
    print("\n".join(l for l in cap if l.strip())[-400:])

    cands = transcript_candidates(sample["path"], sample["pid"])
    print(f"\n-- transcript candidates for {sample['path']}: {len(cands)} --")
    if cands:
        print("newest:", cands[0])
        for t in read_transcript(cands[0], last=3):
            print(f"  [{t['role']}] {t['text'][:90]!r}")

    print("\n-- VIEW 1: FLEET FLAT --")
    print(render_fleet_flat())
    print("\n-- VIEW 3: FLEET DASHBOARD --")
    print(render_fleet_dashboard())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        build_mcp().run()
