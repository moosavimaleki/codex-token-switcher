#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
INSTALL_DIR="$HOME/.local/share/codex-manager"
MANAGER_HOME="${CODEX_MANAGER_HOME:-$HOME/.codex-manager}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
PYTHON_BIN="$(command -v python3)"

mkdir -p "$BIN_DIR" "$INSTALL_DIR" "$MANAGER_HOME/accounts" "$MANAGER_HOME/status" "$SYSTEMD_USER_DIR"
chmod 700 "$MANAGER_HOME" "$MANAGER_HOME/accounts" "$MANAGER_HOME/status"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

if "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  PIP_CMD=("$PYTHON_BIN" -m pip)
elif command -v pip3 >/dev/null 2>&1; then
  PIP_CMD=("pip3" "--python" "$PYTHON_BIN")
else
  echo "No pip available for $PYTHON_BIN. Install pip3 first." >&2
  exit 1
fi

"${PIP_CMD[@]}" install --user --upgrade --break-system-packages -r "$PROJECT_DIR/requirements.txt"

rm -rf "$INSTALL_DIR/codex_manager"
cp -R "$PROJECT_DIR/codex_manager" "$INSTALL_DIR/codex_manager"
install -m 0755 "$PROJECT_DIR/codex-manager" "$INSTALL_DIR/codex-manager"
install -m 0644 "$PROJECT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

cat > "$BIN_DIR/codex-manager" <<EOF
#!/usr/bin/env bash
PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH:-}" exec "$PYTHON_BIN" "$INSTALL_DIR/codex-manager" "\$@"
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
