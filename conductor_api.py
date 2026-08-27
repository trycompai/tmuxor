#!/usr/bin/env python3
"""JSON control-plane for the tmux conductor — the data/control API that the
Even Hub glasses PLUGIN (Path B) consumes. Clean JSON + SSE over the existing
tmux_conductor functions. Separate from even_conductor_server.py (the Path-A
OpenAI/Add-Agent text bridge).

Pane ids in URLs are the NUMBER only (no '%'), because '%' is URL-encoding —
e.g. pane '%29' -> /api/panes/29/screen. The server re-adds the '%'.

Endpoints:
  GET  /api/health
  GET  /api/panes[?claude_only=1]                 -> {panes:[...]}
  GET  /api/panes/<n>/screen[?lines=200]          -> {id, text}
  GET  /api/panes/<n>/conversation                -> {id, turns:[{role,text}...], working}
  GET  /api/windows                               -> {windows:[{index,name,panes}]}
  POST /api/panes/<n>/send  {text, submit=true}   -> {ok, pane, submitted}
  POST /api/panes/<n>/keys  {keys}                -> {ok, pane, keys}
  POST /api/new-session     {path|text, tag?}     -> {ok, pane, n, cwd, how}
  GET  /api/events/<n>[?lines=40]                 -> SSE stream of the pane screen on change

Run:  python conductor_api.py
Bind: defaults to your Tailscale IP (tailscale ip -4), port 8790.
Env:  CONDUCTOR_TOKEN (optional bearer), CONDUCTOR_API_PORT, CONDUCTOR_BIND.
"""
import hmac
import json
import os
import re
import threading
import time
from collections import deque
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import tmux_conductor as tc
import sources  # pluggable backend: tmux (default) or herdr, selected per request

# parallelize the per-pane `capture-pane` forks in /api/panes (one per idle Claude
# pane) so their latency overlaps instead of summing on the 5s fleet poll.
_POOL = ThreadPoolExecutor(max_workers=8)

TOKEN = os.environ.get("CONDUCTOR_TOKEN", "")
# New sessions create their folder UNDER this base only (so we never touch unrelated dirs).
# Default ~/projects; the phone Setup can override it per-request.
PROJECTS_BASE = os.path.abspath(os.path.expanduser(os.environ.get("CONDUCTOR_PROJECTS_DIR", "~/projects")))


def _projects_base(override=None):
    return os.path.abspath(os.path.expanduser(override)) if override else PROJECTS_BASE


# A pane is "waiting" (needs you) when an interactive prompt/menu is on screen.
_WAIT_RE = re.compile(r"(?:to navigate|to select|esc to cancel|\(y/n\)|\[y/n\]|\by/n\b|press enter to continue)", re.I)


def _pane_status(src, p):
    st = src.session_status(p)
    if getattr(src, "native_status", False):
        return st  # herdr reports blocked/idle/working directly — no on-screen regex needed
    if st == "idle" and p["is_claude"]:
        try:
            if _WAIT_RE.search(src.capture_pane(p["pane_id"], 16)):
                return "waiting"
        except Exception:
            pass
    return st


def pane_view(src, p):
    return {
        "id": p["pane_id"],
        "n": src.pane_n(p),                    # tmux '%29'->'29'; herdr 'w3:p6' (opaque URL token)
        "window": p["window_index"],
        "window_name": p["window_name"],
        "pane_index": p["pane_index"],
        "title": p["title"],
        "label": src.session_label(p),
        "tag": src.window_tag(p),
        "status": _pane_status(src, p),
        "cwd": p["path"],
        "is_claude": p["is_claude"],
        "is_conductor": p["is_conductor"],
    }


def find_pane(src, n):
    for p in src.list_panes():
        if src.pane_n(p) == n:
            return p
    return None


