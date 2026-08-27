#!/usr/bin/env bash
# TMUXor backend installer — one command to stand up the control plane on YOUR machine.
#
#   curl -fsSL https://raw.githubusercontent.com/liyiyuian/tmuxor/main/install.sh | bash
#
# It: checks prereqs, downloads the backend, generates a token, writes the env file,
# installs a systemd --user service, exposes it on your tailnet, and prints the
# Backend URL + token + a paste-config blob to enter in the glasses app.
#
# This backend runs commands on your machine — it is loopback-bound + token-required +
# tailnet-only. Never expose it publicly or share the token.
#
# Testing hooks (not for normal use):
#   TMUXOR_SRC=/path/to/repo   copy backend files from a local dir instead of curl
#   TMUXOR_DRYRUN=1            don't touch systemd/tailscale/sudo (print instead)
#   TMUXOR_OPENAI_KEY=sk-...   supply the OpenAI key non-interactively ("" = skip voice)
set -euo pipefail

REPO="${TMUXOR_REPO:-liyiyuian/tmuxor}"
RAW="https://raw.githubusercontent.com/${REPO}/main"
PORT="${CONDUCTOR_API_PORT:-8790}"
INSTALL_DIR="${TMUXOR_DIR:-$HOME/.local/share/tmuxor}"
ENV_FILE="${TMUXOR_ENV:-$HOME/.config/tmux-conductor.env}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# conductor_api.py is portable; service management is not. Detect rather than assume:
# the upstream installer writes a systemd unit unconditionally, which on macOS leaves
# the backend installed and unsupervised with no error.
case "$(uname -s)" in
  Darwin) SERVICE_KIND="launchd" ;;
  Linux)  SERVICE_KIND="systemd" ;;
  *)      SERVICE_KIND="none" ;;
esac
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABEL="ai.comp.tmuxor"
DRY="${TMUXOR_DRYRUN:-0}"

c()  { printf '\033[36m%s\033[0m\n' "$*"; }      # info
ok() { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m! %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
run(){ if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }
# The tailscale CLI hardcodes /var/run/tailscaled.socket and ignores TS_SOCKET, so a
# userspace daemon on a private socket is unreachable without --socket. Set
# TMUXOR_TS_SOCKET to point at one; unset, this behaves exactly as upstream.
ts(){ if [ -n "${TMUXOR_TS_SOCKET:-}" ]; then tailscale --socket="$TMUXOR_TS_SOCKET" "$@"; else tailscale "$@"; fi; }

c "TMUXor backend installer"

# 1) prerequisites -----------------------------------------------------------
# macOS ships python3 as 3.9 via Command Line Tools, so "python3 exists" is not the same
# as "python3 is new enough". Pick the first interpreter that actually satisfies 3.10+.
PY_BIN="${TMUXOR_PYTHON:-}"
if [ -z "$PY_BIN" ]; then
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    c_path=$(command -v "$cand" 2>/dev/null) || continue
    "$c_path" -c 'import sys;exit(0 if sys.version_info[:2]>=(3,10) else 1)' 2>/dev/null || continue
    PY_BIN="$c_path"; break
  done
fi
[ -n "$PY_BIN" ] || die "no python3 >= 3.10 found (system python3 is $(python3 -V 2>&1 | awk '{print $2}' 2>/dev/null || echo absent)). Install one, or set TMUXOR_PYTHON."
PYV=$("$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
ok "python $PYV ($PY_BIN)"
command -v tmux >/dev/null   && ok "tmux"      || die "tmux not found. Install tmux."
command -v claude >/dev/null && ok "claude"    || warn "claude (Claude Code) not on PATH — install it so sessions can launch."
command -v tailscale >/dev/null && ok "tailscale" || die "tailscale not found. Install + log in: https://tailscale.com/download"

# 2) download backend --------------------------------------------------------
mkdir -p "$INSTALL_DIR"
for f in conductor_api.py tmux_conductor.py sources.py; do
  if [ -n "${TMUXOR_SRC:-}" ]; then
    cp "$TMUXOR_SRC/$f" "$INSTALL_DIR/$f"
  else
    curl -fsSL "$RAW/$f" -o "$INSTALL_DIR/$f" || die "could not download $f from $RAW"
  fi
done
ok "backend in $INSTALL_DIR"

# 3) token (reuse existing if present) ---------------------------------------
TOKEN=""
[ -f "$ENV_FILE" ] && TOKEN=$(sed -n 's/^CONDUCTOR_TOKEN=//p' "$ENV_FILE" | head -1)
if [ -z "$TOKEN" ]; then
  TOKEN="tmxr_$("$PY_BIN" -c 'import secrets;print(secrets.token_urlsafe(24))')"
  ok "generated a new access token"
else
  ok "reusing existing access token"
fi

# 4) OpenAI key (OPTIONAL) — enables VOICE input. Without it you just type replies and
#    new-session names on your phone instead. ---------------------------------
if [ "${TMUXOR_OPENAI_KEY+set}" = set ]; then
  OPENAI_KEY="$TMUXOR_OPENAI_KEY"
elif [ -r /dev/tty ]; then
  printf 'OpenAI API key (optional) — enables VOICE input via Whisper; without it you type on your phone. Paste it, or Enter to skip: '
  read -r OPENAI_KEY </dev/tty || OPENAI_KEY=""
else
  OPENAI_KEY=""
fi
[ -n "$OPENAI_KEY" ] && ok "voice input enabled" || warn "no OpenAI key — voice input off; you'll type replies/new-session names on your phone (re-run later to add voice)."

# 5) write env file (chmod 600) ---------------------------------------------
mkdir -p "$(dirname "$ENV_FILE")"
umask 177
{
  echo "CONDUCTOR_TOKEN=$TOKEN"
  echo "CONDUCTOR_BIND=127.0.0.1"
  echo "CONDUCTOR_API_PORT=$PORT"
  [ -n "$OPENAI_KEY" ] && echo "OPENAI_API_KEY=$OPENAI_KEY"
} > "$ENV_FILE"
umask 022
chmod 600 "$ENV_FILE"
ok "wrote $ENV_FILE"

# 6) service --------------------------------------------------------------
# launchd has no EnvironmentFile equivalent, so both platforms go through one
# launcher that sources the env file. That keeps the token in exactly one place
# with one set of permissions, rather than duplicated into a plist.
cat > "$INSTALL_DIR/run-backend.sh" <<'LAUNCH'
#!/usr/bin/env bash
set -euo pipefail
# launchd hands an agent a minimal PATH that excludes Homebrew and ~/.local/bin, so the
# backend cannot find tmux or claude and every pane call fails with ENOENT. Rebuild a
# usable PATH here rather than in the plist, so systemd gets the same treatment.
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/v26.7.0/bin:$PATH"
export PATH
ENV_FILE="${TMUXOR_ENV:-$HOME/.config/tmux-conductor.env}"
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
exec "${TMUXOR_PYTHON:-__PY_BIN__}" "$(dirname "$0")/conductor_api.py"
LAUNCH
sed -i.bak "s|__PY_BIN__|$PY_BIN|" "$INSTALL_DIR/run-backend.sh" && rm -f "$INSTALL_DIR/run-backend.sh.bak"
chmod 755 "$INSTALL_DIR/run-backend.sh"

case "$SERVICE_KIND" in
launchd)
  mkdir -p "$LAUNCH_DIR"
  PLIST_PATH="$LAUNCH_DIR/$LABEL.plist"
  [ "$DRY" = 1 ] && PLIST_PATH="$INSTALL_DIR/$LABEL.plist.dryrun"
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$INSTALL_DIR/run-backend.sh</string></array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$INSTALL_DIR/backend.log</string>
  <key>StandardErrorPath</key><string>$INSTALL_DIR/backend.err.log</string>
</dict>
</plist>
PLIST
  ok "wrote $PLIST_PATH"
  run launchctl unload "$LAUNCH_DIR/$LABEL.plist" 2>/dev/null || true
  run launchctl load -w "$LAUNCH_DIR/$LABEL.plist"
  [ "$DRY" = 1 ] || ok "loaded $LABEL (survives logout and reboot)"
  ;;
systemd)
  mkdir -p "$UNIT_DIR"
  UNIT_PATH="$UNIT_DIR/tmux-conductor.service"
  [ "$DRY" = 1 ] && UNIT_PATH="$INSTALL_DIR/tmux-conductor.service.dryrun"
  cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=TMUXor backend (conductor-api)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/run-backend.sh
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT
  ok "wrote $UNIT_PATH"
  run systemctl --user daemon-reload
  run systemctl --user enable --now tmux-conductor.service
  [ "$DRY" = 1 ] || warn "to keep it running after logout: sudo loginctl enable-linger $USER"
  ;;
