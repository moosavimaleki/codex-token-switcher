#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
INSTALL_DIR="$HOME/.local/share/codex-manager"
MANAGER_HOME="${CODEX_MANAGER_HOME:-$HOME/.codex-manager}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

mkdir -p "$BIN_DIR" "$INSTALL_DIR" "$MANAGER_HOME/accounts" "$MANAGER_HOME/status" "$SYSTEMD_USER_DIR"
chmod 700 "$MANAGER_HOME" "$MANAGER_HOME/accounts" "$MANAGER_HOME/status"

rm -rf "$INSTALL_DIR/codex_manager"
cp -R "$PROJECT_DIR/codex_manager" "$INSTALL_DIR/codex_manager"
install -m 0755 "$PROJECT_DIR/codex-manager" "$INSTALL_DIR/codex-manager"

cat > "$BIN_DIR/codex-manager" <<EOF
#!/usr/bin/env bash
PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH:-}" exec python3 "$INSTALL_DIR/codex-manager" "\$@"
EOF
chmod 0755 "$BIN_DIR/codex-manager"

TIMER_STATUS="$("$BIN_DIR/codex-manager" scheduler apply --bin "$BIN_DIR/codex-manager" --quiet)"

cat <<EOF
codex-manager installed.

Binary: $BIN_DIR/codex-manager
Install dir: $INSTALL_DIR
Timer:  $TIMER_STATUS
Store:  $MANAGER_HOME

If ~/.local/bin is not in PATH, add this to your shell config:
  export PATH="\$HOME/.local/bin:\$PATH"

Try:
  codex-manager add main ~/.codex/auth.json
  codex-manager ls
  systemctl --user list-timers codex-manager-maintain.timer  # if systemd was used
EOF