# --- AI: turn a spoken description into a shell command (for non-claude panes) ---

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _anthropic_key():
    v = os.environ.get("ANTHROPIC_API_KEY", "")
    if v.startswith("sk-ant-"):
        return v
    try:
        with open(os.path.expanduser("~/.env")) as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("export "):
                    ln = ln[7:]
                if "=" in ln:
                    val = ln.split("=", 1)[1].strip().strip('"').strip("'")
                    if val.startswith("sk-ant-"):
                        return val
    except FileNotFoundError:
        pass
    return None


def translate_command(description, cwd):
    """Turn a natural-language description into a single shell command via Claude."""
    key = _anthropic_key()
    if not key:
        raise RuntimeError("no Anthropic key (set ANTHROPIC_API_KEY or ~/.env)")
    system = (
        "Convert the user's request into ONE shell command for bash to run in "
        + (cwd or "~") + ". Output ONLY the command on a single line — no prose, "
        "no markdown, no backticks."
    )
    payload = {
        "model": os.environ.get("CONDUCTOR_MODEL", "claude-opus-4-8"),
        "max_tokens": 300,
        "system": system,
        "messages": [{"role": "user", "content": description}],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text.strip().strip("`").strip()


WHISPER_USD_PER_MIN = 0.006  # OpenAI whisper-1 pricing

# Voice input reaches an agent as ordinary text, and speech-to-text mishears in ways
# that read as confident nonsense: "tmux session" arrived as "team obsession", and an
# agent spent several paragraphs answering the wrong question. The agent cannot tell,
# because /api/transcribe and /api/panes/N/send are separate calls and the text looks
# identical either way.
#
# So remember what Whisper returned, and when that same text is sent to a pane, label
# it. Typed input is left alone -- a marker on everything would be noise, and this is
# only worth saying when it is actually true.
# The server is threaded, so a send can read this while a transcribe appends to it.
# Iterating a deque mid-append raises RuntimeError and would 500 the very request
# carrying Daniel's message. Snapshot under a lock instead.
_RECENT_TRANSCRIPTS = deque(maxlen=24)      # (monotonic_time, text)
_TRANSCRIPTS_LOCK = threading.Lock()
_TRANSCRIPT_TTL = 600.0                      # a minute of thought, then a long pause
# As short as it can be while still meaning something. This is rendered on the
# G2 heads-up display as well as read by the agent, so every character costs
# Daniel screen space. "voice" carries the whole point -- a reader seeing an odd
# word knows to suspect a mishearing rather than an instruction. Two longer
# wordings were tried first and both read as clutter.
TRANSCRIPT_MARKER = "(voice) "


def _remember_transcript(text):
    if text and text.strip():
        with _TRANSCRIPTS_LOCK:
            _RECENT_TRANSCRIPTS.append((time.monotonic(), text.strip()))


def _tap_transcript(text):
    _remember_transcript(text)
    return text


def _was_transcribed(text):
    """True if this exact text came back from Whisper recently."""
    t = (text or "").strip()
    if not t:
        return False
    now = time.monotonic()
    with _TRANSCRIPTS_LOCK:
        recent = list(_RECENT_TRANSCRIPTS)
    return any(now - when <= _TRANSCRIPT_TTL and t == said for when, said in recent)


def wav_seconds(audio):
    """Duration of a PCM16 mono WAV (our recorder's format): data bytes / (rate*2)."""
    try:
        rate = int.from_bytes(audio[24:28], "little")
        data = int.from_bytes(audio[40:44], "little")
        if rate and data:
            return data / (rate * 2)
    except Exception:
        pass
    return 0.0


def _openai_key_files(path_override=None):
    return [p for p in (path_override, os.environ.get("CONDUCTOR_OPENAI_KEY_PATH"),
                        "~/.env", "~/.openai", "~/.config/openai/key", "~/.config/openai.env") if p]


def openai_key_checked(path_override=None):
    """Human-readable list of WHERE we look for the key — shown when voice is off."""
    return ["OPENAI_API_KEY env var"] + [os.path.expanduser(p) for p in _openai_key_files(path_override)]


def openai_key(path_override=None):
    """Find the OpenAI key WITHOUT it ever touching the phone: env var first, then common key
    files on this machine (and an optional user-set path). Returns '' if none found. Excludes
    Anthropic keys (sk-ant-)."""
    k = os.environ.get("OPENAI_API_KEY", "")
    if k.startswith("sk-") and not k.startswith("sk-ant-"):
        return k
    for p in _openai_key_files(path_override):
        try:
            with open(os.path.expanduser(p)) as f:
                txt = f.read()
        except OSError:
            continue
        # prefer an OPENAI_API_KEY=... line; else a bare sk-... that isn't an Anthropic key
        m = re.search(r"OPENAI_API_KEY\s*[=:]\s*['\"]?(sk-[A-Za-z0-9_\-]{20,})", txt) \
            or re.search(r"(?<![A-Za-z0-9])(sk-(?!ant-)[A-Za-z0-9_\-]{20,})", txt)
        if m:
            return m.group(1)
    return ""


def whisper_transcribe(audio, key):
    """Transcribe WAV audio bytes via OpenAI Whisper using the provided key."""
    if not key.startswith("sk-"):
        raise RuntimeError("no OpenAI API key found (set OPENAI_API_KEY, put it in ~/.env, or set a key-file path in Setup)")
    boundary = "----conductor-" + str(int(time.time() * 1000))
    crlf = b"\r\n"
    bb = boundary.encode()

    def field(name, value):
        return (b"--" + bb + crlf
                + ('Content-Disposition: form-data; name="%s"' % name).encode() + crlf + crlf
                + value.encode() + crlf)

    body = field("model", "whisper-1") + field("response_format", "json")
    body += (b"--" + bb + crlf
             + b'Content-Disposition: form-data; name="file"; filename="audio.wav"' + crlf
             + b"Content-Type: audio/wav" + crlf + crlf + audio + crlf
             + b"--" + bb + b"--" + crlf)
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode()).get("text", "").strip()


