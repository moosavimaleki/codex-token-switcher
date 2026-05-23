from __future__ import annotations

import json

from .scheduler import apply_scheduler
from ..config import ensure_config, redact_url, save_config
from ..errors import ManagerError
from ..paths import Paths
from ..terminal import dim, section, style


def print_config(paths: Paths, config: dict) -> None:
    printable = dict(config)
    if printable.get("proxy"):
        printable["proxy"] = redact_url(printable.get("proxy"))
    print(f"{style('Config file', 'bold'):<22} {paths.config_file}")
    print(json.dumps(printable, indent=2, ensure_ascii=False))


def prompt_config_value(label: str, current: str) -> str:
    try:
        return input(f"{label} [{current}]: ").strip()
    except EOFError:
        return ""


def cmd_config_interactive(paths: Paths) -> int:
    config = ensure_config(paths)
    section("Config")
    print_config(paths, config)
    print("")
    print(dim("Press Enter to keep a value. Use `none` to disable proxy."))

    updates = {}
    proxy = prompt_config_value("Proxy", redact_url(config.get("proxy")))
    interval = prompt_config_value("Maintenance interval", str(config["maintain_interval"]))
    monitor_interval = prompt_config_value("Monitor interval", str(config["monitor_interval"]))
    randomized_delay = prompt_config_value("Randomized delay", str(config["randomized_delay"]))
    retention_days = prompt_config_value("History retention days", str(config["history_retention_days"]))

    if proxy:
        updates["proxy"] = proxy
    if interval:
        updates["maintain_interval"] = interval
    if monitor_interval:
        updates["monitor_interval"] = monitor_interval
    if randomized_delay:
        updates["randomized_delay"] = randomized_delay
    if retention_days:
        updates["history_retention_days"] = int(retention_days)

    if updates:
        config = save_config(paths, updates)
        print("")
        print_config(paths, config)
    else:
        print(dim("No config changes."))

    apply_answer = prompt_config_value("Apply scheduler now? Y/n", "Y").lower()
    if apply_answer in {"", "y", "yes"}:
        status = apply_scheduler(paths)
        print(f"scheduler applied: {status}")
    else:
        print(dim("Scheduler left unchanged."))
    return 0


def cmd_config(args) -> int:
    paths = Paths()
    if getattr(args, "config_cmd", None) is None:
        return cmd_config_interactive(paths)

    if args.config_cmd == "show":
        print_config(paths, ensure_config(paths))
        return 0

    updates = {}
    if args.proxy is not None:
        updates["proxy"] = args.proxy
    if args.interval is not None:
        updates["maintain_interval"] = args.interval
    if args.monitor_interval is not None:
        updates["monitor_interval"] = args.monitor_interval
    if args.randomized_delay is not None:
        updates["randomized_delay"] = args.randomized_delay
    if args.history_retention_days is not None:
        updates["history_retention_days"] = args.history_retention_days
    if not updates:
        raise ManagerError("provide --proxy, --interval, --monitor-interval, --randomized-delay, or --history-retention-days")

    config = save_config(paths, updates)
    print_config(paths, config)
    if args.apply_scheduler:
        status = apply_scheduler(paths, args.bin)
        print(f"scheduler applied: {status}")
    elif "maintain_interval" in updates or "monitor_interval" in updates or "randomized_delay" in updates:
        print(dim("Run `codex-manager scheduler apply` to update the installed timer."))
    return 0
