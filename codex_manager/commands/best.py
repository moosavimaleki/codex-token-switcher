from __future__ import annotations

from dataclasses import dataclass

from ..auth import account_metadata, read_auth
from ..codex.limits import format_rate_limits_summary
from ..errors import ManagerError
from ..paths import Paths, account_path, list_accounts, status_path
from ..recommendation import AccountRecommendation, account_rank_sort_key, account_recommendations
from ..storage import read_json
from ..views import effective_plan
from .accounts import activate


@dataclass(frozen=True)
class BestAccountRow:
    name: str
    plan: str
    state: str
    limits: str
    remaining: float | None
    recommendation: AccountRecommendation

    @property
    def available(self) -> bool:
        return self.state not in {"needs_login", "error"} and self.remaining is not None and self.remaining > 0.0


def best_account_rows(paths: Paths) -> list[BestAccountRow]:
    names = list_accounts(paths)
    recommendations = account_recommendations(paths, names)
    rows: list[BestAccountRow] = []
    for name in names:
        try:
            auth = read_auth(account_path(paths, name))
            metadata = account_metadata(auth)
        except ManagerError:
            metadata = {}
        try:
            status = read_json(status_path(paths, name))
        except ManagerError:
            status = {}
        state = str(status.get("state") or "unknown")
        rate_limits = status.get("rate_limits")
        rows.append(
            BestAccountRow(
                name=name,
                plan=effective_plan(metadata.get("plan"), rate_limits, state),
                state=state,
                limits=format_rate_limits_summary(rate_limits, compact=True),
                remaining=recommendations[name].weekly_remaining,
                recommendation=recommendations[name],
            )
        )
    return sorted(rows, key=lambda row: account_rank_sort_key(row.plan, row.recommendation.score, row.name))


def print_best_account_rows(rows: list[BestAccountRow]) -> None:
    print("  Rank  Account            Plan     Limits                 State        Decision")
    eligible_rank = 0
    for row in rows:
        rank = "-"
        decision = row.recommendation.label
        if row.available:
            eligible_rank += 1
            rank = str(eligible_rank)
            decision = "SELECT" if eligible_rank == 1 else row.recommendation.label
        print(
            f"  {rank:>4}  {row.name:<18} {row.plan.upper():<8} {row.limits[:22]:<22} "
            f"{row.state:<12} {decision}"
        )


def select_best_account(rows: list[BestAccountRow]) -> BestAccountRow | None:
    return next((row for row in rows if row.available), None)


def cmd_best(_args) -> int:
    paths = Paths()
    rows = best_account_rows(paths)
    if not rows:
        raise ManagerError("no managed accounts; add an account first")

    print_best_account_rows(rows)
    selected = select_best_account(rows)
    if selected is None:
        if rows and all(row.remaining is not None and row.remaining <= 0.0 for row in rows):
            raise ManagerError("all cached account limits are exhausted")
        raise ManagerError("no account has an available cached limit; run codex-manager check")

    activate(paths, selected.name)
    print(f"Activated best available account: {selected.name} ({selected.limits}).")
    return 0
