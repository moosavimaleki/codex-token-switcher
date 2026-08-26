# codex-manager

A local token switcher and monitoring workspace for multiple Codex ChatGPT `auth.json` accounts.

`codex-manager` stores account auth files under `~/.codex-manager/accounts`, checks one selected
account out to `~/.codex/auth.json`, and runs background maintenance that:

- syncs the active `~/.codex/auth.json` back into its stored account file because Codex may refresh it;
- only syncs that active auth back when the current `auth.json` still matches the stored account identity;
- treats `~/.codex-manager/accounts/*.json` as the source of truth and only promotes live `~/.codex/auth.json` when it is both the same account and provably newer;
- refreshes inactive accounts only when their access token is near expiry or their `last_refresh` is old;
- sends token refresh requests through the configured proxy when one is set;
- fetches Codex usage limits during `check`/maintenance, caches them for `ls`, and appends them to a local history log for charting;
- never auto-switches accounts.

## Commands

```bash
codex-manager add <name> <path-to-working-auth.json>
codex-manager ls
codex-manager check
codex-manager sessions --dry-run
codex-manager chart --hours 24
codex-manager compact <session_id>
codex-manager gateway
codex-manager config
codex-manager doctor
codex-manager maintain --quiet
scripts/setup-litellm.sh
```

`codex-manager ls` opens a Textual dashboard when run in an interactive terminal. The dashboard uses
a Dracula palette and gives you one workspace for account browsing, activation, deletion, imports,
and history charts. The Accounts tab also includes a `Check Now` button that runs the same account
check flow as `codex-manager check`, refreshing cached limits and history samples in place. `ls`
shows the cached weekly limit from the latest `check`/maintenance run until you trigger
that refresh.

When `codex-manager add` imports the live `~/.codex/auth.json`, the manager treats that imported
account as active if the live auth no longer matches the previously recorded active account.
Running `codex-manager add` with no positional arguments opens the Textual Add tab instead. From
there, `Start Device Login` requests a ChatGPT device code through Codex's app-server, shows the
verification URL and one-time code, waits for Codex to receive tokens, and imports the resulting
temporary `auth.json` without overwriting the currently active live Codex auth. Manual `auth.json`
import remains available as a fallback.

`codex-manager check` runs account maintenance immediately for every account, refreshes inactive
accounts whose access token is near expiry, updates cached Codex usage limits, and appends samples
to `~/.codex-manager/history/limits.jsonl`. The live active
Codex account is not refreshed by default because Codex itself may be rotating that token; pass
`--refresh-active` only when you intentionally want the manager to refresh the active account.
Use `--force-refresh` when you want to force refresh requests for inactive accounts.

`codex-manager sessions` reads each local Chrome profile through a temporary, read-only copy of its
cookie database. It reports and retains at most one Linux session whose application name is exactly
`Codex` per profile. When there are extras, Windows Codex sessions are revoked first; then the oldest
remaining Codex login is kept and newer sessions are revoked. It never reports, stores, or revokes web,
mobile, or non-Codex sessions. Use `--dry-run` to inspect the Codex count without
revoking anything. Extracted cookies and response `Set-Cookie` values exist only in memory.

`codex-manager chart` opens the Textual chart tab directly. Use `--hours N` or `--days N` and
`--offset local|UTC|+03:30|-07:00` to control the history window and timezone labeling.

`codex-manager compact <session_id>` prints the same account health/limit context as `ls`, asks
which account should compact the session, resumes that session inside Codex's own `app-server`,
then runs the compaction API with that account and the configured proxy. You can also pass a
rollout `.jsonl` path instead of the raw session id. When multiple `codex` binaries are installed,
the manager auto-selects the newest version it can find; use `--codex-bin` to override it.

`codex-manager config` opens a small interactive wizard for proxy, maintenance interval, Chrome
session monitoring, randomized delay, and scheduler apply. Script-friendly commands such as `codex-manager config show` and
`codex-manager config set --proxy http://127.0.0.1:7890 --interval 6h --apply-scheduler` still work.

`codex-manager doctor` prints a colorized status report with:

- account/token health;
- config path, proxy, and scheduler interval;
- selected active account;
- file paths and permissions;
- systemd timer/service status;
- journal lines;
- crontab fallback status;
- manager log tail.

