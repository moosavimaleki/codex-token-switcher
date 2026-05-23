from __future__ import annotations

from dataclasses import dataclass

from .errors import ManagerError
from .history import available_history_accounts, build_history_window
from .paths import Paths, list_accounts
from .storage import load_state
from .views import describe_account


@dataclass
class ChartDefaults:
    account: str | None = None
    hours: int | None = None
    days: int | None = None
    window_offset: int = 0
    timezone: str | None = None


def run_textual_dashboard(paths: Paths, *, initial_tab: str = "accounts", chart: ChartDefaults | None = None) -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Button, Footer, Header, Input, Select, Static, TabbedContent, TabPane
        from textual_plotext import PlotextPlot
    except ImportError as exc:
        raise ManagerError(
            "Textual UI dependencies are missing. Re-run setup.sh or install requirements.txt for the same python interpreter."
        ) from exc

    from .commands.accounts import activate, add_account, delete_account

    class ManagerApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
            background: #282a36;
            color: #f8f8f2;
        }
        #banner {
            height: 3;
            padding: 1 2;
            background: #44475a;
            color: #f8f8f2;
            text-style: bold;
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
        #accounts-layout, #chart-layout {
            height: 1fr;
        }
        #accounts-left, #chart-controls, #add-form {
            width: 38;
        }
        #accounts-right, #chart-panel {
            width: 1fr;
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
        #account-table, #account-detail, #chart-status, #add-status {
            height: 1fr;
        }
        #account-table, #chart-status, #add-status {
            color: #bd93f9;
        }
        Button {
            margin-right: 1;
            min-width: 14;
        }
        #chart-plot {
            height: 1fr;
            background: #282a36;
        }
        """

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("ctrl+r", "refresh_data", "Refresh"),
            ("ctrl+1", "switch_accounts", "Accounts"),
            ("ctrl+2", "switch_add", "Add"),
            ("ctrl+3", "switch_chart", "Chart"),
        ]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(
                "Codex Manager  Dracula workspace for account switching, imports, and rate-limit history.",
                id="banner",
            )
            with TabbedContent(initial=initial_tab):
                with TabPane("Accounts", id="accounts"):
                    with Horizontal(id="accounts-layout"):
                        with Vertical(id="accounts-left", classes="panel"):
                            yield Static("Account Browser", classes="title")
                            yield Select([], prompt="Choose an account", allow_blank=True, id="account-select")
                            yield Static("", id="account-table")
                            with Horizontal(classes="button-row"):
                                yield Button("Activate", id="activate")
                                yield Button("Delete", id="delete", variant="error")
                            with Horizontal(classes="button-row"):
                                yield Button("Add Account", id="open-add", variant="success")
                                yield Button("Open Chart", id="open-chart", variant="primary")
                        with Vertical(id="accounts-right", classes="panel"):
                            yield Static("Selected Account", classes="title")
                            yield Static("", id="account-detail")
                with TabPane("Add", id="add"):
                    with Vertical(id="add-form", classes="panel"):
                        yield Static("Import A Healthy auth.json", classes="title")
                        yield Input(placeholder="Account name", id="add-name")
                        yield Input(placeholder="/path/to/auth.json", id="add-auth-path")
                        with Horizontal(classes="button-row"):
                            yield Button("Import Account", id="submit-add", variant="success")
                            yield Button("Back To Accounts", id="back-accounts")
                        yield Static("", id="add-status")
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
                            yield Static("", id="chart-status")
                        with Vertical(id="chart-panel", classes="panel"):
                            yield Static("Rate Limit History", classes="title")
                            yield PlotextPlot(id="chart-plot")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_accounts()
            self._refresh_chart_accounts()
            self._apply_chart_defaults()

        def action_refresh_data(self) -> None:
            self._refresh_accounts()
            self._refresh_chart_accounts()
            self._render_chart_if_possible()
            self._set_banner("Reloaded account state and cached history.")

        def action_switch_accounts(self) -> None:
            self.query_one(TabbedContent).active = "accounts"

        def action_switch_add(self) -> None:
            self.query_one(TabbedContent).active = "add"

        def action_switch_chart(self) -> None:
            self.query_one(TabbedContent).active = "charts"
            self._render_chart_if_possible()

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id == "account-select":
                self._update_account_detail(event.value)
            elif event.select.id == "chart-account":
                self._render_chart_if_possible()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id
            if button_id == "activate":
                self._activate_selected_account()
            elif button_id == "delete":
                self._delete_selected_account()
            elif button_id == "open-add":
                self.action_switch_add()
            elif button_id == "open-chart":
                self._push_selected_account_to_chart()
            elif button_id == "submit-add":
                self._submit_add()
            elif button_id == "back-accounts":
                self.action_switch_accounts()
            elif button_id == "render-chart":
                self._render_chart()
            elif button_id == "chart-from-account":
                self._push_selected_account_to_chart()

        def _selected_account(self) -> str | None:
            value = self.query_one("#account-select", Select).value
            return value if isinstance(value, str) else None

        def _selected_chart_account(self) -> str | None:
            value = self.query_one("#chart-account", Select).value
            return value if isinstance(value, str) else None

        def _refresh_accounts(self) -> None:
            active = load_state(paths).get("active")
            names = list_accounts(paths)
            select = self.query_one("#account-select", Select)
            options = [(self._account_option_label(name, active), name) for name in names]
            select.set_options(options)
            if names:
                current = self._selected_account()
                target = current if current in names else active if active in names else names[0]
                select.value = target
                self._update_account_table(active)
                self._update_account_detail(target)
                self._set_banner(
                    f"Primary account: {active}" if active else "No primary account selected yet."
                )
            else:
                self.query_one("#account-table", Static).update("No accounts tracked yet.")
                self.query_one("#account-detail", Static).update("Import an auth.json to get started.")
                self._set_banner("No primary account selected yet.")

        def _refresh_chart_accounts(self) -> None:
            active = load_state(paths).get("active")
            names = available_history_accounts(paths)
            if not names:
                names = list_accounts(paths)
            select = self.query_one("#chart-account", Select)
            select.set_options([(self._account_option_label(name, active), name) for name in names])
            if names and self._selected_chart_account() not in names:
                select.value = names[0]

        def _update_account_table(self, active: str | None) -> None:
            rows = [describe_account(paths, name, active) for name in list_accounts(paths)]
            if not rows:
                self.query_one("#account-table", Static).update("No accounts tracked yet.")
                return
            lines = ["Primary  Name             State            Expires      Limits"]
            for row in rows:
                marker = "PRIMARY" if row["name"] == active else "       "
                lines.append(
                    f"{marker:<7} {row['name']:<16} {row['state']:<15} {row['expires']:<11} {row['limits'][:30]}"
                )
            self.query_one("#account-table", Static).update("\n".join(lines))

        def _account_option_label(self, name: str, active: str | None) -> str:
            return f"● {name}  [primary]" if name == active else f"○ {name}"

        def _update_account_detail(self, name: object) -> None:
            if not isinstance(name, str):
                self.query_one("#account-detail", Static).update("Pick an account to inspect its auth and limit summary.")
                return
            active = load_state(paths).get("active")
            row = describe_account(paths, name, active)
            primary_line = "Primary Account: yes" if row["name"] == active else "Primary Account: no"
            detail = [
                f"Account: {row['name']}",
                primary_line,
                f"Email: {row['email']}",
                f"Account ID: {row['account']}",
                f"State: {row['state']}",
                f"Expires In: {row['expires']}",
                f"Limits: {row['limits']}",
                "",
                row["reason"],
            ]
            self.query_one("#account-detail", Static).update("\n".join(detail))

        def _activate_selected_account(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            activate(paths, name)
            self._refresh_accounts()
            self._set_banner(f"Primary account switched to {name}. Restart Codex if you need the new auth picked up immediately.")

        def _delete_selected_account(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            delete_account(paths, name)
            self._refresh_accounts()
            self._refresh_chart_accounts()
            self._set_banner(f"Deleted {name}.")

        def _submit_add(self) -> None:
            name = self.query_one("#add-name", Input).value.strip()
            auth_path = self.query_one("#add-auth-path", Input).value.strip()
            if not name or not auth_path:
                self.query_one("#add-status", Static).update("Name and auth.json path are both required.")
                return
            try:
                meta = add_account(paths, name, auth_path)
            except ManagerError as exc:
                self.query_one("#add-status", Static).update(str(exc))
                return
            self.query_one("#add-status", Static).update(
                f"Imported {name} for {meta.get('email') or 'unknown email'}."
            )
            self.query_one("#add-name", Input).value = ""
            self._refresh_accounts()
            self._refresh_chart_accounts()
            self.action_switch_accounts()
            self._set_banner(f"Imported account {name}.")

        def _push_selected_account_to_chart(self) -> None:
            name = self._selected_account()
            if not name:
                self._set_banner("Select an account first.")
                return
            chart_select = self.query_one("#chart-account", Select)
            chart_select.value = name
            self.action_switch_chart()
            self._render_chart_if_possible()

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
            plt.clear_figure()
            plt.theme("clear")
            plt.canvas_color("#282a36")
            plt.axes_color("#44475a")
            plt.ticks_color("#f8f8f2")
            plt.ticks_style("bold")
            all_points = window.primary_points or window.secondary_points
            x = list(range(len(all_points)))
            primary_values = [point[1] for point in window.primary_points]
            secondary_values = [point[1] for point in window.secondary_points]
            primary_x = list(range(len(window.primary_points)))
            secondary_x = list(range(len(window.secondary_points)))
            if primary_values:
                plt.plot(primary_x, primary_values, color="#8be9fd", marker="dot")
            if secondary_values:
                plt.scatter(secondary_x, secondary_values, color="#ffb86c", marker="hd")
                plt.plot(secondary_x, secondary_values, color="#ff79c6", marker="braille")
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
            self.query_one("#chart-status", Static).update(
                f"5h line=cyan dots, weekly line=magenta with amber markers, samples={len(all_points)}, window={window.window_label}, lookback={window.offset_label}, tz={window.timezone_label}"
            )

        def _set_banner(self, message: str) -> None:
            self.query_one("#banner", Static).update(message)

    ManagerApp().run()
