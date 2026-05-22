# codex-manager

A local token switcher for multiple Codex ChatGPT `auth.json` accounts.

`codex-manager` stores account auth files under `~/.codex-manager/accounts`, checks one selected
account out to `~/.codex/auth.json`, and runs background maintenance that:

- syncs the active `~/.codex/auth.json` back into its stored account file because Codex may refresh it;
- only syncs that active auth back when the current `auth.json` still matches the stored account identity;
- treats `~/.codex-manager/accounts/*.json` as the source of truth and only promotes live `~/.codex/auth.json` when it is both the same account and provably newer;
- refreshes inactive accounts only when their access token is near expiry or their `last_refresh` is old;
- sends token refresh requests through the configured proxy when one is set;
- fetches Codex usage limits during `check`/maintenance and caches them for `ls`;
- never auto-switches accounts.

## Commands

```bash
codex-manager add <name> <path-to-working-auth.json>
codex-manager ls
codex-manager check
codex-manager compact <session_id>
codex-manager config
codex-manager doctor
codex-manager maintain --quiet
```

`codex-manager ls` is interactive when run in a terminal: use up/down and Enter to choose the
active account, or press `d` to delete the selected inactive account. `ls` shows cached 5-hour and
weekly limits from the latest `check`/maintenance run and does not make network requests.

When `codex-manager add` imports the live `~/.codex/auth.json`, the manager treats that imported
account as active if the live auth no longer matches the previously recorded active account.

`codex-manager check` runs account maintenance immediately for every account, including the active
one, refreshes any account whose access token is near expiry, and updates cached Codex usage
limits. Use `--force-refresh` when you want to force a refresh request for every account.

`codex-manager compact <session_id>` prints the same account health/limit context as `ls`, asks
which account should compact the session, resumes that session inside Codex's own `app-server`,
then runs the compaction API with that account and the configured proxy. You can also pass a
rollout `.jsonl` path instead of the raw session id. When multiple `codex` binaries are installed,
the manager auto-selects the newest version it can find; use `--codex-bin` to override it.

`codex-manager config` opens a small interactive wizard for proxy, maintenance interval, randomized
delay, and scheduler apply. Script-friendly commands such as `codex-manager config show` and
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

## Setup

```bash
./setup.sh
```

The setup script installs:

- launcher: `~/.local/bin/codex-manager`
- package files: `~/.local/share/codex-manager/codex_manager/`
- user data: `~/.codex-manager/`

Maintenance is scheduled every 6 hours using a user systemd timer when available, otherwise a crontab entry.
The interval is read from `~/.codex-manager/config.json`; after changing it, run:

```bash
codex-manager config
```

## Config

Default config:

```json
{
  "proxy": null,
  "maintain_interval": "6h",
  "randomized_delay": "10min"
}
```

Supported proxy URLs are `http://` and `https://`. Duration values accept forms like `30m`,
`6h`, and `1d`. The interactive `codex-manager config` command can update these values and apply
the scheduler in one pass.

## Files

- active Codex auth: `~/.codex/auth.json`
- manager config: `~/.codex-manager/config.json`
- manager store: `~/.codex-manager/accounts/*.json`
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