# --- new session: resolve a spoken folder -> dir, spawn a pane in window 1 ---

def resolve_folder(src, spoken, base=None):
    s = (spoken or "").strip().rstrip(".")
    if not s:
        return None
    cand = os.path.expanduser(s)
    if os.path.isdir(cand):
        return os.path.abspath(cand)
    norm = re.sub(r"[\s_\-]", "", s.lower())
    bases, seen = [], set()
    for root in (_projects_base(base), "~/projects", "~/Documents", "~"):
        r = os.path.expanduser(root)
        try:
            for d in sorted(os.listdir(r)):
                p = os.path.join(r, d)
                if os.path.isdir(p) and p not in seen:
                    seen.add(p)
                    bases.append(p)
        except OSError:
            pass
    for p in src.list_panes():
        if os.path.isdir(p["path"]) and p["path"] not in seen:
            seen.add(p["path"])
            bases.append(p["path"])
    named = [(re.sub(r"[\s_\-]", "", os.path.basename(b).lower()), b) for b in bases]
    for name, b in named:  # exact basename first
        if name and name == norm:
            return os.path.abspath(b)
    for name, b in named:  # then partial — require a substantial (>=4 char) overlap so a short
        if name and min(len(name), len(norm)) >= 4 and (norm in name or name in norm):  # folder name doesn't match unrelated input
            return os.path.abspath(b)
    return None


def propose_folder(spoken, base):
    """When no folder matches, propose a safe path to CREATE under `base`: <base>/<name>."""
    name = re.sub(r"[^\w.-]+", "-", (spoken or "").strip().lower()).strip("-")[:40] or "session"
    return os.path.join(base, name)


