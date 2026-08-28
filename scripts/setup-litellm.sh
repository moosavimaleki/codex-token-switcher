#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
LITELLM_HOME="${CODEX_MANAGER_LITELLM_HOME:-$HOME/.config/codex-manager/litellm}"
LITELLM_VENV="$LITELLM_HOME/venv"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

mkdir -p "$LITELLM_HOME"
chmod 700 "$LITELLM_HOME"
if [[ ! -x "$LITELLM_VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$LITELLM_VENV"
fi
"$LITELLM_VENV/bin/python" -m pip install --upgrade 'litellm[proxy]'
install -m 0600 "$PROJECT_DIR/litellm/.env.example" "$LITELLM_HOME/.env.example"
install -m 0644 "$PROJECT_DIR/litellm/config.yaml" "$LITELLM_HOME/config.yaml"
install -m 0644 "$PROJECT_DIR/litellm/gemini_vertex_adapter.py" "$LITELLM_HOME/gemini_vertex_adapter.py"
install -m 0644 "$PROJECT_DIR/litellm/openai_passthrough_adapter.py" "$LITELLM_HOME/openai_passthrough_adapter.py"

if [[ ! -e "$LITELLM_HOME/.env" ]]; then
  install -m 0600 "$PROJECT_DIR/litellm/.env.example" "$LITELLM_HOME/.env"
  echo "Created $LITELLM_HOME/.env; edit it with your OpenRouter keys, then start the service." >&2
fi

SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/codex-manager-litellm.service" <<EOF
[Unit]
Description=LiteLLM model gateway for codex-manager
Wants=codex-manager-gemini-adapter.service codex-manager-chatgpt-adapter.service
After=network-online.target codex-manager-gemini-adapter.service codex-manager-chatgpt-adapter.service

[Service]
Type=simple
WorkingDirectory=$LITELLM_HOME
EnvironmentFile=$LITELLM_HOME/.env
ExecStart=$LITELLM_VENV/bin/litellm --config $LITELLM_HOME/config.yaml --host 127.0.0.1 --port 4000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/codex-manager-gemini-adapter.service" <<EOF
[Unit]
Description=OpenAI adapter for the local Gemini Vertex lab
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$LITELLM_HOME
EnvironmentFile=$LITELLM_HOME/.env
ExecStart=$LITELLM_VENV/bin/uvicorn gemini_vertex_adapter:app --host 127.0.0.1 --port 4010
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/codex-manager-chatgpt-adapter.service" <<EOF
[Unit]
Description=Header-clean OpenAI passthrough for the local ChatGPT lab
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$LITELLM_HOME
EnvironmentFile=$LITELLM_HOME/.env
ExecStart=$LITELLM_VENV/bin/uvicorn openai_passthrough_adapter:app --host 127.0.0.1 --port 4011
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now codex-manager-gemini-adapter.service
  systemctl --user enable --now codex-manager-chatgpt-adapter.service
  systemctl --user enable --now codex-manager-litellm.service
  echo "LiteLLM is running at http://127.0.0.1:4000"
else
  echo "systemd user service created at $SYSTEMD_DIR/codex-manager-litellm.service"
  echo "Start manually with: $LITELLM_VENV/bin/litellm --config $LITELLM_HOME/config.yaml --host 127.0.0.1 --port 4000"
fi

echo "Edit credentials: $LITELLM_HOME/.env"
echo "Client model name: openrouter-model"
