from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .auth import account_metadata, read_auth
from .codex.limits import describe_rate_limit_windows, format_rate_limit_resets
from .config import ensure_config
from .errors import ManagerError
from .history import available_history_accounts, build_history_window
from .paths import Paths, account_path, list_accounts, status_path
from .recommendation import account_rank_sort_key, account_recommendations
from .storage import atomic_write_json, load_state, read_json
from .system import copy_text_to_clipboard
from .time_utils import human_delta, parse_datetime, utcnow
from .views import describe_account


@dataclass
class ChartDefaults:
    account: str | None = None
    hours: int | None = None
    days: int | None = None
    window_offset: int = 0
    timezone: str | None = None


def tracked_data_signature(paths: Paths) -> tuple[tuple[str, int, int], ...]:
    tracked_paths = [
        paths.state_file,
        paths.history_file,
        *sorted(paths.accounts_dir.glob("*.json")),
        *sorted(paths.status_dir.glob("*.json")),
    ]
    signature: list[tuple[str, int, int]] = []
    for path in tracked_paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def latest_account_refresh(paths: Paths) -> str | None:
    latest = None
    for name in list_accounts(paths):
        try:
            checked_at = parse_datetime(read_json(status_path(paths, name)).get("last_checked_at"))
        except ManagerError:
            checked_at = None
        if checked_at is not None and (latest is None or checked_at > latest):
            latest = checked_at
    if latest is None:
        return None
    return latest.isoformat()


def run_check_command(paths: Paths) -> dict[str, object]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(paths.codex_home)
    env["CODEX_MANAGER_HOME"] = str(paths.manager_home)
    env["CODEX_AUTH_PATH"] = str(paths.codex_auth)
    repo_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = repo_root if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from codex_manager.cli import main; raise SystemExit(main(['check', '--quiet']))",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    stderr = completed.stderr.strip()
    if completed.returncode not in {0, 1} or stderr:
        message = stderr or completed.stdout.strip() or f"codex-manager check exited {completed.returncode}"
        raise ManagerError(message)
    return {"returncode": completed.returncode}