`codex-manager gateway` runs a local OpenAI-compatible gateway. It exposes `POST /v1/responses`,
`GET /v1/models`, and `GET /health`, selects among stored accounts using the latest available
5-hour, weekly, or monthly quota windows, and forwards requests directly to the Codex backend
with that account's access token. The gateway uses a local bearer key from `gateway_api_key` and
does not read or modify the active `~/.codex/auth.json`. A conversation can be pinned to an
account with the `X-Conversation-ID` header.

## Setup

```bash
./setup.sh
```

The setup script installs:

- launcher: `~/.local/bin/codex-manager`
- package files: `~/.local/share/codex-manager/codex_manager/`
- pinned UI/runtime dependencies from `requirements.txt` into the user site for the same Python interpreter that runs the launcher
- user data: `~/.codex-manager/`

The installer intentionally avoids virtualenvs. On distributions such as Debian/Ubuntu it uses
`pip --user --break-system-packages` for the selected interpreter so Textual dependencies end up in
the same Python environment that launches `codex-manager`.

### LiteLLM with multiple OpenRouter accounts

The optional `scripts/setup-litellm.sh` installs LiteLLM and creates a user service on
`127.0.0.1:4000`. It uses three OpenRouter API keys as separate deployments of the same model;
LiteLLM load-balances requests and retries another deployment when one fails.

```bash
scripts/setup-litellm.sh
$EDITOR ~/.config/codex-manager/litellm/.env
systemctl --user restart codex-manager-litellm.service
curl http://127.0.0.1:4000/health/liveliness
```

The local proxy is bound to `127.0.0.1` and does not require a client token. Any OpenRouter model
can be requested by its normal OpenRouter ID, for example
`nvidia/nemotron-3.5-lightning:free` (the optional `openrouter/` prefix also works). The model and
routing settings live in
`litellm/config.yaml`.
The OpenRouter keys must be ordinary API keys, not management keys.

Maintenance is scheduled every 6 hours using a user systemd timer when available, otherwise a
crontab entry. A second 5-minute monitor timer runs `codex-manager check --quiet` so limit history
stays fresh for charting. A third timer runs `codex-manager sessions --quiet` every 10 minutes by
default. Chrome session monitoring can be disabled in config. The intervals are read from
`~/.codex-manager/config.json`; after changing them, run:

```bash
codex-manager config
```

## Config

Default config:

```json
{
  "proxy": null,
  "maintain_interval": "6h",
  "monitor_interval": "5min",
  "session_monitor_enabled": true,
  "session_monitor_interval": "10min",
  "chrome_root": null,
  "randomized_delay": "10min",
  "history_retention_days": 90,
  "gateway_listen": "127.0.0.1:8787",
  "gateway_api_key": "change-me",
  "gateway_upstream": "https://chatgpt.com/backend-api/codex"
}
```

Supported proxy URLs are `http://` and `https://`. Duration values accept forms like `30m`,
`6h`, and `1d`. The interactive `codex-manager config` command can update these values and apply
the scheduler in one pass.

## Files

- active Codex auth: `~/.codex/auth.json`
- manager config: `~/.codex-manager/config.json`
- manager store: `~/.codex-manager/accounts/*.json`
- manager history: `~/.codex-manager/history/limits.jsonl`
- manager state: `~/.codex-manager/state.json`
- maintenance log: `~/.codex-manager/log.txt`

Auth/state files are written with `0600` permissions and manager directories with `0700`.
Whenever a file under `~/.codex-manager/` is overwritten, the previous five versions are kept as
rotating backups named like `account.json.BAK1` through `account.json.BAK5`.

## Development Layout

```text
codex-manager          # thin executable launcher
codex_manager/         # Python package
  auth.py              # auth.json parsing and token refresh
  cli.py               # argparse entrypoint
  config.py            # manager config, durations, scheduler helpers
  constants.py         # refresh endpoint and policy constants
  codex/               # Codex app-server and backend API adapters
    app_server.py      # minimal Codex app-server JSON-RPC client
    limits.py          # Codex usage limit fetching and display shaping
  commands/            # CLI command handlers
    accounts.py        # add/list/activate/delete account commands
    compact.py         # session compaction account selection and auth checkout
    config.py          # config CLI command and interactive wizard
    doctor.py          # diagnostic report command
    maintenance.py     # check/maintain account refresh and limits cache
    scheduler.py       # systemd/crontab scheduler install/update
  errors.py            # user-facing errors
  paths.py             # filesystem paths and account names
  storage.py           # atomic writes, lock, state, logs
  system.py            # subprocess helpers
  terminal.py          # colors and badges
  time_utils.py        # datetime helpers
  views.py             # account table rendering
setup.sh               # installer + scheduler setup
```
