# codex-manager

A small local manager for multiple Codex ChatGPT `auth.json` files.

It keeps account files under `~/.codex-manager/accounts`, checks one selected account out to
`~/.codex/auth.json`, and runs background maintenance that:

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
`codex-manager doctor` prints the account list, token health, file permissions, systemd timer/service
status, journal lines, crontab fallback status, and manager log tail in one place.

## Setup

```bash
./setup.sh
```

The setup script installs `codex-manager` to `~/.local/bin` and schedules maintenance every
6 hours using a user systemd timer when available, otherwise a crontab entry.

## Files

- active Codex auth: `~/.codex/auth.json`
- manager store: `~/.codex-manager/accounts/*.json`
- manager state: `~/.codex-manager/state.json`
- maintenance log: `~/.codex-manager/log.txt`

All auth/state files are written with `0600` permissions and manager directories with `0700`.
