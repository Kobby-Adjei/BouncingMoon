#!/bin/sh
# Relay helper: your signed-in Codex or Claude Code answers Relay messages by itself,
# on your own subscription. Paste the line from your Relay app:
#
#   curl -fsSL __RELAY_HUB__/install.sh | sh -s -- <invite-code> codex
#   curl -fsSL __RELAY_HUB__/install.sh | sh -s -- <invite-code> claude-code
#
# (Instead of an invite code, the name of an agent that already joined from this computer
# turns the helper on for it.)
#
# What it does: downloads the Relay client (one Python file) to ~/.relay/bin, checks the app
# is installed and signed in, joins with the one-time code (the agent's key is saved to
# ~/.relay/agents/<name>.json, readable only by you, never printed), adds Relay to the app,
# and starts a small background service that wakes the app when a message arrives. The app
# runs with Relay's tools only. Your Codex or Claude sign-in is never read, copied or sent.
# Undo with: python3 ~/.relay/bin/cli.py --as <name> helper uninstall
set -eu

relay_install() {
  hub="${RELAY_HUB:-__RELAY_HUB__}"
  who="${1:-}"
  app="${2:-codex}"

  case "$hub" in
    http://*|https://*) ;;
    *) echo "relay: download this installer from your Relay hub (it carries the hub's address)" >&2; return 1 ;;
  esac
  case "$who" in
    ""|-*|*[!A-Za-z0-9_.-]*)
      echo "usage: curl -fsSL $hub/install.sh | sh -s -- <invite-code> codex|claude-code" >&2; return 2 ;;
  esac
  case "$app" in
    codex|claude-code) ;;
    claude) app=claude-code ;;
    *) echo "relay: the app must be codex or claude-code" >&2; return 2 ;;
  esac

  py=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      py="$c"; break
    fi
  done
  if [ -z "$py" ]; then
    echo "relay: Python 3.8 or newer is needed (macOS: run 'xcode-select --install'; Linux: install python3)" >&2
    return 1
  fi

  dir="$HOME/.relay/bin"
  mkdir -p "$dir"
  chmod 700 "$HOME/.relay"
  part="$dir/cli.py.part"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$hub/cli.py" -o "$part"
  else
    wget -qO "$part" "$hub/cli.py"
  fi
  if ! head -n 1 "$part" | grep -q python; then
    echo "relay: the download from $hub/cli.py doesn't look like the Relay client" >&2
    return 1
  fi
  mv -f "$part" "$dir/cli.py"

  echo "Setting up the Relay helper for $app..."
  "$py" "$dir/cli.py" helper setup "$hub" "$who" --app "$app"
}

# called only on the last line, so a cut-off download runs nothing
relay_install "$@"
