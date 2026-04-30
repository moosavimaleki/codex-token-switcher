# codex-manager

A local token switcher for multiple Codex ChatGPT `auth.json` accounts.

`codex-manager` stores account auth files under `~/.codex-manager/accounts`, checks one selected
account out to `~/.codex/auth.json`, and runs background maintenance that:

- syncs the active `~/.codex/auth.json` back into its stored account file because Codex may refresh it;
- refreshes inactive accounts only when their access token is near expiry or their `last_refresh` is old;
- never auto-switches accounts.

## Commands

```bash
codex-manager add <name> <path-to-working-auth.json>
codex-manager ls
codex-manager doctor
codex-manager maintain --quiet
```

`codex-manager ls` is interactive when run in a terminal: use up/down and Enter to choose the active account.

`codex-manager doctor` prints a colorized status report with:

- account/token health;
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

## Files

- active Codex auth: `~/.codex/auth.json`
- manager store: `~/.codex-manager/accounts/*.json`
- manager state: `~/.codex-manager/state.json`
- maintenance log: `~/.codex-manager/log.txt`

Auth/state files are written with `0600` permissions and manager directories with `0700`.

## Development Layout

```text
codex-manager          # thin executable launcher
codex_manager/         # Python package
  auth.py              # auth.json parsing and token refresh
  cli.py               # argparse entrypoint
  commands.py          # add/list/maintain/doctor commands
  constants.py         # refresh endpoint and policy constants
  errors.py            # user-facing errors
  paths.py             # filesystem paths and account names
  storage.py           # atomic writes, lock, state, logs
  system.py            # subprocess helpers
  terminal.py          # colors and badges
  time_utils.py        # datetime helpers
  views.py             # account table rendering
setup.sh               # installer + scheduler setup
```
