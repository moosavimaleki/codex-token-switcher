from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from yt_dlp.cookies import _extract_chrome_cookies

from .errors import ManagerError


CHATGPT_HOST = "chatgpt.com"
SESSION_URL = f"https://{CHATGPT_HOST}/api/auth/session"
DEVICES_URL = f"https://{CHATGPT_HOST}/backend-api/accounts/sessions"
REVOKE_URL = f"{DEVICES_URL}/revoke"
ACCOUNT_SWITCH_STORAGE_KEY = b"oai/apps/accountSwitchSessions"


@dataclass(frozen=True)
class ChromeProfile:
    name: str
    cookie_db: Path
    directory: str = ""
    display_name: str = ""
    chrome_root: Path | None = None

    @property
    def label(self) -> str:
        display_name = self.display_name or self.directory or self.name
        return f"{display_name} ({self.directory})" if self.directory else display_name


class ProfileNotSignedIn(ManagerError):
    pass


def discover_chrome_profiles(chrome_root: str | None = None) -> list[ChromeProfile]:
    roots = [Path(chrome_root).expanduser()] if chrome_root else [
        Path.home() / ".config/google-chrome",
        Path.home() / ".config/google-chrome-beta",
        Path.home() / ".config/chromium",
    ]
    profiles: list[ChromeProfile] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        info_cache = _profile_info_cache(root)
        for profile_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            if profile_dir.name != "Default" and not profile_dir.name.startswith("Profile "):
                continue
            cookie_db = profile_dir / "Cookies"
            if not cookie_db.is_file():
                cookie_db = profile_dir / "Network" / "Cookies"
            if cookie_db.is_file() and cookie_db not in seen:
                metadata = info_cache.get(profile_dir.name, {})
                display_name = metadata.get("name") if isinstance(metadata.get("name"), str) else _profile_display_name(profile_dir)
                profiles.append(ChromeProfile(
                    f"{root.name}/{profile_dir.name}",
                    cookie_db,
                    directory=profile_dir.name,
                    display_name=display_name,
                    chrome_root=root,
                ))
                seen.add(cookie_db)
    return profiles