def run_textual_dashboard(paths: Paths, *, initial_tab: str = "accounts", chart: ChartDefaults | None = None) -> None:
    os.environ.pop("NO_COLOR", None)
    os.environ.setdefault("FORCE_COLOR", "1")
    os.environ.setdefault("COLORTERM", "truecolor")
    try:
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from textual import events, work
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.screen import ModalScreen
        from textual.worker import Worker, WorkerState
        from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static, TabbedContent, TabPane
        from textual_plotext import PlotextPlot
    except ImportError as exc:
        raise ManagerError(
            "Textual UI dependencies are missing. Re-run setup.sh or install requirements.txt for the same python interpreter."
        ) from exc

    from .commands.accounts import activate, add_account, delete_account, rename_account
    from .commands.sessions import scan_chrome_profiles
    from .codex.device_login import DeviceLoginCode, login_with_device_code

    class AccountDetailStatic(Static):
        ALLOW_SELECT = True

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._email = ""
            self._email_line = -1

        def set_detail_content(self, content: str, *, email: str = "", email_line: int = -1) -> None:
            self._email = email
            self._email_line = email_line
            self.update(content)

        async def on_click(self, event: events.Click) -> None:
            if not self._email or self._email == "unknown":
                return
            if event.y == self._email_line:
                self.app.copy_to_clipboard(self._email)
                copy_text_to_clipboard(self._email)
                self.app.notify(f"Copied {self._email}", title="Email copied")
                event.stop()

    class DeleteConfirmModal(ModalScreen[str | None]):
        BINDINGS = [("escape", "cancel", "Cancel")]

        def __init__(self, account: str) -> None:
            super().__init__()
            self.account = account

        def compose(self) -> ComposeResult:
            with Vertical(id="delete-dialog"):
                yield Static("Delete account?", classes="title")
                yield Static(f"Account: {self.account}", id="delete-account-name")
                yield Static(
                    "This removes the stored auth.json copy and status file from codex-manager. "
                    "It will not delete the currently active Codex auth, but the manager account entry is gone.",
                    id="delete-warning",
                )
                with Horizontal(id="delete-actions", classes="button-row"):
                    yield Button("Cancel", id="cancel-delete")
                    yield Button("Delete Account", id="confirm-delete", variant="error")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "confirm-delete":
                self.dismiss(self.account)
            else:
                self.dismiss(None)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class RenameAccountModal(ModalScreen[tuple[str, str] | None]):
        BINDINGS = [("escape", "cancel", "Cancel")]

        def __init__(self, account: str) -> None:
            super().__init__()
            self.account = account

        def compose(self) -> ComposeResult:
            with Vertical(id="delete-dialog"):
                yield Static("Rename account", classes="title")
                yield Static(f"Current: {self.account}", id="delete-account-name")
                yield Static("New account name", classes="field-label")
                yield Input(value=self.account, id="rename-account-input")
                with Horizontal(id="delete-actions", classes="button-row"):
                    yield Button("Cancel", id="cancel-rename")
                    yield Button("Rename", id="confirm-rename", variant="primary")

        def on_mount(self) -> None:
            input_widget = self.query_one("#rename-account-input", Input)
            input_widget.focus()
            input_widget.select_all()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "confirm-rename":
                new_name = self.query_one("#rename-account-input", Input).value.strip()
                self.dismiss((self.account, new_name))
            else:
                self.dismiss(None)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ManagerApp(App[None]):
        _selected_account_name: str | None = None
        _account_names: list[str] = []
        _recommendations = {}
        _device_login_code: DeviceLoginCode | None = None
        _check_worker = None
        _check_requested_after_current = False
        _activation_worker = None
        _activation_target: str | None = None
        _device_login_worker_ref = None
        _device_login_cancel_event: threading.Event | None = None
        _chrome_scan_worker = None
        _chrome_profiles: list[dict[str, object]] = []
        _selected_browser_profile_key: str | None = None
        _add_method = "device"
        _last_data_signature: tuple[tuple[str, int, int], ...] = ()

        CSS = """
        Screen {
            layout: vertical;
            background: #282a36;
            color: #f8f8f2;
        }
        TabbedContent {
            height: 1fr;
        }
        TabPane {
            padding: 1 2;
        }
        .panel {
            border: round #6272a4;
            padding: 1;
            margin: 0 1 1 0;
            background: #21222c;
        }
        .title {
            color: #8be9fd;
            text-style: bold;
            margin-bottom: 1;
        }
        .accent {
            color: #50fa7b;
            text-style: bold;
        }
        .button-row {
            height: auto;
            margin-top: 1;
        }
        .hidden {
            display: none;
        }
        #accounts-header {
            height: auto;
            align-vertical: middle;
            margin-bottom: 1;
        }
        #accounts-layout, #chart-layout, #chrome-layout {
            height: 1fr;
        }
        #accounts-left {
            width: 7fr;
        }
        #accounts-right {
            width: 3fr;
            margin-right: 0;
        }
        #chrome-left {
            width: 7fr;
        }
        #chrome-right {
            width: 3fr;
            margin-right: 0;
        }
        #chrome-maintenance-panel {
            height: auto;
            padding: 0 1;
        }
        #chrome-maintenance-row {
            height: auto;
            align-vertical: middle;
            margin: 0;
        }
        #chrome-scan-status {
            color: #f8f8f2;
            width: 1fr;
            height: auto;
            margin: 0;
        }
        #chrome-list-panel {
            height: 1fr;
            margin-bottom: 0;
        }
        #account-maintenance-panel {
            height: auto;
            padding: 0 1;
        }
        #account-maintenance-row {
            height: auto;
            align-vertical: middle;
            margin: 0;
        }
        #accounts-list-panel {
            height: 1fr;
            margin-bottom: 0;
        }
        #account-maintenance-status {
            color: #f8f8f2;
            width: 1fr;
            height: auto;
            margin: 0;
        }
        #check-all {
            min-width: 14;
            width: 14;
            margin-right: 0;
        }
        #chart-controls {
            width: 38;
        }
        #add-method-panel {
            height: auto;
            padding: 0 1;
            margin-right: 0;
        }
        #add-method-switch {
            height: auto;
            margin: 0;
        }
        .method-tab {
            min-width: 16;
            margin-right: 1;
        }
        .field-label {
            color: #f8f8f2;
            margin-bottom: 1;
        }
        #add-device-panel, #add-manual-panel {
            margin-top: 0;
        }
        #add-device-copy-row {
            height: auto;
            align-vertical: middle;
            margin-top: 1;
        }
        #device-status-panel {
            margin-top: 1;
        }
        #chart-panel {
            width: 1fr;
            margin-right: 0;
        }
        .section-label {
            color: #ffb86c;
            text-style: bold;
            margin-bottom: 1;
        }
        Input, Select {
            margin-bottom: 1;
        }
        Input {
            background: #282a36;
            color: #f8f8f2;
            border: tall #6272a4;
        }
        Select {
            background: #282a36;
            color: #f8f8f2;
        }
        #account-detail, #chart-status, #device-status, #manual-status, #add-help {
            height: 1fr;
        }
        #account-table, #chart-status, #device-status, #manual-status, #add-help, #device-login-link, #device-login-code {
            color: #bd93f9;
        }
        #account-detail-summary {
            height: auto;
            min-height: 8;
            margin-bottom: 1;
            color: #f8f8f2;
        }
        #account-detail-extra {
            height: 1fr;
        }
        #account-table, #chrome-profile-table {
            height: 1fr;
        }
        #device-login-code-row {
            height: auto;
            align-vertical: middle;
            margin-bottom: 1;
        }
        #device-login-code {
            width: 1fr;
            padding: 0 1;
            background: #282a36;
            border: tall #6272a4;
            color: #f8f8f2;
        }
        Button {
            margin-right: 1;
            min-width: 14;
        }
        #copy-device-code {
            min-width: 8;
            width: 8;
            margin-right: 0;
        }
        #chart-plot {
            height: 1fr;
            background: #282a36;
        }
        #chart-legend {
            height: 1;
            margin-bottom: 1;
        }
        DeleteConfirmModal {
            align: center middle;
        }
        #delete-dialog {
            width: 64;
            height: auto;
            border: round #ff5555;
            background: #21222c;
            padding: 1 2;
        }
        #delete-account-name {
            color: #ff5555;
            text-style: bold;
            margin-bottom: 1;
        }
        #delete-warning {
            color: #ffb86c;
            margin-bottom: 1;
        }
        #delete-actions {
            height: auto;
            align-horizontal: right;
        }
        """

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("ctrl+r", "refresh_data", "Refresh"),
            ("ctrl+shift+c", "copy_selection", "Copy"),
            ("ctrl+1", "switch_accounts", "Accounts"),
            ("ctrl+2", "switch_chrome", "Chrome"),
            ("ctrl+3", "switch_add", "Add"),
            ("ctrl+4", "switch_chart", "Chart"),
        ]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with TabbedContent(initial=initial_tab):
                with TabPane("Accounts", id="accounts"):
                    with Horizontal(id="accounts-layout"):
                        with Vertical(id="accounts-left"):
                            with Vertical(id="account-maintenance-panel", classes="panel"):
                                with Horizontal(id="account-maintenance-row"):
                                    yield Static("", id="account-maintenance-status")
                                    yield Button("↻ Check All", id="check-all", variant="primary", compact=True)
                            with Vertical(id="accounts-list-panel", classes="panel"):
                                yield Static("Account Browser", classes="title")
                                yield DataTable(
                                    show_row_labels=False,
                                    cursor_type="row",
                                    zebra_stripes=True,
                                    id="account-table",
                                )
                                yield Static("Selected Account Actions", classes="section-label")
                                with Horizontal(classes="button-row"):
                                    yield Button("Activate", id="activate", variant="success", disabled=True)
                                    yield Button("Rename...", id="rename", disabled=True)
                                    yield Button("Relogin", id="relogin", variant="warning", disabled=True, classes="hidden")
                                    yield Button("Open Chart", id="open-chart", disabled=True)
                                    yield Button("Open Chrome", id="open-chrome", disabled=True)
                                    yield Button("Ignore Sessions", id="toggle-session-monitor", disabled=True)
                                    yield Button("Delete...", id="delete", variant="error", disabled=True)
                        with Vertical(id="accounts-right", classes="panel"):
                            yield Static("Selected Account", classes="title")
                            yield AccountDetailStatic("", id="account-detail-summary")
                            yield Static("", expand=True, id="account-detail-extra")
                with TabPane("Chrome", id="chrome"):
                    with Horizontal(id="chrome-layout"):
                        with Vertical(id="chrome-left"):
                            with Vertical(id="chrome-maintenance-panel", classes="panel"):
                                with Horizontal(id="chrome-maintenance-row"):
                                    yield Static("Profile scan has not run yet.", id="chrome-scan-status")
                                    yield Button("Scan Profiles", id="scan-chrome-profiles", variant="primary", compact=True)
                            with Vertical(id="chrome-list-panel", classes="panel"):
                                yield Static("Chrome Profiles", classes="title")
                                yield DataTable(show_row_labels=False, cursor_type="row", zebra_stripes=True, id="chrome-profile-table")
                                with Horizontal(classes="button-row"):
                                    yield Button("Open Chrome", id="open-browser-profile", disabled=True)
                        with Vertical(id="chrome-right", classes="panel"):
                            yield Static("Selected Chrome Profile", classes="title")
                            yield Static("Scan Chrome profiles to verify their ChatGPT sign-in state.", id="chrome-profile-detail")
                with TabPane("Add", id="add"):
                    with Vertical():
                        with Vertical(id="add-method-panel", classes="panel"):
                            with Horizontal(id="add-method-switch"):
                                yield Button("Device Login", id="add-method-device", classes="method-tab", variant="primary")
                                yield Button("Manual Import", id="add-method-manual", classes="method-tab")
                        with Vertical(id="add-device-panel", classes="panel"):
                            yield Static("Device Login", classes="title")
                            yield Static(
                                "Use ChatGPT device login to create a fresh token from inside codex-manager.",
                                classes="field-label",
                            )
                            with Vertical(id="device-form-step"):
                                yield Static("Account Name", classes="field-label")
                                yield Input(placeholder="device-login account name", id="add-name")
                                with Horizontal(classes="button-row"):
                                    yield Button("Start Device Login", id="start-device-login", variant="primary")
                            with Vertical(id="device-status-panel", classes="hidden"):
                                yield Static("Login Status", classes="section-label")
                                yield Static(
                                    "Open the ChatGPT verification URL, submit the code, and keep this screen open.",
                                    id="add-help",
                                )
                                yield Static("Verification URL will appear here.", id="device-login-link")
                                with Horizontal(id="add-device-copy-row"):
                                    yield Static("Code: not requested yet", id="device-login-code")
                                    yield Button("Copy", id="copy-device-code", disabled=True)
                                    yield Button("Cancel", id="cancel-device-login", variant="error")
                                yield Static("", id="device-status")
                        with Vertical(id="add-manual-panel", classes="panel hidden"):
                            yield Static("Manual Import", classes="title")
                            yield Static("Account Name", classes="field-label")
                            yield Input(placeholder="manual account name", id="add-manual-name")
                            yield Static("auth.json Path", classes="field-label")
                            yield Input(placeholder="/path/to/auth.json for manual import", id="add-auth-path")
                            with Horizontal(classes="button-row"):
                                yield Button("Import File", id="submit-add", variant="success")
                            yield Static("", id="manual-status")
                with TabPane("Charts", id="charts"):
                    with Horizontal(id="chart-layout"):
                        with Vertical(id="chart-controls", classes="panel"):
                            yield Static("History Filters", classes="title")
                            yield Select([], prompt="History account", allow_blank=True, id="chart-account")
                            yield Input(
                                value=str(chart.hours or chart.days or 24) if chart else "24",
                                placeholder="Window size",
                                id="chart-range",
                            )
                            yield Select(
                                [("Hours", "hours"), ("Days", "days")],
                                prompt="Unit",
                                allow_blank=False,
                                value=("days" if chart and chart.days else "hours"),
                                id="chart-unit",
                            )
                            yield Input(
                                value=str(chart.window_offset if chart else 0),
                                placeholder="Lookback offset",
                                id="chart-window-offset",
                            )
                            yield Input(
                                value=chart.timezone or "local" if chart else "local",
                                placeholder="local | UTC | +03:30",
                                id="chart-timezone",
                            )
                            with Horizontal(classes="button-row"):
                                yield Button("Render Chart", id="render-chart", variant="primary")
                                yield Button("Sync From Accounts", id="chart-from-account")
                            with Horizontal(classes="button-row"):
                                yield Button("Activate", id="chart-activate", variant="success")
                            yield Static("", id="chart-status")
                        with Vertical(id="chart-panel", classes="panel"):
                            yield Static("Rate Limit History", classes="title")
                            yield Static("", id="chart-legend")
                            yield PlotextPlot(id="chart-plot")
            yield Footer()

        def on_mount(self) -> None:
            account_table = self.query_one("#account-table", DataTable)
            account_table.add_columns("Pick", "On", "Account", "Email", "Plan", "Chrome", "Codex", "Revoked", "State", "Limit")
            chrome_table = self.query_one("#chrome-profile-table", DataTable)
            chrome_table.add_columns("Status", "Profile", "Active ChatGPT", "Plan", "Saved ChatGPT", "Previous")
            self._refresh_dashboard_data(update_banner=False)
            self._apply_chart_defaults()
            self._set_add_method("device", focus_input=False)
            self._focus_account_table()
            self.set_interval(1.5, self._poll_for_data_changes)

        def action_refresh_data(self) -> None:
            self._refresh_dashboard_data()
            self._set_banner("Reloaded account state and cached history.")

        def action_switch_accounts(self) -> None:
            self._switch_tab("accounts")

        def action_switch_add(self) -> None:
            self._switch_tab("add")

        def action_switch_chrome(self) -> None:
            self._switch_tab("chrome")
            if self._chrome_scan_worker is None and not self._chrome_profiles:
                self._scan_chrome_profiles()

        def action_switch_chart(self) -> None:
            self._switch_tab("charts", after=self._render_chart_if_possible)

        def action_copy_selection(self) -> None:
            selection = self.screen.get_selected_text()
            if not selection:
                self._set_banner("No text is selected.")
                return
            copied_with_system_clipboard = copy_text_to_clipboard(selection)
            self.copy_to_clipboard(selection)
            if copied_with_system_clipboard:
                self._set_banner("Selected text copied to clipboard.")
            else:
                self._set_banner("Selected text sent to terminal clipboard.")

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id == "chart-account":
                self._render_chart_if_possible()

        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            if event.data_table.id == "account-table" and self._active_tab() == "accounts":
                self._set_selected_account(str(event.row_key.value))
            elif event.data_table.id == "chrome-profile-table" and self._active_tab() == "chrome":
                self._set_selected_browser_profile(str(event.row_key.value))

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            if event.data_table.id == "account-table" and self._active_tab() == "accounts":
                selected_name = str(event.row_key.value)
                self._set_selected_account(selected_name)
                if event.data_table.cursor_column == 3:
                    self._copy_account_email(selected_name)
                    return
                if event.data_table.cursor_column == 5:
                    self._open_selected_chrome_profile()
                    return
                self._activate_selected_account()
            elif event.data_table.id == "chrome-profile-table" and self._active_tab() == "chrome":
                self._set_selected_browser_profile(str(event.row_key.value))
                self._open_selected_browser_profile()

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id
            if button_id == "activate":
                self._activate_selected_account()
            elif button_id == "check-all":
                self._run_check_all()
            elif button_id == "relogin":
                self._start_relogin_selected_account()
            elif button_id == "rename":
                self._rename_selected_account()
            elif button_id == "delete":
                self._delete_selected_account()
            elif button_id == "open-chart":
                self._push_selected_account_to_chart()
            elif button_id == "open-chrome":
                self._open_selected_chrome_profile()
            elif button_id == "toggle-session-monitor":
                self._toggle_selected_session_monitor()
            elif button_id == "scan-chrome-profiles":
                self._scan_chrome_profiles()
            elif button_id == "open-browser-profile":
                self._open_selected_browser_profile()
            elif button_id == "start-device-login":
                self._start_device_login()
            elif button_id == "cancel-device-login":
                self._cancel_device_login()
            elif button_id == "add-method-device":
                self._set_add_method("device")
            elif button_id == "add-method-manual":
                self._set_add_method("manual")
            elif button_id == "copy-device-code":
                self._copy_device_code()
            elif button_id == "submit-add":
                self._submit_add()
            elif button_id == "render-chart":
                self._render_chart()
            elif button_id == "chart-from-account":
                self._push_selected_account_to_chart()
            elif button_id == "chart-activate":
                self._activate_chart_account()

        def _selected_account(self) -> str | None:
            return self._selected_account_name

        def _selected_chart_account(self) -> str | None:
            value = self.query_one("#chart-account", Select).value
            return value if isinstance(value, str) else None

        def _active_tab(self) -> str | None:
            active = self.query_one(TabbedContent).active
            return active if isinstance(active, str) else None

        def _focus_tab_content(self, tab_id: str) -> None:
            if tab_id == "accounts":
                self._focus_account_table()
            elif tab_id == "chrome":
                self.query_one("#chrome-profile-table", DataTable).focus()
            elif tab_id == "charts":
                self.query_one("#chart-account", Select).focus()
            elif tab_id == "add":
                if self._device_login_worker_ref is not None or not self.query_one("#device-status-panel", Vertical).has_class("hidden"):
                    self.query_one("#cancel-device-login", Button).focus()
                elif self._add_method == "manual":
                    self.query_one("#add-manual-name", Input).focus()
                else:
                    self.query_one("#add-name", Input).focus()

        def _switch_tab(self, tab_id: str, after=None) -> None:
            def callback() -> None:
                self.query_one(TabbedContent).active = tab_id
                if after is not None:
                    after()
                self._focus_tab_content(tab_id)

            self.call_after_refresh(callback)

        def _set_add_method(self, method: str, *, focus_input: bool = True) -> None:
            self._add_method = method
            device_panel = self.query_one("#add-device-panel", Vertical)
            manual_panel = self.query_one("#add-manual-panel", Vertical)
            device_button = self.query_one("#add-method-device", Button)
            manual_button = self.query_one("#add-method-manual", Button)
            if method == "manual":
                if self._device_login_worker_ref is not None:
                    self._cancel_device_login()
                device_panel.add_class("hidden")
                manual_panel.remove_class("hidden")
                device_button.variant = "default"
                manual_button.variant = "primary"
                if focus_input:
                    self.query_one("#add-manual-name", Input).focus()
            else:
                manual_panel.add_class("hidden")
                device_panel.remove_class("hidden")
                manual_button.variant = "default"
                device_button.variant = "primary"
                self._reset_device_login_view()
                if focus_input:
                    self.query_one("#add-name", Input).focus()

        def _reset_device_login_view(self) -> None:
            self._device_login_code = None
            self.query_one("#device-form-step", Vertical).remove_class("hidden")
            self.query_one("#device-status-panel", Vertical).add_class("hidden")
            self.query_one("#device-login-link", Static).update("Verification URL will appear here.")
            self.query_one("#device-login-code", Static).update("Code: not requested yet")
            self.query_one("#device-status", Static).update("")
            self.query_one("#copy-device-code", Button).disabled = True
            self.query_one("#cancel-device-login", Button).disabled = False
            button = self.query_one("#start-device-login", Button)
            button.disabled = False
            button.label = "Start Device Login"
            self.query_one("#add-name", Input).disabled = False

        def _refresh_dashboard_data(
            self,
            *,
            preserve_selected: bool = True,
            rerender_chart: bool = True,
            update_banner: bool = False,
        ) -> None:
            active_tab_before = self._active_tab()
            selected_before = self._selected_account() if preserve_selected else None
            self._refresh_accounts(
                update_banner=update_banner,
                sync_table_cursor=active_tab_before == "accounts",
            )
            self._refresh_chart_accounts()
            if selected_before and selected_before in self._account_names:
                if active_tab_before == "accounts":
                    self._select_account_row(selected_before)
                else:
                    self._set_selected_account(selected_before)
            if active_tab_before and self._active_tab() != active_tab_before:
                self.query_one(TabbedContent).active = active_tab_before
                self._focus_tab_content(active_tab_before)
            # Background check/session services update several files in quick
            # succession. Avoid rebuilding the plot while the user is working
            # in Accounts; chart rendering is comparatively expensive.
            if rerender_chart and active_tab_before == "charts":
                self._render_chart_if_possible()
            self._last_data_signature = tracked_data_signature(paths)

        def _poll_for_data_changes(self) -> None:
            if self._manager_operation_in_progress():
                return
            current_signature = tracked_data_signature(paths)
            if current_signature != self._last_data_signature:
                self._refresh_dashboard_data()
                self._set_banner("Dashboard updated from the background monitor.")

        def _schedule_background_check(self, *, banner_message: str) -> None:
            if self._check_worker is not None:
                self._check_requested_after_current = True
                self._set_banner(banner_message)
                return
            self._set_banner(banner_message)
            self._run_check_all()

        def _refresh_accounts(self, *, update_banner: bool = True, sync_table_cursor: bool = True) -> None:
            active = load_state(paths).get("active")
            names = list_accounts(paths)
            self._recommendations = account_recommendations(paths, names)
            self._account_names = self._ordered_account_names(names)
            if self._account_names:
                current = self._selected_account()
                target = (
                    current
                    if current in self._account_names
                    else active
                    if active in self._account_names
                    else self._account_names[0]
                )
                self._update_account_table(active)
                if sync_table_cursor:
                    self._select_account_row(target)
                else:
                    self._set_selected_account(target)
                if update_banner:
                    self._set_banner(
                        f"Primary account: {active}" if active else "No primary account selected yet."
                    )
            else:
                self.query_one("#account-table", DataTable).clear(columns=False)
                self._selected_account_name = None
                self._recommendations = {}
                self._update_account_action_state()
                self.query_one("#account-detail-summary", AccountDetailStatic).set_detail_content("Import an auth.json to get started.")
                self.query_one("#account-detail-extra", Static).update("")
                if update_banner:
                    self._set_banner("No primary account selected yet.")

        def _refresh_chart_accounts(self) -> None:
            active = load_state(paths).get("active")
            names = sorted(set(available_history_accounts(paths)) | set(list_accounts(paths)))
            select = self.query_one("#chart-account", Select)
            select.set_options([(self._account_option_label(name, active), name) for name in names])
            if names and self._selected_chart_account() not in names:
                select.value = active if active in names else names[0]

        def _refresh_chrome_profile_table(self) -> None:
            table = self.query_one("#chrome-profile-table", DataTable)
            table.clear(columns=False)
            if not self._chrome_profiles:
                self._selected_browser_profile_key = None
                self.query_one("#open-browser-profile", Button).disabled = True
                self.query_one("#chrome-profile-detail", Static).update("No Chrome profile scan result is available yet.")
                return
            for profile in self._chrome_profiles:
                saved_accounts = profile.get("saved_accounts")
                cached_accounts = profile.get("cached_accounts")
                table.add_row(
                    self._chrome_status_badge(str(profile.get("outcome") or "error")),
                    str(profile.get("label") or "unknown"),
                    str(profile.get("active_email") or "-"),
                    self._plan_badge(str(profile.get("managed_plan") or "unknown")),
                    ", ".join(saved_accounts) if isinstance(saved_accounts, list) and saved_accounts else "-",
                    ", ".join(cached_accounts) if isinstance(cached_accounts, list) and cached_accounts else "-",
                    key=str(profile["key"]),
                )
            available = {str(profile["key"]) for profile in self._chrome_profiles}
            target = self._selected_browser_profile_key if self._selected_browser_profile_key in available else str(self._chrome_profiles[0]["key"])
            self._select_browser_profile_row(target)

        def _chrome_status_badge(self, outcome: str) -> Text:
            labels = {
                "signed_in": ("SIGNED IN", "bold #50fa7b"),
                "partial": ("PARTIAL", "bold #ff5555"),
                "signed_out": ("SIGNED OUT", "bold #ff5555"),
                "error": ("CHECK ERROR", "bold #ffb86c"),
            }
            label, style = labels.get(outcome, ("UNKNOWN", "dim"))
            return Text(label, style=style)

        def _selected_browser_profile(self) -> dict[str, object] | None:
            key = self._selected_browser_profile_key
            return next((profile for profile in self._chrome_profiles if profile.get("key") == key), None) if key else None

        def _set_selected_browser_profile(self, key: str | None) -> None:
            self._selected_browser_profile_key = key
            profile = self._selected_browser_profile()
            self.query_one("#open-browser-profile", Button).disabled = profile is None
            if profile is None:
                self.query_one("#chrome-profile-detail", Static).update("Pick a Chrome profile to inspect its sign-in state.")
                return
            saved_accounts = profile.get("saved_accounts")
            cached_accounts = profile.get("cached_accounts")
            lines = [
                f"Profile: {profile.get('label') or 'unknown'}",
                f"Status: {str(profile.get('outcome') or 'unknown').replace('_', ' ')}",
                f"Active ChatGPT: {profile.get('active_email') or '-'}",
                f"Cookie Account: {profile.get('cookie_email') or '-'}",
                f"Plan: {profile.get('managed_plan') or '-'}",
                "Saved ChatGPT Accounts: " + ", ".join(saved_accounts) if isinstance(saved_accounts, list) and saved_accounts else "Saved ChatGPT Accounts: -",
                "Previous Managed Accounts: " + ", ".join(cached_accounts) if isinstance(cached_accounts, list) and cached_accounts else "Previous Managed Accounts: -",
            ]
            reason = profile.get("reason")
            if isinstance(reason, str) and reason:
                lines.append(f"Reason: {reason}")
            self.query_one("#chrome-profile-detail", Static).update("\n".join(lines))

        def _select_browser_profile_row(self, key: str | None) -> None:
            if not key:
                self._set_selected_browser_profile(None)
                return
            table = self.query_one("#chrome-profile-table", DataTable)
            try:
                row_index = [str(profile["key"]) for profile in self._chrome_profiles].index(key)
            except ValueError:
                self._set_selected_browser_profile(None)
                return
            table.move_cursor(row=row_index, column=0, animate=False, scroll=True)
            self._set_selected_browser_profile(key)

        def _update_account_table(self, active: str | None) -> None:
            table = self.query_one("#account-table", DataTable)
            table.clear(columns=False)
            rows = [describe_account(paths, name, active) for name in self._account_names]
            if not rows:
                return
            for row in rows:
                marker = "●" if row["name"] == active else "○"
                recommendation = self._recommendations.get(row["name"])
                label = recommendation.label if recommendation else ""
                table.add_row(
                    label,
                    marker,
                    row["name"],
                    row["email"],
                    self._plan_badge(row["plan"]),
                    row["chrome_profile"],
                    self._session_count_badge(row["codex_sessions"]),
                    self._revoked_count_badge(row["revoked_total"]),
                    self._state_badge(row["state"]),
                    self._limit_bar(recommendation.weekly_remaining if recommendation else None, "magenta"),
                    key=row["name"],
                )

        def _plan_badge(self, plan: str) -> Text:
            if plan == "free":
                return Text("FREE", style="bold #ffb86c")
            if plan in {"plus", "pro", "team", "business", "enterprise"}:
                return Text(plan.upper(), style="bold #8be9fd")
            return Text("-", style="dim")

        def _state_badge(self, state: str) -> Text:
            if state == "session alert":
                return Text("ALERT", style="bold #ff5555")
            if state in {"warning", "refresh soon"}:
                return Text(state, style="bold #ffb86c")
            if state in {"needs_login", "error"}:
                return Text(state, style="bold #ff5555")
            return Text(state, style="bold #8be9fd" if state == "active" else "")

        def _session_count_badge(self, value: str) -> Text:
            if not value.isdigit():
                if value in {"error", "partial"}:
                    return Text(value, style="bold #ff5555")
                return Text(value, style="bold #ffb86c" if value == "unavailable" else "dim")
            count = int(value)
            color = "#ff5555" if count > 1 else "#8be9fd"
            return Text(value, style=f"bold {color}")

        def _revoked_count_badge(self, value: str) -> Text:
            if value == "-":
                return Text("-", style="dim")
            return Text(value, style="bold #ffb86c" if int(value) else "dim")

        def _session_check_history_lines(self, status: dict[str, object]) -> list[str]:
            session_monitor = status.get("session_monitor")
            if not isinstance(session_monitor, dict):
                return []
            history = session_monitor.get("check_history")
            if not isinstance(history, list):
                return []
            lines: list[str] = []
            for entry in history[:3]:
                if not isinstance(entry, dict):
                    continue
                checked_at = parse_datetime(entry.get("checked_at"))
                if checked_at is None:
                    continue
                lines.append(checked_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
            return lines

        def _ordered_account_names(self, names: list[str]) -> list[str]:
            active = load_state(paths).get("active")
            plans = {name: describe_account(paths, name, active)["plan"] for name in names}
            return sorted(
                names,
                key=lambda name: account_rank_sort_key(
                    plans[name],
                    self._recommendations.get(name).score if self._recommendations.get(name) else float("-inf"),
                    name,
                ),
            )

        def _limit_bar(self, percent: float | None, color: str) -> Text:
            if percent is None:
                return Text("unknown", style="dim")
            clamped = max(0.0, min(100.0, float(percent)))
            filled = round(clamped / 10)
            empty = 10 - filled
            text = Text()
            text.append("█" * filled, style=f"bold {color}")
            text.append("░" * empty, style="dim")
            text.append(f" {clamped:>5.1f}%", style=f"bold {color}")
            return text

        def _account_option_label(self, name: str, active: str | None) -> str:
            return f"● {name}  [primary]" if name == active else f"○ {name}"

        def _focus_account_table(self) -> None:
            if self._active_tab() == "accounts":
                self.query_one("#account-table", DataTable).focus()

        def _update_account_maintenance_status(self) -> None:
            interval = str(ensure_config(paths).get("monitor_interval"))
            latest_refresh_raw = latest_account_refresh(paths)
            if self._activation_worker is not None:
                status_text = f"Last refresh: switching to {self._activation_target or 'account'}... | monitor {interval}"
            elif self._check_worker is not None:
                status_text = f"Last refresh: checking now | monitor {interval}"
            elif latest_refresh_raw:
                refreshed_at = parse_datetime(latest_refresh_raw)
                if refreshed_at is not None:
                    status_text = (
                        f"Last refresh: {human_delta(utcnow() - refreshed_at)} ago"
                        f" | {refreshed_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
                        f" | monitor {interval}"
                    )
                else:
                    status_text = f"Last refresh: {latest_refresh_raw} | monitor {interval}"
            else:
                status_text = f"Last refresh: not available yet | monitor {interval}"

            self.query_one("#account-maintenance-status", Static).update(status_text)

        def _update_account_action_state(self) -> None:
            has_accounts = bool(self._account_names)
            has_selection = isinstance(self._selected_account_name, str) and self._selected_account_name in self._account_names
            selected_state = self._selected_account_state() if has_selection else None
            account_operation_busy = self._check_worker is not None or self._activation_worker is not None
            self.query_one("#check-all", Button).disabled = (not has_accounts) or account_operation_busy
            self.query_one("#activate", Button).disabled = (
                not has_selection or selected_state == "needs_login" or account_operation_busy
            )
            self.query_one("#chart-activate", Button).disabled = account_operation_busy
            can_relogin = has_selection and selected_state == "needs_login" and not account_operation_busy
            relogin_button = self.query_one("#relogin", Button)
            relogin_button.disabled = not can_relogin
            if can_relogin:
                relogin_button.remove_class("hidden")
            else:
                relogin_button.add_class("hidden")
            self.query_one("#open-chart", Button).disabled = not has_selection
            self.query_one("#open-chrome", Button).disabled = not self._selected_chrome_profile()
            session_button = self.query_one("#toggle-session-monitor", Button)
            can_toggle_sessions = self._selected_chrome_profile() is not None
            session_button.disabled = not can_toggle_sessions
            session_button.label = "Monitor Sessions" if self._selected_session_monitor_disabled() else "Ignore Sessions"
            self.query_one("#rename", Button).disabled = not has_selection or account_operation_busy
            self.query_one("#delete", Button).disabled = not has_selection or account_operation_busy
            self._update_account_maintenance_status()

        def _manager_operation_in_progress(self) -> bool:
            return self._check_worker is not None or self._activation_worker is not None

        def _selected_account_state(self) -> str | None:
            name = self._selected_account()
            if not name:
                return None
            return self._account_state(name)

        def _account_state(self, name: str) -> str | None:
            active = load_state(paths).get("active")
            return describe_account(paths, name, active).get("state")

        def _selected_chrome_profile(self) -> dict[str, str] | None:
            name = self._selected_account()
            if not name:
                return None
            try:
                profile = read_json(status_path(paths, name)).get("chrome_profile")
            except ManagerError:
                return None
            if not isinstance(profile, dict):
                return None
            directory = profile.get("directory")
            if not isinstance(directory, str) or not directory:
                return None
            return {key: value for key, value in profile.items() if isinstance(value, str)}

        def _selected_session_monitor_disabled(self) -> bool:
            name = self._selected_account()
            if not name:
                return False
            try:
                return read_json(status_path(paths, name)).get("session_monitor_disabled") is True
            except ManagerError:
                return False

        def _toggle_selected_session_monitor(self) -> None:
            name = self._selected_account()
            if not name or self._selected_chrome_profile() is None:
                self._set_banner("No Chrome profile mapping for this account yet.")
                return
            try:
                status = read_json(status_path(paths, name))
            except ManagerError:
                status = {}
            disabled = not (status.get("session_monitor_disabled") is True)
            status["session_monitor_disabled"] = disabled
            atomic_write_json(status_path(paths, name), status)
            self._update_account_action_state()
            self._set_banner(
                f"Session monitoring {'disabled' if disabled else 'enabled'} for {name}."
            )

        def _open_selected_chrome_profile(self) -> None:
            profile = self._selected_chrome_profile()
            if profile is None:
                self._set_banner("No Chrome profile mapping for this account yet. Run the session scan first.")
                return
            self._open_chrome_profile(profile["directory"], profile.get("chrome_root"))

        def _open_selected_browser_profile(self) -> None:
            profile = self._selected_browser_profile()
            if profile is None:
                self._set_banner("Select a Chrome profile first.")
                return
            directory = profile.get("directory")
            if not isinstance(directory, str) or not directory:
                self._set_banner("The selected Chrome profile has no profile directory.")
                return
            root = profile.get("chrome_root")
            self._open_chrome_profile(directory, root if isinstance(root, str) else None)

        def _open_chrome_profile(self, directory: str, root: str | None) -> None:
            chrome = next((shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium") if shutil.which(name)), None)
            if chrome is None:
                self._set_banner("Chrome or Chromium was not found in PATH.")
                return
            command = [chrome, f"--profile-directory={directory}"]
            if root:
                command.append(f"--user-data-dir={root}")
            try:
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self._set_banner(f"Could not open Chrome: {exc}")
                return
            self._set_banner(f"Opened Chrome profile {directory}.")

        def _set_selected_account(self, name: str | None) -> None:
            self._selected_account_name = name
            self._update_account_action_state()
            self._update_account_detail(name)

        def _select_account_row(self, name: str | None) -> None:
            if not name:
                self._set_selected_account(None)
                return
            table = self.query_one("#account-table", DataTable)
            try:
                row_index = self._account_names.index(name)
            except ValueError:
                self._set_selected_account(None)
                return
            table.move_cursor(row=row_index, column=0, animate=False, scroll=True)
            self._set_selected_account(name)

        def _update_account_detail(self, name: object) -> None:
            if not isinstance(name, str):
                self.query_one("#account-detail-summary", AccountDetailStatic).set_detail_content(
                    "Pick an account to inspect its auth and limit summary."
                )
                self.query_one("#account-detail-extra", Static).update("")
                return
            active = load_state(paths).get("active")
            row = describe_account(paths, name, active)
            reset_lines = []
            limit_windows = []
            try:
                status = read_json(status_path(paths, name))
                reset_lines = format_rate_limit_resets(status.get("rate_limits"))
                limit_windows = describe_rate_limit_windows(status.get("rate_limits"))
            except ManagerError:
                status = {}
                reset_lines = []
                limit_windows = []
            primary_line = "Primary Account: yes" if row["name"] == active else "Primary Account: no"
            session_check_lines = self._session_check_history_lines(status)
            recommendation = self._recommendations.get(name)
            summary_lines = [
                f"Account: {row['name']}",
                primary_line,
                f"Email: {row['email']}",
                f"Account ID: {row['account']}",
                f"Plan: {row['plan']}",
                f"Codex Sessions: {row['codex_sessions']}",
                f"Session Revokes: {row['revoked_total']}",
                f"Session Monitor: {row['session_monitor_mode']}",
                f"State: {row['state']}",
                f"Expires In: {row['expires']}",
                f"Limits: {row['limits']}",
                "Session Checks:"
                if session_check_lines
                else "Session Checks: none yet",
            ]
            summary_lines.extend(f"  {line}" for line in session_check_lines)
            email_line = 2
            self.query_one("#account-detail-summary", AccountDetailStatic).set_detail_content(
                "\n".join(summary_lines),
                email=row["email"],
                email_line=email_line,
            )
            self.query_one("#account-detail-extra", Static).update(
                self._build_account_detail_extra(row["reason"], reset_lines, limit_windows, recommendation)
            )

        def _build_account_detail_extra(
            self,
            reason: str,
            reset_lines: list[str],
            windows: list[dict[str, object]],
            recommendation,
        ) -> Group:
            renderables: list[object] = []
            if windows:
                renderables.append(self._build_limit_visual_panel(windows))
            elif reset_lines:
                renderables.append(Panel(Text("\n".join(reset_lines), style="#ffb86c"), title="Resets", border_style="#6272a4"))
            renderables.append(Panel(Text(reason, style="#f8f8f2"), title="Status Reason", border_style="#6272a4"))
            if recommendation:
                renderables.append(self._build_recommendation_visual_panel(recommendation))
            return Group(*renderables)

        def _build_limit_visual_panel(self, windows: list[dict[str, object]]) -> Panel:
            body = Table.grid(expand=True)
            body.add_column(ratio=2)
            for window in windows:
                label = str(window.get("label") or "window")
                remaining = window.get("remaining_percent")
                used = window.get("used_percent")
                reached = bool(window.get("reached"))
                reset_text = str(window.get("reset_text") or "unknown")
                reset_in_text = str(window.get("reset_in_text") or "unknown")
                bar = self._detail_limit_bar(remaining, reached=reached, color="magenta" if label == "weekly" else "cyan")
                lines = Text()
                lines.append(f"{label}  ", style="bold #8be9fd")
                lines.append("reached" if reached else "available", style="bold #ff5555" if reached else "bold #50fa7b")
                lines.append("\n")
                lines.append(bar)
                lines.append("\n")
                if isinstance(remaining, (int, float)):
                    lines.append(f"remaining {remaining:>5.1f}%", style="#f8f8f2")
                else:
                    lines.append("remaining unknown", style="dim")
                if isinstance(used, (int, float)):
                    lines.append(f"   used {used:>5.1f}%", style="#f8f8f2")
                lines.append("\n")
                lines.append(f"reset in {reset_in_text}", style="#ffb86c")
                lines.append("\n")
                lines.append(reset_text, style="#bd93f9")
                body.add_row(lines)
            return Panel(body, title="Limit Visual", border_style="#6272a4")

        def _build_recommendation_visual_panel(self, recommendation) -> Panel:
            weekly_remaining = recommendation.weekly_remaining
            weekly_target = recommendation.weekly_target
            weekly_used = None if weekly_remaining is None else 100.0 - weekly_remaining
            target_used = None if weekly_target is None else 100.0 - weekly_target
            pace_gap = None if recommendation.weekly_health is None else -recommendation.weekly_health

            body = Table.grid(expand=True)
            body.add_column(width=14, style="bold #8be9fd")
            body.add_column(ratio=1)

            status_text = Text()
            status_style = {
                "BEST": "bold #50fa7b",
                "OK": "bold #50fa7b",
                "WAIT": "bold #f1fa8c",
                "SAVE": "bold #ff5555",
                "RISK": "bold #ff5555",
                "STALE": "bold #ffb86c",
                "CHECK": "bold #ffb86c",
                "LOGIN": "bold #ff5555",
            }.get(recommendation.label, "bold #f8f8f2")
            status_text.append(recommendation.label, style=status_style)
            body.add_row("Status", status_text)
            body.add_row("Should Use", self._metric_line(target_used, color="magenta"))
            body.add_row("Used Now", self._metric_line(weekly_used, color="cyan"))
            body.add_row("Pace Gap", self._delta_line(pace_gap))
            return Panel(body, title="Recommendation", border_style="#6272a4")

        def _metric_line(self, percent: object, *, color: str) -> Text:
            if not isinstance(percent, (int, float)):
                return Text("unknown", style="dim")
            clamped = max(0.0, min(100.0, float(percent)))
            filled = round(clamped / 10)
            empty = 10 - filled
            text = Text()
            text.append("█" * filled, style=f"bold {color}")
            text.append("░" * empty, style="dim")
            text.append(f" {clamped:>5.1f}%", style=f"bold {color}")
            return text

        def _delta_line(self, delta: object) -> Text:
            if not isinstance(delta, (int, float)):
                return Text("unknown", style="dim")
            value = float(delta)
            text = Text()
            if value > 0:
                text.append("over ", style="bold #ff5555")
                text.append(f"+{value:>4.1f}%", style="bold #ff5555")
            elif value < 0:
                text.append("under ", style="bold #50fa7b")
                text.append(f"{value:>5.1f}%", style="bold #50fa7b")
            else:
                text.append("on pace 0.0%", style="bold #8be9fd")
            return text

        def _detail_limit_bar(self, percent: object, *, reached: bool, color: str) -> Text:
            if not isinstance(percent, (int, float)):
                return Text("unknown", style="dim")
            clamped = max(0.0, min(100.0, float(percent)))
            filled = round(clamped / 5)
            empty = 20 - filled
            text = Text()
            fill_style = "bold #ff5555" if reached else f"bold {color}"
            fill_char = "■" if reached else "█"
            text.append(fill_char * filled, style=fill_style)
            text.append("░" * empty, style="dim")
            return text

        def _activate_selected_account(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            self._activate_account(name, source="Accounts")

        def _activate_account(self, name: str, *, source: str) -> None:
            if self._activation_worker is not None:
                self._set_banner(f"Already switching to {self._activation_target or 'another account'}.")
                return
            if self._check_worker is not None:
                self._set_banner("Account check is running; wait for it to finish before switching.")
                return
            if self._account_state(name) == "needs_login":
                self._set_banner(f"{name} needs relogin before activation.")
                return
            self._activation_target = name
            self._set_banner(f"Switching primary account to {name} from {source}...")
            self._update_account_action_state()
            self._activation_worker = self._activate_account_worker(name)

        def _activate_chart_account(self) -> None:
            name = self._selected_chart_account()
            if not name:
                self._set_banner("Select a chart account first.")
                return
            self._activate_account(name, source="Charts")

        def _scan_chrome_profiles(self) -> None:
            if self._chrome_scan_worker is not None:
                self._set_banner("Chrome profile scan is already running.")
                return
            button = self.query_one("#scan-chrome-profiles", Button)
            button.disabled = True
            button.label = "Scanning..."
            self.query_one("#chrome-scan-status", Static).update("Verifying complete ChatGPT sign-in for every Chrome profile...")
            self._chrome_scan_worker = self._scan_chrome_profiles_worker()

        @work(thread=True, group="chrome-profile-scan", exclusive=True, exit_on_error=False)
        def _scan_chrome_profiles_worker(self) -> list[dict]:
            return scan_chrome_profiles(paths)

        @work(thread=True, group="account-check", exclusive=True, exit_on_error=False)
        def _check_accounts_worker(self) -> dict:
            return run_check_command(paths)

        @work(thread=True, group="account-activation", exclusive=True, exit_on_error=False)
        def _activate_account_worker(self, name: str) -> str:
            activate(paths, name)
            return name

        @work(thread=True, group="device-login", exclusive=True, exit_on_error=False)
        def _device_login_worker(self, name: str, replace_existing: bool = False, expected_email: str | None = None):
            return login_with_device_code(
                paths,
                name,
                on_code=lambda code: self.call_from_thread(self._show_device_login_code, code),
                on_poll=lambda attempts, elapsed: self.call_from_thread(
                    self._show_device_login_poll, attempts, elapsed
                ),
                cancel_event=self._device_login_cancel_event,
                replace_existing=replace_existing,
                expected_email=expected_email,
            )

        def _run_check_all(self) -> None:
            button = self.query_one("#check-all", Button)
            if self._check_worker is not None:
                self._set_banner("Account check is already running.")
                return
            button.disabled = True
            button.label = "Checking..."
            self._set_banner("Running codex-manager check and fetching latest limits from Codex...")
            self._check_worker = self._check_accounts_worker()

        def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
            if event.state not in {WorkerState.ERROR, WorkerState.SUCCESS, WorkerState.CANCELLED}:
                return
            if event.worker is self._check_worker:
                self._finish_check_worker(event)
            elif event.worker is self._activation_worker:
                self._finish_activation_worker(event)
            elif event.worker is self._device_login_worker_ref:
                self._finish_device_login_worker(event)
            elif event.worker is self._chrome_scan_worker:
                self._finish_chrome_scan_worker(event)

        def _finish_chrome_scan_worker(self, event: Worker.StateChanged) -> None:
            button = self.query_one("#scan-chrome-profiles", Button)
            button.disabled = False
            button.label = "Scan Profiles"
            self._chrome_scan_worker = None
            status = self.query_one("#chrome-scan-status", Static)
            if event.state == WorkerState.SUCCESS:
                result = event.worker.result
                self._chrome_profiles = result if isinstance(result, list) else []
                self._refresh_chrome_profile_table()
                counts = {outcome: sum(1 for item in self._chrome_profiles if item.get("outcome") == outcome) for outcome in ("signed_out", "partial", "signed_in", "error")}
                status.update(
                    f"Profiles: {len(self._chrome_profiles)} | signed out: {counts['signed_out']} | partial: {counts['partial']} | signed in: {counts['signed_in']} | errors: {counts['error']}"
                )
                self._set_banner("Chrome profile scan finished.")
            elif event.state == WorkerState.ERROR:
                error = event.worker.error
                message = str(error) if error else "Chrome profile scan failed."
                status.update(message)
                self._set_banner(message)
            else:
                status.update("Chrome profile scan cancelled.")
                self._set_banner("Chrome profile scan cancelled.")

        def _finish_activation_worker(self, event: Worker.StateChanged) -> None:
            name = self._activation_target
            self._activation_worker = None
            self._activation_target = None

            if event.state == WorkerState.SUCCESS:
                activated = event.worker.result
                self._refresh_dashboard_data(rerender_chart=False)
                if isinstance(activated, str):
                    self._select_account_row(activated)
                    self.query_one("#chart-account", Select).value = activated
                self._set_banner(
                    f"Primary account switched to {activated or name}. Restart Codex if you need the new auth picked up immediately."
                )
            elif event.state == WorkerState.ERROR:
                error = event.worker.error
                self._set_banner(str(error) if error else f"Could not switch to {name or 'the selected account'}.")
            else:
                self._set_banner("Account switch cancelled.")

            self._update_account_action_state()
            self._focus_account_table()

        def _finish_check_worker(self, event: Worker.StateChanged) -> None:
            button = self.query_one("#check-all", Button)
            button.disabled = False
            button.label = "↻ Check All"
            self._check_worker = None

            if event.state == WorkerState.SUCCESS:
                summary = event.worker.result
                self._refresh_dashboard_data()
                if summary.get("returncode") == 0:
                    self._set_banner("Check finished; dashboard refreshed.")
                else:
                    self._set_banner("Check finished with accounts needing login; dashboard refreshed.")
            elif event.state == WorkerState.ERROR:
                error = event.worker.error
                self._set_banner(str(error) if error else "Account check failed.")
            else:
                self._set_banner("Account check cancelled.")

            self._update_account_action_state()
            if self._check_requested_after_current:
                self._check_requested_after_current = False
                self._run_check_all()
                return
            self._focus_account_table()

        def _finish_device_login_worker(self, event: Worker.StateChanged) -> None:
            if event.state == WorkerState.SUCCESS:
                result = event.worker.result
                action = "Replaced" if result.replaced else "Imported"
                self.query_one("#device-status", Static).update(
                    f"{action} {result.name} for {result.email or 'unknown email'}."
                )
                self.query_one("#device-login-link", Static).update("Verification finished.")
                self.query_one("#device-login-code", Static).update("Code: complete")
                self.query_one("#copy-device-code", Button).disabled = True
                self.query_one("#add-name", Input).disabled = False
                self.query_one("#add-name", Input).value = ""
                self.query_one("#add-manual-name", Input).value = ""
                self.query_one("#add-auth-path", Input).value = ""
                self._refresh_dashboard_data(rerender_chart=False)
                self._select_account_row(result.name)
                self._switch_tab("accounts")
                self._schedule_background_check(
                    banner_message=f"{action} account {result.name} from ChatGPT device login. Running background check..."
                )
            elif event.state == WorkerState.ERROR:
                error = event.worker.error
                message = str(error) if error else "Device login failed."
                if "cancelled" in message.lower():
                    self._reset_device_login_view()
                    self._set_banner("Device login cancelled.")
                else:
                    button = self.query_one("#start-device-login", Button)
                    button.disabled = False
                    button.label = "Start Device Login"
                    self.query_one("#add-name", Input).disabled = False
                    self.query_one("#device-form-step", Vertical).remove_class("hidden")
                    self.query_one("#cancel-device-login", Button).disabled = True
                    self.query_one("#device-status-panel", Vertical).remove_class("hidden")
                    self.query_one("#device-status", Static).update(message)
                    self._set_banner("Device login failed.")
            else:
                self._reset_device_login_view()
                self._set_banner("Device login cancelled.")

            self.query_one("#add-name", Input).disabled = False
            self._device_login_worker_ref = None
            self._device_login_cancel_event = None

        def _show_device_login_code(self, code: DeviceLoginCode) -> None:
            self._device_login_code = code
            self.query_one("#device-form-step", Vertical).add_class("hidden")
            self.query_one("#device-status-panel", Vertical).remove_class("hidden")
            self.query_one("#device-login-link", Static).update(code.verification_url)
            self.query_one("#device-login-code", Static).update(f"Code: {code.user_code}")
            self.query_one("#copy-device-code", Button).disabled = False
            self.query_one("#device-status", Static).update(
                "\n".join(
                    [
                        "Waiting for ChatGPT approval...",
                        "",
                        "Open the verification URL, submit the code, then leave this screen open.",
                        "",
                        "Waiting for Codex to receive tokens...",
                    ]
                )
            )

        def _show_device_login_poll(self, attempts: int, elapsed_seconds: float) -> None:
            self.query_one("#device-status", Static).update(
                f"Polling ChatGPT login... attempt {attempts}, elapsed {int(elapsed_seconds)}s"
            )

        def _copy_device_code(self) -> None:
            if not self._device_login_code:
                self._set_banner("No device code is available yet.")
                return
            copied_with_system_clipboard = copy_text_to_clipboard(self._device_login_code.user_code)
            self.copy_to_clipboard(self._device_login_code.user_code)
            if copied_with_system_clipboard:
                self._set_banner("Device code copied to clipboard.")
            else:
                self._set_banner("Device code sent to terminal clipboard.")

        def _copy_account_email(self, name: str) -> None:
            active = load_state(paths).get("active")
            row = describe_account(paths, name, active)
            email = row.get("email") or ""
            if email == "unknown":
                self._set_banner(f"{name} does not have a known email to copy.")
                return
            copied_with_system_clipboard = copy_text_to_clipboard(email)
            self.copy_to_clipboard(email)
            if copied_with_system_clipboard:
                self._set_banner(f"Copied {email} to clipboard.")
            else:
                self._set_banner(f"Sent {email} to terminal clipboard.")

        def _cancel_device_login(self) -> None:
            if self._device_login_cancel_event is None or self._device_login_worker_ref is None:
                self._reset_device_login_view()
                return
            self._device_login_cancel_event.set()
            self.query_one("#cancel-device-login", Button).disabled = True
            self.query_one("#device-status", Static).update("Cancelling device login...")
            self._set_banner("Cancelling device login...")

        def _start_device_login(self) -> None:
            name = self.query_one("#add-name", Input).value.strip()
            if not name:
                self.query_one("#device-status", Static).update("Account name is required.")
                return
            self._begin_device_login(name)

        def _begin_device_login(
            self,
            name: str,
            *,
            replace_existing: bool = False,
            expected_email: str | None = None,
        ) -> None:
            if self._device_login_worker_ref is not None:
                self._set_banner("Device login is already running.")
                return
            self._device_login_code = None
            self._device_login_cancel_event = threading.Event()
            self.query_one("#add-name", Input).value = name
            self.query_one("#add-name", Input).disabled = replace_existing
            self.query_one("#device-form-step", Vertical).add_class("hidden")
            self.query_one("#device-status-panel", Vertical).remove_class("hidden")
            button = self.query_one("#start-device-login", Button)
            button.disabled = True
            button.label = "Relogging..." if replace_existing else "Waiting..."
            self.query_one("#device-login-link", Static).update("Requesting verification URL...")
            self.query_one("#device-login-code", Static).update("Code: pending...")
            self.query_one("#copy-device-code", Button).disabled = True
            self.query_one("#device-status", Static).update("Requesting a ChatGPT device code...")
            self.query_one("#cancel-device-login", Button).disabled = False
            self._device_login_worker_ref = self._device_login_worker(
                name,
                replace_existing,
                expected_email,
            )

        def _start_relogin_selected_account(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            if self._device_login_worker_ref is not None:
                self._set_banner("Device login is already running.")
                return
            try:
                expected_email = account_metadata(read_auth(account_path(paths, name))).get("email")
            except ManagerError as exc:
                self._set_banner(f"Cannot relogin {name}: {exc}")
                return
            if not expected_email:
                self._set_banner(f"Cannot relogin {name}: stored account email is unknown.")
                return

            def start_relogin() -> None:
                self._set_add_method("device", focus_input=False)
                self.query_one("#add-name", Input).value = name
                self._begin_device_login(name, replace_existing=True, expected_email=expected_email)

            self._switch_tab("add", after=start_relogin)
            self._set_banner(f"Starting relogin for {name}...")

        def _delete_selected_account(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            self.push_screen(DeleteConfirmModal(name), self._delete_account_after_confirm)

        def _rename_selected_account(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            self.push_screen(RenameAccountModal(name), self._rename_account_after_confirm)

        def _rename_account_after_confirm(self, result: tuple[str, str] | None) -> None:
            if not result:
                self._set_banner("Rename cancelled.")
                return
            old_name, new_name = result
            try:
                renamed = rename_account(paths, old_name, new_name)
            except ManagerError as exc:
                self._set_banner(str(exc))
                return
            self._refresh_dashboard_data(rerender_chart=False, preserve_selected=False)
            self._select_account_row(renamed)
            self._set_banner(f"Renamed {old_name} to {renamed}.")

        def _delete_account_after_confirm(self, name: str | None) -> None:
            if not name:
                self._set_banner("Delete cancelled.")
                return
            active = load_state(paths).get("active")
            if name == active:
                self._set_banner("Cannot delete the active account. Activate another account first.")
                return
            try:
                delete_account(paths, name)
            except ManagerError as exc:
                self._set_banner(str(exc))
                return
            self._refresh_dashboard_data(rerender_chart=False)
            self._set_banner(f"Deleted {name}.")

        def _submit_add(self) -> None:
            name = self.query_one("#add-manual-name", Input).value.strip()
            auth_path = self.query_one("#add-auth-path", Input).value.strip()
            if not name or not auth_path:
                self.query_one("#manual-status", Static).update("Name and auth.json path are both required.")
                return
            try:
                meta = add_account(paths, name, auth_path)
            except ManagerError as exc:
                self.query_one("#manual-status", Static).update(str(exc))
                return
            self.query_one("#manual-status", Static).update(
                f"Imported {name} for {meta.get('email') or 'unknown email'}."
            )
            self.query_one("#add-manual-name", Input).value = ""
            self.query_one("#add-auth-path", Input).value = ""
            self._refresh_dashboard_data(rerender_chart=False)
            self._select_account_row(name)
            self._switch_tab("accounts")
            self._schedule_background_check(
                banner_message=f"Imported account {name}. Running background check..."
            )

        def _push_selected_account_to_chart(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            chart_select = self.query_one("#chart-account", Select)
            chart_select.value = name
            self._switch_tab("charts", after=self._render_chart_if_possible)

        def _apply_chart_defaults(self) -> None:
            if chart and chart.account:
                chart_accounts = available_history_accounts(paths) or list_accounts(paths)
                if chart.account in chart_accounts:
                    self.query_one("#chart-account", Select).value = chart.account
            self._render_chart_if_possible()

        def _render_chart_if_possible(self) -> None:
            if self._selected_chart_account():
                self._render_chart()

        def _render_chart(self) -> None:
            account = self._selected_chart_account()
            range_value = self.query_one("#chart-range", Input).value.strip()
            unit = self.query_one("#chart-unit", Select).value
            window_offset_value = self.query_one("#chart-window-offset", Input).value.strip()
            timezone_value = self.query_one("#chart-timezone", Input).value.strip() or "local"
            if not account or not isinstance(unit, str):
                self.query_one("#chart-status", Static).update("Pick a history account to render a chart.")
                return
            try:
                amount = int(range_value)
                window_offset = int(window_offset_value or "0")
            except ValueError:
                self.query_one("#chart-status", Static).update("Window size and lookback offset must be integers.")
                return
            kwargs = {"hours": amount} if unit == "hours" else {"days": amount}
            try:
                window = build_history_window(
                    paths,
                    account=account,
                    window_offset=window_offset,
                    timezone=timezone_value,
                    **kwargs,
                )
            except ManagerError as exc:
                self.query_one("#chart-status", Static).update(str(exc))
                return
            plot = self.query_one("#chart-plot", PlotextPlot)
            plt = plot.plt
            plot.theme = "clear"
            plt.clear_figure()
            plt.theme("clear")
            plt.canvas_color("#282a36")
            plt.axes_color("#44475a")
            plt.ticks_color("#f8f8f2")
            plt.ticks_style("bold")
            # Current samples are weekly in secondary; retain historical charts
            # recorded before the weekly-only schema change.
            all_points = window.secondary_points or window.primary_points
            x = list(range(len(all_points)))
            secondary_values = [point[1] for point in all_points]
            secondary_x = list(range(len(all_points)))
            secondary_color = "magenta"
            secondary_marker_color = "yellow"
            if secondary_values:
                plt.scatter(secondary_x, secondary_values, color=secondary_marker_color, marker="hd")
                plt.plot(secondary_x, secondary_values, color=secondary_color, marker="braille")
            plt.title(
                f"{window.account}  window={window.window_label}  offset={window.offset_label}  {window.timezone_label}"
            )
            plt.ylabel("Remaining %")
            plt.xlabel("Sample Time")
            plt.ylim(0, 100)
            plt.plotsize(None, 22)
            if len(all_points) <= 8:
                tick_positions = x
            else:
                step = max(1, len(all_points) // 6)
                tick_positions = sorted(set([0, *range(step, len(all_points), step), len(all_points) - 1]))
            tick_labels = [all_points[index][0].strftime("%m-%d %H:%M") for index in tick_positions]
            plt.xticks(tick_positions, tick_labels)
            plt.xfrequency(len(tick_positions))
            plot.refresh()
            legend = Text()
            legend.append("weekly remaining", style="bold magenta")
            legend.append("   ")
            legend.append("weekly samples", style="bold yellow")
            self.query_one("#chart-legend", Static).update(legend)
            self.query_one("#chart-status", Static).update(
                f"weekly line=magenta with amber markers, samples={len(all_points)}, window={window.window_label}, lookback={window.offset_label}, tz={window.timezone_label}"
            )

        def _set_banner(self, message: str) -> None:
            self.notify(message, timeout=3.5)

    ManagerApp().run()
