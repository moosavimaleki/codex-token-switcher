from __future__ import annotations

import json

from .scheduler import apply_scheduler
from ..config import ensure_config, redact_url, reset_config, save_config
from ..errors import ManagerError
from ..paths import Paths
from ..terminal import dim, section, style


def print_config(paths: Paths, config: dict) -> None:
    printable = dict(config)
    if printable.get("proxy"):
        printable["proxy"] = redact_url(printable.get("proxy"))
    if printable.get("gateway_api_key"):
        printable["gateway_api_key"] = "*** configured ***"
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
    session_monitor_enabled = prompt_config_value(
        "Monitor Chrome Codex sessions? y/N",
        "Y" if config["session_monitor_enabled"] else "N",
    )
    session_monitor_interval = prompt_config_value(
        "Chrome session monitor interval",
        str(config["session_monitor_interval"]),
    )
    chrome_root = prompt_config_value("Chrome profile directory", str(config.get("chrome_root") or "auto"))
    randomized_delay = prompt_config_value("Randomized delay", str(config["randomized_delay"]))
    retention_days = prompt_config_value("History retention days", str(config["history_retention_days"]))
    gateway_listen = prompt_config_value("Gateway listen address", str(config["gateway_listen"]))
    gateway_api_key = prompt_config_value("Gateway API key", "configured")

    if proxy:
        updates["proxy"] = proxy
    if interval:
        updates["maintain_interval"] = interval
    if monitor_interval:
        updates["monitor_interval"] = monitor_interval
    if session_monitor_enabled:
        updates["session_monitor_enabled"] = session_monitor_enabled.lower() in {"y", "yes", "true", "1", "on"}
    if session_monitor_interval:
        updates["session_monitor_interval"] = session_monitor_interval
    if chrome_root:
        updates["chrome_root"] = None if chrome_root.lower() == "auto" else chrome_root
    if randomized_delay:
        updates["randomized_delay"] = randomized_delay
    if retention_days:
        updates["history_retention_days"] = int(retention_days)
    if gateway_listen:
        updates["gateway_listen"] = gateway_listen
    if gateway_api_key and gateway_api_key.lower() != "configured":
        updates["gateway_api_key"] = gateway_api_key

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

    if args.config_cmd == "reset":
        print_config(paths, reset_config(paths))
        return 0

    updates = {}
    if args.proxy is not None:
        updates["proxy"] = args.proxy
    if args.interval is not None:
        updates["maintain_interval"] = args.interval
    if args.monitor_interval is not None:
        updates["monitor_interval"] = args.monitor_interval
    if args.session_monitor is not None:
        updates["session_monitor_enabled"] = args.session_monitor
    if args.session_monitor_interval is not None:
        updates["session_monitor_interval"] = args.session_monitor_interval
    if args.chrome_root is not None:
        updates["chrome_root"] = args.chrome_root
    if args.randomized_delay is not None:
        updates["randomized_delay"] = args.randomized_delay
    if args.history_retention_days is not None:
        updates["history_retention_days"] = args.history_retention_days
    if args.gateway_listen is not None:
        updates["gateway_listen"] = args.gateway_listen
    if args.gateway_api_key is not None:
        updates["gateway_api_key"] = args.gateway_api_key
    if not updates:
        raise ManagerError("provide a config value to update")

    config = save_config(paths, updates)
    print_config(paths, config)
    if args.apply_scheduler:
        status = apply_scheduler(paths, args.bin)
        print(f"scheduler applied: {status}")
    elif any(key in updates for key in ("maintain_interval", "monitor_interval", "session_monitor_interval", "session_monitor_enabled", "randomized_delay", "gateway_listen", "gateway_api_key")):
        print(dim("Run `codex-manager scheduler apply` to update the installed timer."))
    return 0
