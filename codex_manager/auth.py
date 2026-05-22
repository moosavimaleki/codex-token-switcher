from __future__ import annotations

import base64
import datetime as dt
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .constants import CLIENT_ID, DEFAULT_LAST_REFRESH_MAX_AGE, DEFAULT_REFRESH_MARGIN, REFRESH_URL
from .errors import ManagerError
from .storage import read_json
from .time_utils import human_delta, iso_now, parse_datetime, utcnow


def read_auth(path: Path) -> dict[str, Any]:
    auth = read_json(path)
    tokens = auth.get("tokens")
    if auth.get("OPENAI_API_KEY"):
        raise ManagerError(f"{path} looks like API-key auth; codex-manager expects ChatGPT token auth")
    if not isinstance(tokens, dict):
        raise ManagerError(f"{path} does not contain tokens")
    if not tokens.get("refresh_token"):
        raise ManagerError(f"{path} is missing tokens.refresh_token")
    return auth


def b64url_json(segment: str) -> dict[str, Any] | None:
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode((segment + padding).encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def jwt_claims(jwt: str | None) -> dict[str, Any] | None:
    if not isinstance(jwt, str):
        return None
    parts = jwt.split(".")
    if len(parts) < 2:
        return None
    return b64url_json(parts[1])


def access_expiry(auth: dict[str, Any]) -> dt.datetime | None:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    claims = jwt_claims(tokens.get("access_token"))
    exp = claims.get("exp") if claims else None
    if isinstance(exp, (int, float)):
        return dt.datetime.fromtimestamp(exp, tz=dt.timezone.utc)
    return None


def account_metadata(auth: dict[str, Any]) -> dict[str, str | None]:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    id_token = tokens.get("id_token")
    info = id_token if isinstance(id_token, dict) else None
    if info is None:
        info = jwt_claims(id_token) if isinstance(id_token, str) else None
    info = info or {}
    auth_ns = info.get("https://api.openai.com/auth")
    profile_ns = info.get("https://api.openai.com/profile")
    if not isinstance(auth_ns, dict):
        auth_ns = {}
    if not isinstance(profile_ns, dict):
        profile_ns = {}
    return {
        "email": info.get("email") or profile_ns.get("email"),
        "account_id": tokens.get("account_id") or info.get("chatgpt_account_id") or auth_ns.get("chatgpt_account_id"),
        "plan": info.get("chatgpt_plan_type") or auth_ns.get("chatgpt_plan_type"),
    }


def auth_identity(auth: dict[str, Any]) -> dict[str, str]:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    id_token = tokens.get("id_token")
    info = id_token if isinstance(id_token, dict) else None
    if info is None:
        info = jwt_claims(id_token) if isinstance(id_token, str) else None
    info = info or {}
    meta = account_metadata(auth)

    identity: dict[str, str] = {}
    for key, value in {
        "account_id": meta.get("account_id"),
        "email": meta.get("email"),
        "subject": info.get("sub"),
    }.items():
        if isinstance(value, str) and value:
            identity[key] = value
    return identity


def same_account_identity(expected: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str]:
    expected_identity = auth_identity(expected)
    candidate_identity = auth_identity(candidate)
    if not expected_identity or not candidate_identity:
        return False, "could not verify account identity"

    shared_keys = sorted(set(expected_identity) & set(candidate_identity))
    if not shared_keys:
        return False, "no shared identity fields to compare"

    mismatches = [
        key for key in shared_keys
        if expected_identity[key] != candidate_identity[key]
    ]
    if mismatches:
        key_list = ", ".join(mismatches)
        return False, f"identity mismatch on {key_list}"

    return True, f"matched on {', '.join(shared_keys)}"


def should_refresh(auth: dict[str, Any]) -> tuple[bool, str]:
    exp = access_expiry(auth)
    if exp is not None:
        remaining = exp - utcnow()
        if remaining <= DEFAULT_REFRESH_MARGIN:
            return True, f"access token expires in {human_delta(remaining)}"
        return False, f"access token valid for {human_delta(remaining)}"

    last_refresh = parse_datetime(auth.get("last_refresh"))
    if last_refresh is None:
        return True, "missing last_refresh and unreadable access_token exp"
    age = utcnow() - last_refresh
    if age >= DEFAULT_LAST_REFRESH_MAX_AGE:
        return True, f"last_refresh is {human_delta(age)} old"
    return False, f"last_refresh age {human_delta(age)}"


def refresh_auth(auth: dict[str, Any], proxy_url: str | None = None) -> dict[str, Any]:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else None
    if not tokens or not tokens.get("refresh_token"):
        raise ManagerError("missing refresh token")

    body = json.dumps({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode("utf-8")
    req = urllib.request.Request(
        REFRESH_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            response = opener.open(req, timeout=30)
        else:
            response = urllib.request.urlopen(req, timeout=30)
        with response as resp:
            raw = resp.read()
            refreshed = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise ManagerError(f"refresh failed: HTTP {exc.code}: {error_body}") from exc
    except Exception as exc:
        raise ManagerError(f"refresh failed: {exc}") from exc

    if not isinstance(refreshed, dict):
        raise ManagerError("refresh response was not an object")

    new_auth = json.loads(json.dumps(auth))
    new_tokens = new_auth.setdefault("tokens", {})
    if refreshed.get("id_token"):
        new_tokens["id_token"] = refreshed["id_token"]
    if refreshed.get("access_token"):
        new_tokens["access_token"] = refreshed["access_token"]
    if refreshed.get("refresh_token"):
        new_tokens["refresh_token"] = refreshed["refresh_token"]
    new_auth["last_refresh"] = iso_now()
    return new_auth
