from __future__ import annotations

import shutil

from ..codex.limits import format_rate_limits_summary
from ..config import ensure_config, redact_url
from ..constants import DEFAULT_LAST_REFRESH_MAX_AGE, DEFAULT_REFRESH_MARGIN
from ..paths import Paths, account_path, ensure_dirs, list_accounts, status_path
from ..storage import load_state, read_json, tail_lines
from ..system import run_command
from ..terminal import bad, badge, dim, info, ok, section, style, warn
from ..time_utils import human_delta
from ..views import colored_mode, print_accounts


def print_command_output(label: str, command: list[str], timeout: int = 5) -> None:
    code, output = run_command(command, timeout=timeout)
    status = ok("ok") if code == 0 else warn(f"exit={code}") if code is not None else bad("unavailable")
    print(f"{style(label, 'bold')}: {dim(' '.join(command))} [{status}]")
    print(output if output else dim("(no output)"))


def cmd_doctor(args) -> int:
    paths = Paths()
    ensure_dirs(paths)
    config = ensure_config(paths)
    state = load_state(paths)
    active = state.get("active")
    accounts = list_accounts(paths)

    section("Summary")
    selected = ok(active) if active else warn("(none)")
    print(f"{style('Manager home', 'bold'):<22} {paths.manager_home}")
    print(f"{style('Active auth', 'bold'):<22} {paths.codex_auth}")
    print(f"{style('Config', 'bold'):<22} {paths.config_file}")
    print(f"{style('Proxy', 'bold'):<22} {redact_url(config.get('proxy'))}")
    print(f"{style('Job interval', 'bold'):<22} {config['maintain_interval']} (+ random {config['randomized_delay']})")
    print(f"{style('Monitor interval', 'bold'):<22} {config['monitor_interval']}")
    print(f"{style('History retention', 'bold'):<22} {config['history_retention_days']} day(s)")
    print(f"{style('Gateway', 'bold'):<22} http://{config['gateway_listen']}/v1")
    print(f"{style('Selected account', 'bold'):<22} {selected}")
    print(f"{style('Accounts', 'bold'):<22} {len(accounts)}")
    print(f"{style('Refresh policy', 'bold'):<22} inactive tokens refresh when access token <= {warn(human_delta(DEFAULT_REFRESH_MARGIN))} or last_refresh >= {warn(human_delta(DEFAULT_LAST_REFRESH_MAX_AGE))}")
    if active is None and accounts:
        print(f"{warn('Action needed')}       run {info('codex-manager ls')} and press Enter on the active account")

    section("Accounts")
    print_accounts(paths)

    section("Account Status")
    if not accounts:
        print(dim("No account status files yet."))
    for name in accounts:
        sp = status_path(paths, name)
        if sp.exists():
            s = read_json(sp)
            state_name = str(s.get("state") or "unknown")
            limits = format_rate_limits_summary(s.get("rate_limits"))
            print(
                f"{style(name, 'bold'):<18} {badge(state_name, state_name):<24} "
                f"{s.get('message') or ''} {dim(limits)} {dim(s.get('last_checked_at') or '')}"
            )
        else:
            print(f"{style(name, 'bold'):<18} {warn('no status')}          no status file yet")

    section("Files")
    print(f"{colored_mode(paths.codex_auth, '600')}  {paths.codex_auth}")
    print(f"{colored_mode(paths.config_file, '600')}  {paths.config_file}")
    print(f"{colored_mode(paths.state_file, '600')}  {paths.state_file}")
    print(f"{colored_mode(paths.lock_file, '600')}  {paths.lock_file}")
    print(f"{colored_mode(paths.log_file, '600')}  {paths.log_file}")
    print(f"{colored_mode(paths.history_file, '600')}  {paths.history_file}")
    for name in accounts:
        path = account_path(paths, name)
        print(f"{colored_mode(path, '600')}  {path}")

    section("Scheduler")
    service_path = paths.home / ".config/systemd/user/codex-manager-maintain.service"
    timer_path = paths.home / ".config/systemd/user/codex-manager-maintain.timer"
    monitor_service_path = paths.home / ".config/systemd/user/codex-manager-check.service"
    monitor_timer_path = paths.home / ".config/systemd/user/codex-manager-check.timer"
    gateway_path = paths.home / ".config/systemd/user/codex-manager-gateway.service"
    print(f"{style('systemd service', 'bold'):<22} {colored_mode(service_path)}  {service_path}")
    print(f"{style('systemd timer', 'bold'):<22} {colored_mode(timer_path)}  {timer_path}")
    print(f"{style('monitor service', 'bold'):<22} {colored_mode(monitor_service_path)}  {monitor_service_path}")
    print(f"{style('monitor timer', 'bold'):<22} {colored_mode(monitor_timer_path)}  {monitor_timer_path}")
    print(f"{style('gateway service', 'bold'):<22} {colored_mode(gateway_path)}  {gateway_path}")
    if shutil.which("systemctl"):
        print_command_output("timer status", ["systemctl", "--user", "status", "codex-manager-maintain.timer", "--no-pager"], timeout=8)
        print_command_output("timer schedule", ["systemctl", "--user", "list-timers", "codex-manager-maintain.timer", "--no-pager"], timeout=8)
        print_command_output("service status", ["systemctl", "--user", "status", "codex-manager-maintain.service", "--no-pager"], timeout=8)
        print_command_output("service journal", ["journalctl", "--user", "-u", "codex-manager-maintain.service", "-n", str(args.journal_lines), "--no-pager"], timeout=8)
        print_command_output("monitor status", ["systemctl", "--user", "status", "codex-manager-check.timer", "--no-pager"], timeout=8)
        print_command_output("monitor schedule", ["systemctl", "--user", "list-timers", "codex-manager-check.timer", "--no-pager"], timeout=8)
        print_command_output("monitor journal", ["journalctl", "--user", "-u", "codex-manager-check.service", "-n", str(args.journal_lines), "--no-pager"], timeout=8)
        print_command_output("gateway status", ["systemctl", "--user", "status", "codex-manager-gateway.service", "--no-pager"], timeout=8)
    else:
        print(warn("systemctl not found"))

    section("Crontab Fallback")
    if shutil.which("crontab"):
        print_command_output("crontab", ["sh", "-lc", "crontab -l 2>/dev/null | grep codex-manager || true"])
    else:
        print(warn("crontab not found"))

    section("Manager Log")
    lines = tail_lines(paths.log_file, args.log_lines)
    if lines:
        for line in lines:
            print(dim(line[:32]) + line[32:] if len(line) > 32 else line)
    else:
        print(dim("(no manager log entries yet; normal if no refresh/error happened)"))
    return 0
