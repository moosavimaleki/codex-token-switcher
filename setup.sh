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

SERVICE_FILE="$SYSTEMD_USER_DIR/codex-manager-maintain.service"
TIMER_FILE="$SYSTEMD_USER_DIR/codex-manager-maintain.timer"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Codex Manager maintenance

[Service]
Type=oneshot
ExecStart=$BIN_DIR/codex-manager maintain --quiet
EOF

cat > "$TIMER_FILE" <<EOF
[Unit]
Description=Run Codex Manager maintenance every 6 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
RandomizedDelaySec=10min
Persistent=true
Unit=codex-manager-maintain.service

[Install]
WantedBy=timers.target
EOF

TIMER_STATUS="not installed"
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now codex-manager-maintain.timer
  TIMER_STATUS="systemd user timer: codex-manager-maintain.timer"
elif command -v crontab >/dev/null 2>&1; then
  CRON_LINE="17 */6 * * * $BIN_DIR/codex-manager maintain --quiet >/dev/null 2>&1"
  TMP_CRON="$(mktemp)"
  crontab -l 2>/dev/null | grep -vF "$BIN_DIR/codex-manager maintain --quiet" > "$TMP_CRON" || true
  echo "$CRON_LINE" >> "$TMP_CRON"
  crontab "$TMP_CRON"
  rm -f "$TMP_CRON"
  TIMER_STATUS="crontab: $CRON_LINE"
else
  TIMER_STATUS="not installed; neither systemd user timers nor crontab are available"
fi

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