*)
  warn "unsupported platform $(uname -s) — backend installed but NOT supervised."
  warn "run it yourself: $INSTALL_DIR/run-backend.sh"
  ;;
esac

# 7) expose on the tailnet ---------------------------------------------------
# --operator needs sudo and only applies to a root daemon; skip it for a userspace one.
if [ -z "${TMUXOR_TS_SOCKET:-}" ]; then
  run sudo tailscale set --operator="$USER"   # one-time, so 'tailscale serve' needs no sudo
fi
if [ "$DRY" = 1 ]; then echo "  [dry-run] tailscale serve --bg $PORT"; else ts serve --bg "$PORT"; fi

# 8) summary + paste-config --------------------------------------------------
DNS=$(ts status --json 2>/dev/null | "$PY_BIN" -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
URL="https://${DNS:-<your-tailscale-host>.ts.net}"
BLOB="tmuxor:$("$PY_BIN" -c 'import base64,json,sys;print(base64.urlsafe_b64encode(json.dumps({"base":sys.argv[1],"token":sys.argv[2]}).encode()).decode())' "$URL" "$TOKEN")"

# The config blob CONTAINS the token. Upstream prints it and renders it as a QR in the
# terminal; on a machine where agents can read any pane's scrollback that publishes the
# secret to everything on the box. Write it to 0600 files and print only the paths.
OUT_DIR="${TMUXOR_OUT:-$HOME/.config/tmuxor}"
mkdir -p "$OUT_DIR"; chmod 700 "$OUT_DIR"
umask 177
printf '%s\n' "$BLOB" > "$OUT_DIR/setup-config.txt"
umask 022
chmod 600 "$OUT_DIR/setup-config.txt"

echo
ok "TMUXor backend is up."
c  "Backend URL   : $URL"
c  "Config blob   : $OUT_DIR/setup-config.txt  (contains the token — do not cat it into a shared pane)"
if command -v qrencode >/dev/null; then
  qrencode -o "$OUT_DIR/setup-qr.png" "$BLOB" 2>/dev/null && chmod 600 "$OUT_DIR/setup-qr.png" \
    && c "QR image      : $OUT_DIR/setup-qr.png  (open it, scan with the phone)"
fi
c  "On your phone: open TMUXor → Setup → 'Paste config'."
echo
[ -z "$DNS" ] && warn "couldn't read your Tailscale domain — run 'tailscale status' and use your https://<host>.ts.net URL."