def _profile_info_cache(root: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads((root / "Local State").read_text(encoding="utf-8"))
        cache = payload.get("profile", {}).get("info_cache", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _profile_display_name(profile_dir: Path) -> str:
    try:
        payload = json.loads((profile_dir / "Preferences").read_text(encoding="utf-8"))
        profile = payload.get("profile") if isinstance(payload, dict) else None
        name = profile.get("name") if isinstance(profile, dict) else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return profile_dir.name
    return name.strip() if isinstance(name, str) and name.strip() else profile_dir.name


class _SilentCookieLogger:
    def debug(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass


def load_chatgpt_cookies(profile: ChromeProfile) -> CookieJar:
    with tempfile.TemporaryDirectory(prefix="codex-manager-chrome-") as temp_dir:
        copied_profile = Path(temp_dir) / "Profile"
        copied_profile.mkdir(mode=0o700)
        copied_db = copied_profile / "Cookies"
        shutil.copy2(profile.cookie_db, copied_db)
        source_jar = _extract_chrome_cookies("chrome", str(copied_profile), None, _SilentCookieLogger())

    jar = CookieJar()
    for cookie in source_jar:
        if cookie.domain.lstrip(".").endswith(CHATGPT_HOST):
            jar.set_cookie(cookie)
    if not list(jar):
        raise ManagerError("no ChatGPT cookies found in this Chrome profile")
    return jar


def chrome_account_email(cookies: CookieJar) -> str | None:
    try:
        raw = next((cookie.value for cookie in cookies if cookie.name == "oai-client-auth-info"), None)
    except TypeError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(unquote(raw))
        user = payload.get("user") if isinstance(payload, dict) else None
        email = user.get("email") if isinstance(user, dict) else None
    except (json.JSONDecodeError, AttributeError):
        return None
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def chatgpt_switch_accounts(profile: ChromeProfile) -> list[str]:
    """Return the account-switcher emails stored by ChatGPT, without retaining tokens."""
    if profile.chrome_root is None or not profile.directory:
        return []
    profile_dir = profile.chrome_root / profile.directory
    storage_dirs = [
        profile_dir / "Local Storage" / "leveldb",
        profile_dir / "IndexedDB" / "https_chatgpt.com_0.indexeddb.leveldb",
    ]

    candidates: list[tuple[int, list[str]]] = []
    decoder = json.JSONDecoder()
    for storage_dir in storage_dirs:
        if not storage_dir.is_dir():
            continue
        for path in storage_dir.iterdir():
            if not path.is_file():
                continue
            try:
                contents = path.read_bytes()
            except OSError:
                continue
            start = 0
            while (offset := contents.find(ACCOUNT_SWITCH_STORAGE_KEY, start)) >= 0:
                start = offset + len(ACCOUNT_SWITCH_STORAGE_KEY)
                value = contents[start:]
                array_start = value.find(b"[")
                if array_start < 0:
                    continue
                try:
                    parsed, _ = decoder.raw_decode(value[array_start:].decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, list):
                    continue
                emails: list[str] = []
                latest_login = 0
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    email = item.get("email")
                    if isinstance(email, str) and email.strip():
                        emails.append(email.strip().lower())
                    logged_in_at = item.get("lastLoggedInAt")
                    if isinstance(logged_in_at, int):
                        latest_login = max(latest_login, logged_in_at)
                if emails:
                    candidates.append((latest_login, sorted(set(emails))))

    if not candidates:
        return []
    return max(candidates, key=lambda item: (item[0], len(item[1])))[1]


class ChatGPTSessionClient:
    def __init__(self, cookies: CookieJar, proxy_url: str | None = None) -> None:
        self._cookies = cookies
        handlers = [HTTPCookieProcessor(cookies)]
        if proxy_url:
            handlers.append(ProxyHandler({"https": proxy_url}))
        self._opener = build_opener(*handlers)

    def _cookie_value(self, name: str) -> str | None:
        for cookie in self._cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def _browser_headers(self, path: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Oai-Language": "en-US",
            "Oai-Session-Id": str(uuid.uuid4()),
            "Origin": f"https://{CHATGPT_HOST}",
            "Referer": f"https://{CHATGPT_HOST}/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "X-OpenAI-Target-Path": path,
            "X-OpenAI-Target-Route": path,
        }
        if device_id := self._cookie_value("oai-did"):
            headers["Oai-Device-Id"] = device_id
        if integrity_state := self._cookie_value("__Secure-oai-is"):
            parts = integrity_state.split(".")
            if len(parts) >= 3:
                headers["X-Oai-Is-Client-Observation"] = f"v1.r.p.{parts[2]}"
        return headers

    def _request(self, url: str, headers: dict[str, str], data: bytes | None = None) -> dict[str, Any]:
        request = Request(url, headers=headers, data=data)
        try:
            with self._opener.open(request, timeout=20) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise ManagerError(f"ChatGPT sessions API returned HTTP {exc.code}") from exc
        except (OSError, URLError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManagerError(f"ChatGPT sessions API request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise ManagerError("ChatGPT sessions API returned an unexpected response")
        return payload

    def _access_token(self) -> str:
        try:
            payload = self._request(SESSION_URL, self._browser_headers("/api/auth/session"))
        except ManagerError as exc:
            if "HTTP 401" in str(exc):
                raise ProfileNotSignedIn("not signed in to ChatGPT") from exc
            raise
        token = payload.get("accessToken") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ProfileNotSignedIn("not signed in to ChatGPT")
        return token

    def devices(self) -> list[dict[str, Any]]:
        token = self._access_token()
        headers = self._browser_headers("/backend-api/accounts/sessions")
        headers["Authorization"] = f"Bearer {token}"
        try:
            payload = self._request(DEVICES_URL, headers)
        except ManagerError as exc:
            if "HTTP 401" in str(exc):
                raise ProfileNotSignedIn("not signed in to ChatGPT") from exc
            raise
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise ManagerError("ChatGPT sessions response did not include devices")
        return [device for device in devices if isinstance(device, dict)]

    def revoke(self, session_id: str) -> None:
        token = self._access_token()
        headers = self._browser_headers("/backend-api/accounts/sessions/revoke")
        headers["Authorization"] = f"Bearer {token}"
        headers["Content-Type"] = "application/json"
        self._request(REVOKE_URL, headers, json.dumps({"session_id": session_id}).encode("utf-8"))


def codex_sessions(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for device in devices:
        applications = device.get("app_sessions")
        client_names = {
            app.get("client_name")
            for app in applications
            if isinstance(app, dict) and isinstance(app.get("client_name"), str)
        } if isinstance(applications, list) else set()
        if "Codex" in client_names:
            selected.append(device)
    return sorted(selected, key=lambda item: int(item.get("last_signed_in_timestamp_second") or 0))


def session_time(device: dict[str, Any]) -> str:
    timestamp = int(device.get("last_signed_in_timestamp_second") or 0)
    if timestamp <= 0:
        return "unknown"
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