def _ensure_under_base(folder, base=None):
    """Create the session folder if missing — but ONLY under the projects base, so we
    never touch unrelated dirs. Source-independent (filesystem); runs before the
    selected source spawns the session. The per-source spawn (tmux window / herdr
    workspace) lives in sources.py."""
    if os.path.isdir(folder):
        return
    af, b = os.path.abspath(folder), _projects_base(base)
    if af == b or af.startswith(b + os.sep):
        os.makedirs(folder, exist_ok=True)
    else:
        raise RuntimeError("refusing to create a folder outside the projects base")


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # drop slow/half-open connections so they can't pin threads (slowloris)

    # --- helpers ---
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        # token is REQUIRED in every mode (no default-open); main() refuses to start
        # without one. Bearer header, or ?token= for SSE (EventSource can't set headers).
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not tok:
            tok = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        # compare BYTES: hmac.compare_digest on two str raises TypeError on non-ASCII
        # input (e.g. ?token=%C3%A9), which would abort the request thread.
        return bool(TOKEN) and hmac.compare_digest(tok.encode(), TOKEN.encode())  # constant-time

    def _body(self):
        n = int(self.headers.get("content-length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return None

    def _source(self):
        # per-request backend: ?source= (also works for SSE/EventSource, which can't
        # set headers) or the X-Conductor-Source header. No selection -> the default
        # (tmux), resolved WITHOUT any availability probe so the hot poll is unchanged.
        q = parse_qs(urlparse(self.path).query)
        requested = q.get("source", [None])[0] or self.headers.get("X-Conductor-Source")
        return sources.get_source(sources.resolve_source_name(requested))

    def log_message(self, *a):
        pass

    # --- GET ---
    def do_OPTIONS(self):  # CORS preflight for POSTs from the phone webview
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "authorization, content-type, x-conductor-source")
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self):
        # any unhandled error (bad ?lines=, a pane dying mid-request) -> JSON 500, not a dropped
        # connection. The SSE route sends its own headers; if it already did, the 500 send no-ops.
        try:
            self._route_get()
        except Exception as e:
            try: self._json(500, {"error": str(e)})
            except Exception: pass

    def _route_get(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/api/health":
            # advertise whether voice works (an OpenAI key was found) so the app can gate the
            # talk gesture instead of failing on transcribe. Returns a bool, never the key.
            # Health stays unauthenticated as a reachability probe, BUT the caller-supplied
            # keypath (which opens a file) and the absolute "checked" paths are honored ONLY
            # for an authenticated caller — otherwise an unauthenticated tailnet peer could
            # open an arbitrary file (DoS / secret-presence oracle) or learn this machine's
            # home directory. The phone always carries the token, so it still gets both.
            authed = self._authed()
            kp = q.get("keypath", [None])[0] if authed else None
            # Only probe for the key (which reads ~/.env etc. and reveals its presence) when authed —
            # otherwise an unauthenticated tailnet peer gets a secret-presence oracle. The phone always
            # carries the token, so it still gets the real value; an anon probe just sees reachability.
            voice = bool(openai_key(kp)) if authed else False
            resp = {"ok": True, "service": "conductor-api", "voice": voice}
            if authed:
                # which multiplexers this box can serve + the default, so the phone can
                # show a source picker gated on what's actually installed. Authed-only:
                # don't let an anon tailnet peer probe what's running here.
                resp["sources"] = sources.available_sources()
                resp["source"] = sources.DEFAULT_SOURCE
            if not voice and authed:
                resp["checked"] = openai_key_checked(kp)  # tell the user WHERE we looked
            return self._json(200, resp)
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        src = self._source()
        if path == "/api/panes":
            claude_only = q.get("claude_only", ["0"])[0] in ("1", "true")
            panes = src.list_panes(claude_only=claude_only)
            views = list(_POOL.map(lambda p: pane_view(src, p), panes)) if panes else []  # captures run concurrently
            return self._json(200, {"panes": views})
        if path == "/api/windows":
            return self._json(200, {"windows": src.list_windows()})
        m = re.fullmatch(r"/api/panes/([^/]+)/screen", path)  # id: tmux '29' or herdr 'w3:p6'
        if m:
            p = find_pane(src, unquote(m.group(1)))
            if not p:
                return self._json(404, {"error": "no such pane"})
            lines = int(q.get("lines", ["200"])[0])
            return self._json(200, {"id": p["pane_id"], "text": src.capture_pane(p["pane_id"], lines)})
        m = re.fullmatch(r"/api/panes/([^/]+)/conversation", path)
        if m:
            p = find_pane(src, unquote(m.group(1)))
            if not p:
                return self._json(404, {"error": "no such pane"})
            sess = src.resolve_session(p)
            if sess is not None:
                # exact session identified: use ITS transcript. jsonl=None means a
                # brand-new session with no turns yet -> show empty, NEVER fall back
                # to "newest in folder" (that's a different session's conversation).
                jsonl = sess.get("jsonl")
                working = sess.get("status") == "busy"
            else:
                # couldn't identify the session -> best-effort newest transcript in cwd
                cands = src.transcript_candidates(p)
                jsonl = str(cands[0]) if cands else None
                working = _pane_status(src, p) == "working"
            # etag = transcript identity + working flag, passed back via ?etag=. When it
            # matches, return a tiny {notModified} 200 (skip read/parse/serialize) — a
            # plain 200 (not HTTP 304) so the WebView's fetch never sees a bare 304.
            etag = None
            if jsonl:
                try:
                    st = os.stat(jsonl)
                    etag = f"{st.st_mtime_ns}-{st.st_size}-{int(working)}"
                except OSError:
                    pass
            if etag and q.get("etag", [""])[0] == etag:
                return self._json(200, {"id": p["pane_id"], "notModified": True, "etag": etag})
            turns = tc.read_conversation(jsonl) if jsonl else []
            return self._json(200, {"id": p["pane_id"], "turns": turns, "working": working, "etag": etag})
        m = re.fullmatch(r"/api/events/([^/]+)", path)
        if m:
            return self._sse_screen(src, unquote(m.group(1)), int(q.get("lines", ["40"])[0]))
        self._json(404, {"error": "not_found", "path": path})

    # --- POST ---
    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            try: self._json(500, {"error": str(e)})
            except Exception: pass

    def _route_post(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        src = self._source()
        if path == "/api/transcribe":  # raw WAV body -> Whisper (read before JSON parse)
            n = int(self.headers.get("content-length", 0))
            audio = self.rfile.read(n)
            try:
                secs = wav_seconds(audio)
                return self._json(200, {
                    "text": _tap_transcript(whisper_transcribe(audio, openai_key(q.get("keypath", [None])[0]))),
                    "seconds": round(secs, 1),
                    "cost": round(secs / 60 * WHISPER_USD_PER_MIN, 4),
                })
            except Exception as e:
                return self._json(502, {"error": str(e)})
        body = self._body()
        if body is None:
            return self._json(400, {"error": "invalid JSON"})
        m = re.fullmatch(r"/api/panes/([^/]+)/send", path)
        if m:
            p = find_pane(src, unquote(m.group(1)))
            if not p:
                return self._json(404, {"error": "no such pane"})
            text = body.get("text", "")
            if not text:
                return self._json(400, {"error": "text required"})
            if _was_transcribed(text):
                text = TRANSCRIPT_MARKER + text
            try:  # pane may be unsendable (the conductor's own pane) or closed mid-request
                r = src.send_text(p["pane_id"], text, submit=bool(body.get("submit", True)))
            except Exception as e:
                return self._json(502, {"error": str(e)})
            return self._json(200, {"ok": True, **r})
        m = re.fullmatch(r"/api/panes/([^/]+)/keys", path)
        if m:
            p = find_pane(src, unquote(m.group(1)))
            if not p:
                return self._json(404, {"error": "no such pane"})
            keys = body.get("keys")
            if not keys:
                return self._json(400, {"error": "keys required"})
            try:
                return self._json(200, {"ok": True, **src.send_keys(p["pane_id"], keys)})
            except Exception as e:
                return self._json(502, {"error": str(e)})
        if path == "/api/clientlog":
            # the glasses app POSTs uncaught JS errors here so they land in the service journal
            # (journalctl --user -u tmux-conductor) — turns a mysterious app "quit" into a readable
            # error+stack. Just logs a bounded string; no execution, no injection surface.
            print("[clientlog] " + str(body.get("msg", ""))[:4000], flush=True)
            return self._json(200, {"ok": True})
        if path == "/api/translate":
            desc = body.get("description", "")
            if not desc:
                return self._json(400, {"error": "description required"})
            try:
                return self._json(200, {"command": translate_command(desc, body.get("cwd", ""))})
            except Exception as e:
                return self._json(502, {"error": str(e)})
        if path == "/api/resolve-folder":
            q = body.get("text", ""); base = _projects_base(body.get("base"))
            f = resolve_folder(src, q, base)
            return self._json(200, {"found": bool(f), "path": f or "", "create_path": propose_folder(q, base), "query": q})
        if path == "/api/new-session":
            base = body.get("base")
            f = body.get("path") or resolve_folder(src, body.get("text", ""), base) or propose_folder(body.get("text", ""), _projects_base(base))
            if not f:
                return self._json(400, {"error": "folder not found", "query": body.get("text", "")})
            try:
                # sanitize the spoken tag -> a safe window/workspace name (no tabs/newlines/
                # control chars that would corrupt the tab-delimited list-panes parse)
                tag = re.sub(r"[^\w.-]", "", (body.get("tag") or "").strip()) or None
                _ensure_under_base(f, base)          # confine new dirs to the projects base (source-independent)
                pane, how = src.create_session(f, tag)
                return self._json(200, {"ok": True, "pane": pane, "n": src.pane_n({"pane_id": pane}), "cwd": f, "how": how})
            except Exception as e:
                return self._json(502, {"error": str(e)})
        self._json(404, {"error": "not_found", "path": path})

    # --- SSE: stream a pane's screen whenever it changes ---
    def _sse_screen(self, src, n, lines):
        p = find_pane(src, n)
        if not p:
            return self._json(404, {"error": "no such pane"})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        last = None
        try:
            for i in range(36000):  # ~10h cap at 1s
                screen = src.capture_pane(p["pane_id"], lines)
                if screen != last:
                    last = screen
                    payload = json.dumps({"id": p["pane_id"], "text": screen})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                elif i % 15 == 0:
                    self.wfile.write(b": ping\n\n")  # heartbeat: a disconnected client surfaces within ~15s
                    self.wfile.flush()                # (instead of only when the screen next changes)
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            return  # pane closed mid-stream, or any other error -> end the stream cleanly


def main():
    port = int(os.environ.get("CONDUCTOR_API_PORT", "8790"))
    # fail closed: this control plane runs commands on the box, and loopback is NOT
    # user-isolated on Linux — so require a token in EVERY mode, and default to loopback.
    if not TOKEN:
        raise SystemExit("refusing to start without CONDUCTOR_TOKEN — this runs commands on your machine; set a token.")
    # default to loopback; publish to the tailnet over HTTPS with `tailscale serve`
    # (install.sh does this). Never bind the open network — refuse 0.0.0.0 outright.
    bind = os.environ.get("CONDUCTOR_BIND") or "127.0.0.1"
    if bind == "0.0.0.0":
        raise SystemExit("refusing to bind 0.0.0.0 — this runs commands on your machine; bind 127.0.0.1 and expose it with `tailscale serve`.")
    print(f"conductor-api on http://{bind}:{port}  (token required)")
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
