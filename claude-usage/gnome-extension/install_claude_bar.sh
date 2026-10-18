#!/usr/bin/env bash
# Install (or with --uninstall, remove) the Claude Usage GNOME Shell extension
# for the current user.
set -euo pipefail

UUID=claude-usage@davidschledewitz.github.io
SRC="$(cd "$(dirname "$0")" && pwd)/$UUID"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"

# Add or remove our UUID in org.gnome.shell enabled-extensions.
set_enabled() {
    local current
    current=$(gsettings get org.gnome.shell enabled-extensions)
    gsettings set org.gnome.shell enabled-extensions "$(python3 - "$current" "$UUID" "$1" <<'EOF'
import ast, sys
current, uuid, mode = sys.argv[1:]
items = ast.literal_eval(current.removeprefix("@as "))
items = [i for i in items if i != uuid] + ([uuid] if mode == "on" else [])
print(repr(items))
EOF
)"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    gnome-extensions disable "$UUID" 2>/dev/null || set_enabled off
    rm -rf "$DEST"
    echo "Removed. Log out and back in to clear it from the top bar."
    exit 0
fi

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
cp -r "$SRC" "$DEST"
echo "Installed to $DEST"

if gnome-extensions info "$UUID" &>/dev/null; then
    # Already known to the running shell (re-install/update).
    gnome-extensions enable "$UUID"
    echo "Enabled. To load the updated code, log out and back in."
else
    # GNOME on Wayland only discovers new extensions at login.
    set_enabled on
    echo "Marked as enabled. Log out and back in, and it appears in the top bar."
fi
