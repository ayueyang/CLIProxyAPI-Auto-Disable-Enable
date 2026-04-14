from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MANAGEMENT_BASE_PATH = "/v0/management"
DEFAULT_MANAGEMENT_URL = f"http://127.0.0.1:8317{MANAGEMENT_BASE_PATH}"
MANAGEMENT_KEY_ENV = "CLIPROXYAPI_MANAGEMENT_KEY"
CODEX_USAGE_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"


@dataclass
class ProbeResult:
    status: str
    reason: str
    http_status: int | None = None
    error_code: str | None = None
    refreshed: bool = False
    reset_at: str | None = None


@dataclass
class ApiCallResponse:
    status_code: int
    headers: dict[str, Any]
    body: Any
    body_text: str


class ManagementApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def now_local() -> datetime:
    return datetime.now().astimezone()


def log(message: str = "") -> None:
    prefix = now_local().strftime("%Y-%m-%d %H:%M:%S")
    if message:
        print(f"[{prefix}] {message}", flush=True)
    else:
        print("", flush=True)


def _strip_inline_comment(value: str) -> str:
    in_quotes = False
    quote_char = ""
    result: list[str] = []
    for char in value:
        if char in ("'", '"'):
            if in_quotes and char == quote_char:
                in_quotes = False
                quote_char = ""
            elif not in_quotes:
                in_quotes = True
                quote_char = char
        if char == "#" and not in_quotes:
            break
        result.append(char)
    return "".join(result).strip()


def _parse_scalar(value: str) -> str:
    stripped = _strip_inline_comment(value).strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        return stripped[1:-1]
    return stripped


def _parse_bool(value: str, default: bool = False) -> bool:
    normalized = _parse_scalar(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return default


def find_config_path() -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [
        here / "config.yaml",
        here.parent / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def read_local_config() -> dict[str, Any]:
    config_path = find_config_path()
    if config_path is None:
        return {}

    host = ""
    port = 8317
    tls_enable = False
    auth_dir = ""
    section = ""

    for raw_line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if indent == 0:
            section = ""
            if line.endswith(":"):
                section = line[:-1].strip()
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key == "host":
                host = _parse_scalar(value)
            elif key == "port":
                try:
                    port = int(_parse_scalar(value))
                except ValueError:
                    port = 8317
            elif key == "auth-dir":
                auth_dir = _parse_scalar(value)
            continue

        if section == "tls" and line.startswith("enable:"):
            _, value = line.split(":", 1)
            tls_enable = _parse_bool(value)

    return {
        "config_path": config_path,
        "host": host,
        "port": port,
        "tls_enable": tls_enable,
        "auth_dir": auth_dir,
    }


def default_auth_dir() -> Path:
    config = read_local_config()
    config_path = config.get("config_path")
    auth_dir_value = config.get("auth_dir")
    if isinstance(config_path, Path) and isinstance(auth_dir_value, str) and auth_dir_value:
        candidate = Path(auth_dir_value)
        if not candidate.is_absolute():
            candidate = config_path.parent / candidate
        return candidate.resolve()

    here = Path(__file__).resolve().parent
    sibling_data = here / "data"
    if sibling_data.exists() and sibling_data.is_dir():
        return sibling_data.resolve()
    return here.resolve()


def default_management_base_url() -> str:
    config = read_local_config()
    host = str(config.get("host") or "").strip()
    if not host or host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    scheme = "https" if config.get("tls_enable") else "http"
    port = int(config.get("port") or 8317)
    return f"{scheme}://{host}:{port}{MANAGEMENT_BASE_PATH}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Watch Codex auth files, query quota via CLIProxyAPI Management API, "
            "and move accounts between root/no_quota/invalid folders."
        )
    )
    parser.add_argument(
        "--auth-dir",
        type=Path,
        default=default_auth_dir(),
        help="Directory that contains CLIProxyAPI auth JSON files.",
    )
    parser.add_argument(
        "--management-base-url",
        default=default_management_base_url(),
        help="CLIProxyAPI Management API base URL, for example http://127.0.0.1:8317/v0/management.",
    )
    parser.add_argument(
        "--management-key",
        default=os.environ.get(MANAGEMENT_KEY_ENV, ""),
        help=f"CLIProxyAPI Management API key. Defaults to env {MANAGEMENT_KEY_ENV}.",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=[],
        help="Specific files to inspect. Relative paths are resolved under --auth-dir.",
    )
    parser.add_argument(
        "--probe-root",
        action="store_true",
        help="Deprecated no-op. Root accounts are always checked safely via the usage endpoint.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write refreshed tokens and move files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe accounts but do not write files or move anything.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan and exit. Default behavior is continuous watching.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=600,
        help="Seconds between scans in watch mode. Default is 600 seconds.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="HTTP timeout in seconds for refresh/probe requests.",
    )
    parser.add_argument(
        "--invalid-dir-name",
        default="invalid_accounts",
        help="Folder name used for invalid accounts.",
    )
    parser.add_argument(
        "--no-quota-dir-name",
        default="no_quota_accounts",
        help="Folder name used for quota-exhausted accounts.",
    )
    args = parser.parse_args()
    args.apply = False if args.dry_run else True
    return args


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def resolve_files(
    auth_dir: Path,
    explicit_files: list[str],
    invalid_dir_name: str,
    no_quota_dir_name: str,
) -> list[Path]:
    if explicit_files:
        paths: list[Path] = []
        for item in explicit_files:
            candidate = Path(item)
            if not candidate.is_absolute():
                candidate = auth_dir / candidate
            paths.append(candidate.resolve())
        return paths

    candidates: set[Path] = set()
    scan_dirs = [
        auth_dir,
        auth_dir / invalid_dir_name,
        auth_dir / no_quota_dir_name,
    ]
    for scan_dir in scan_dirs:
        if not scan_dir.exists() or not scan_dir.is_dir():
            continue
        for path in scan_dir.glob("*.json"):
            if path.is_file():
                candidates.add(path.resolve())
    return sorted(candidates)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone()
    return dt.astimezone()


def get_access_expiry(account: dict[str, Any]) -> datetime | None:
    by_file = parse_datetime(account.get("expired"))
    if by_file is not None:
        return by_file
    token = account.get("access_token")
    if not token:
        return None
    claims = decode_jwt_payload(token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()


def needs_refresh(account: dict[str, Any], skew_seconds: int = 60) -> bool:
    expiry = get_access_expiry(account)
    if expiry is None:
        return not bool(account.get("access_token"))
    return expiry <= now_local() + timedelta(seconds=skew_seconds)


def parse_json_body(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def extract_error(data: dict[str, Any]) -> dict[str, Any]:
    error = data.get("error")
    return error if isinstance(error, dict) else {}


def extract_error_code(data: dict[str, Any]) -> str | None:
    error = extract_error(data)
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and value:
            return value
    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return None


def extract_error_message(data: dict[str, Any]) -> str | None:
    error = extract_error(data)
    message = error.get("message")
    if isinstance(message, str) and message:
        return message
    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    generic = data.get("message")
    if isinstance(generic, str) and generic:
        return generic
    return None


def normalize_api_body(raw_body: Any) -> tuple[str, Any]:
    if raw_body is None:
        return "", None
    if isinstance(raw_body, str):
        body_text = raw_body
        stripped = body_text.strip()
        if not stripped:
            return body_text, None
        try:
            return body_text, json.loads(stripped)
        except ValueError:
            return body_text, body_text
    try:
        return json.dumps(raw_body, ensure_ascii=False), raw_body
    except Exception:
        return str(raw_body), raw_body


def prompt_management_key(existing: str) -> str:
    if existing.strip():
        return existing.strip()
    if not sys.stdin.isatty():
        raise ManagementApiError(
            f"Management key is required. Pass --management-key or set {MANAGEMENT_KEY_ENV}.",
        )
    try:
        value = getpass.getpass("CLIProxyAPI Management key: ").strip()
    except Exception:
        value = input("CLIProxyAPI Management key: ").strip()
    if not value:
        raise ManagementApiError("Management key is required.")
    return value


class ManagementClient:
    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        management_key: str,
        timeout: int,
    ) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")
        self.management_key = management_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.management_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        try:
            return self._session.request(
                method=method,
                url=self._build_url(path),
                headers=self._headers(),
                timeout=timeout or self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ManagementApiError(f"management request failed: {exc}") from exc

    def request_json(
        self,
        method: str,
        path: str,
        *,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self._request(method, path, timeout=timeout, **kwargs)
        data = parse_json_body(response)
        if response.status_code >= 400:
            message = extract_error_message(data) or response.text[:200] or response.reason
            raise ManagementApiError(
                f"{response.status_code} {message}".strip(),
                status_code=response.status_code,
            )
        return data

    def api_call(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        data: str | None = None,
        timeout: int | None = None,
    ) -> ApiCallResponse:
        payload: dict[str, Any] = {"method": method.upper(), "url": url}
        if headers:
            payload["header"] = headers
        if data is not None:
            payload["data"] = data

        body = self.request_json("POST", "/api-call", json=payload, timeout=timeout)
        status_code = int(body.get("status_code") or body.get("statusCode") or 0)
        response_headers = body.get("header") or body.get("headers") or {}
        body_text, normalized_body = normalize_api_body(body.get("body"))
        return ApiCallResponse(
            status_code=status_code,
            headers=response_headers if isinstance(response_headers, dict) else {},
            body=normalized_body,
            body_text=body_text,
        )

    def validate(self) -> None:
        self.request_json("GET", "/config", timeout=min(self.timeout, 15))


def parse_api_data(response: ApiCallResponse) -> dict[str, Any]:
    return response.body if isinstance(response.body, dict) else {}


def get_account_id(account: dict[str, Any]) -> str | None:
    candidates: list[Any] = [
        account.get("account_id"),
        account.get("chatgpt_account_id"),
    ]
    for token_key in ("id_token", "access_token"):
        token = account.get(token_key)
        if isinstance(token, str) and token:
            claims = decode_jwt_payload(token)
            candidates.append(claims.get("chatgpt_account_id"))

    for value in candidates:
        if isinstance(value, str) and value and not value.startswith(("email_", "local_")):
            return value
    return None


def refresh_account(
    account: dict[str, Any],
    management: ManagementClient,
    timeout: int,
) -> tuple[bool, str, str | None]:
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return False, "missing refresh_token", "missing_refresh_token"

    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": "openid profile email offline_access",
    }
    response = management.api_call(
        method="POST",
        url=TOKEN_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(payload, separators=(",", ":")),
        timeout=timeout,
    )

    data = parse_api_data(response)
    if response.status_code != 200:
        error_code = extract_error_code(data)
        error_message = extract_error_message(data) or response.body_text[:200]
        return False, f"refresh failed: {error_message}", error_code

    access_token = data.get("access_token")
    id_token = data.get("id_token")
    new_refresh_token = data.get("refresh_token")
    if not all(isinstance(value, str) and value for value in (access_token, id_token, new_refresh_token)):
        return False, "refresh succeeded but response was missing tokens", "refresh_tokens_missing"

    refreshed_at = now_local()
    account["access_token"] = access_token
    account["id_token"] = id_token
    account["refresh_token"] = new_refresh_token
    account["last_refresh"] = refreshed_at.isoformat(timespec="seconds")

    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)):
        account["expired"] = (
            refreshed_at + timedelta(seconds=int(expires_in))
        ).isoformat(timespec="seconds")

    account["disabled"] = False
    return True, "refresh succeeded", None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _window_reset_iso(window: dict[str, Any]) -> str | None:
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)):
        return datetime.fromtimestamp(int(reset_at), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    if isinstance(reset_at, str):
        parsed = parse_datetime(reset_at)
        if parsed is not None:
            return parsed.isoformat(timespec="seconds")

    reset_after_seconds = window.get("reset_after_seconds")
    seconds = _as_float(reset_after_seconds)
    if seconds is None or seconds <= 0:
        return None
    return (now_local() + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


def _window_exhausted(window: dict[str, Any]) -> tuple[bool, str | None]:
    used_percent = _as_float(window.get("used_percent"))
    if used_percent is None:
        used_percent = _as_float(window.get("usedPercent"))
    limit_reached = window.get("limit_reached")
    if limit_reached is None:
        limit_reached = window.get("limitReached")
    exhausted = False
    if used_percent is not None and used_percent >= 100.0:
        exhausted = True
    elif isinstance(limit_reached, bool) and limit_reached:
        exhausted = True
    return exhausted, _window_reset_iso(window)


def classify_usage_payload(data: dict[str, Any]) -> ProbeResult:
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = data.get("rateLimit")
    if not isinstance(rate_limit, dict):
        plan_type = data.get("plan_type") or data.get("planType")
        if isinstance(plan_type, str) and plan_type:
            return ProbeResult("valid", f"usage fetch succeeded | plan={plan_type}", http_status=200)
        return ProbeResult("valid", "usage fetch succeeded", http_status=200)

    primary = rate_limit.get("primary_window")
    if not isinstance(primary, dict):
        primary = rate_limit.get("primaryWindow")
    secondary = rate_limit.get("secondary_window")
    if not isinstance(secondary, dict):
        secondary = rate_limit.get("secondaryWindow")

    for window in (secondary, primary):
        if not isinstance(window, dict):
            continue
        exhausted, reset_at = _window_exhausted(window)
        if exhausted:
            return ProbeResult(
                "no_quota",
                "usage window is exhausted",
                http_status=200,
                error_code="usage_limit_reached",
                reset_at=reset_at,
            )

    plan_type = data.get("plan_type") or data.get("planType")
    if isinstance(plan_type, str) and plan_type:
        return ProbeResult("valid", f"usage fetch succeeded | plan={plan_type}", http_status=200)
    return ProbeResult("valid", "usage fetch succeeded", http_status=200)


def probe_once(
    account: dict[str, Any],
    management: ManagementClient,
    timeout: int,
) -> ProbeResult:
    access_token = account.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return ProbeResult("invalid", "missing access_token", error_code="missing_access_token")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USAGE_USER_AGENT,
    }
    account_id = get_account_id(account)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    response = management.api_call(
        method="GET",
        url=CODEX_USAGE_URL,
        headers=headers,
        timeout=timeout,
    )
    data = parse_api_data(response)
    error_code = extract_error_code(data)
    error_message = extract_error_message(data)

    if response.status_code == 200:
        return classify_usage_payload(data)

    if response.status_code == 429:
        message = error_message or response.body_text[:200] or "usage limit reached"
        if error_code == "usage_limit_reached" or "usage limit" in message.lower():
            reset_at = None
            error = extract_error(data)
            resets_at = error.get("resets_at")
            if isinstance(resets_at, (int, float)):
                reset_at = datetime.fromtimestamp(int(resets_at), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
            return ProbeResult(
                "no_quota",
                message,
                http_status=response.status_code,
                error_code=error_code or "usage_limit_reached",
                reset_at=reset_at,
            )
        return ProbeResult(
            "unknown",
            message,
            http_status=response.status_code,
            error_code=error_code,
        )

    if response.status_code in (402, 404):
        return ProbeResult(
            "invalid",
            error_message or response.body_text[:200] or "usage endpoint rejected this account",
            http_status=response.status_code,
            error_code=error_code,
        )

    if response.status_code == 401:
        return ProbeResult(
            "invalid",
            error_message or response.body_text[:200] or "unauthorized",
            http_status=response.status_code,
            error_code=error_code,
        )

    if response.status_code == 403:
        return ProbeResult(
            "unknown",
            error_message or response.body_text[:200] or "usage endpoint returned 403",
            http_status=response.status_code,
            error_code=error_code,
        )

    return ProbeResult(
        "unknown",
        error_message or response.body_text[:200] or "usage request failed",
        http_status=response.status_code,
        error_code=error_code,
    )


def classify_account(
    account: dict[str, Any],
    management: ManagementClient,
    timeout: int,
) -> ProbeResult:
    if account.get("type") != "codex":
        return ProbeResult("skip", "not a codex account")

    access_token = account.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return ProbeResult("invalid", "missing access_token", error_code="missing_access_token")

    refreshed = False
    if needs_refresh(account):
        ok, reason, error_code = refresh_account(account, management, timeout)
        if not ok:
            probe_result = probe_once(account, management, timeout)
            if probe_result.status in {"valid", "no_quota"}:
                probe_result.refreshed = False
                return probe_result
            return ProbeResult("invalid", reason, error_code=error_code)
        refreshed = True

    result = probe_once(account, management, timeout)
    result.refreshed = refreshed
    if result.status != "invalid" or refreshed:
        return result

    ok, reason, error_code = refresh_account(account, management, timeout)
    if not ok:
        return ProbeResult("invalid", reason, error_code=error_code)

    result = probe_once(account, management, timeout)
    result.refreshed = True
    return result


def update_account_state(account: dict[str, Any], result: ProbeResult) -> bool:
    if result.status not in {"valid", "no_quota", "invalid"}:
        return False

    changed = False
    desired_disabled = True if result.status == "invalid" else False
    if account.get("disabled") is not desired_disabled:
        account["disabled"] = desired_disabled
        changed = True
    return changed


def ensure_unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}__{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def move_by_status(
    path: Path,
    auth_dir: Path,
    status: str,
    invalid_dir_name: str,
    no_quota_dir_name: str,
    apply: bool,
) -> Path | None:
    if status == "valid":
        target_dir = auth_dir
    elif status == "invalid":
        target_dir = auth_dir / invalid_dir_name
    elif status == "no_quota":
        target_dir = auth_dir / no_quota_dir_name
    else:
        return None

    if path.parent.resolve() == target_dir.resolve():
        return None

    target_path = ensure_unique_target(target_dir / path.name)
    if not apply:
        return target_path

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target_path))
    return target_path


def status_label(status: str) -> str:
    return {
        "valid": "VALID",
        "no_quota": "NO_QUOTA",
        "invalid": "INVALID",
        "unknown": "UNKNOWN",
        "skip": "SKIP",
    }.get(status, status.upper())


def process_file(
    path: Path,
    auth_dir: Path,
    management: ManagementClient,
    args: argparse.Namespace,
) -> tuple[str, str]:
    if not path.exists():
        return "unknown", f"[UNKNOWN] {path.name} | file not found"

    try:
        account = read_json(path)
    except Exception as exc:
        return "invalid", f"[INVALID] {path.name} | invalid json: {exc}"

    result = classify_account(account, management, args.timeout)
    account_changed = False
    if result.status != "skip":
        account_changed = update_account_state(account, result)

    if args.apply and result.status != "skip" and (result.refreshed or account_changed):
        write_json(path, account)

    moved_to = move_by_status(
        path=path,
        auth_dir=auth_dir,
        status=result.status,
        invalid_dir_name=args.invalid_dir_name,
        no_quota_dir_name=args.no_quota_dir_name,
        apply=args.apply,
    )

    extras: list[str] = []
    if result.http_status is not None:
        extras.append(f"http={result.http_status}")
    if result.error_code:
        extras.append(f"code={result.error_code}")
    if result.refreshed:
        extras.append("refreshed=yes")
    if result.reset_at:
        extras.append(f"resets_at={result.reset_at}")
    if moved_to is not None:
        extras.append(f"move_to={moved_to}")

    extra_text = " | " + " | ".join(extras) if extras else ""
    message = f"[{status_label(result.status)}] {path.name} | {result.reason}{extra_text}"
    return result.status, message


def scan_once(
    auth_dir: Path,
    management: ManagementClient,
    args: argparse.Namespace,
) -> dict[str, int]:
    files = resolve_files(
        auth_dir,
        args.files,
        args.invalid_dir_name,
        args.no_quota_dir_name,
    )
    counts = {"valid": 0, "no_quota": 0, "invalid": 0, "unknown": 0, "skip": 0}

    if not files:
        log("No account files found.")
        return counts

    log(f"Auth dir: {auth_dir}")
    log(f"Management API: {management.base_url}")
    log(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    log("Transport: CLIProxyAPI /api-call -> wham/usage")
    log(f"Files: {len(files)}")

    for path in files:
        status, message = process_file(path, auth_dir, management, args)
        counts[status] = counts.get(status, 0) + 1
        log(message)

    log("Summary:")
    for key in ("valid", "no_quota", "invalid", "unknown", "skip"):
        log(f"  {key}: {counts.get(key, 0)}")
    log("")
    return counts


def sleep_with_heartbeat(seconds: int) -> None:
    remaining = max(0, int(seconds))
    while remaining > 0:
        chunk = 60 if remaining > 60 else remaining
        if remaining == seconds or remaining <= 60:
            minutes = remaining // 60
            secs = remaining % 60
            log(f"Next scan in {minutes:02d}:{secs:02d}")
        time.sleep(chunk)
        remaining -= chunk


def main() -> int:
    args = parse_args()
    auth_dir = args.auth_dir.resolve()
    if not auth_dir.exists():
        print(f"Auth dir not found: {auth_dir}", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("Interval must be greater than 0.", file=sys.stderr)
        return 2

    session = requests.Session()

    try:
        management_key = prompt_management_key(args.management_key)
        management = ManagementClient(
            session=session,
            base_url=args.management_base_url,
            management_key=management_key,
            timeout=args.timeout,
        )
        management.validate()
    except ManagementApiError as exc:
        print(f"Management API unavailable: {exc.message}", file=sys.stderr)
        return 2

    log("Codex account watcher started.")

    cycle = 0
    while True:
        cycle += 1
        log(f"Scan cycle #{cycle} started.")
        try:
            scan_once(auth_dir, management, args)
        except KeyboardInterrupt:
            log("Stopped by user.")
            return 0
        except ManagementApiError as exc:
            log(f"[ERROR] Management API failed: {exc.message}")
        except Exception as exc:
            log(f"[ERROR] Scan cycle failed: {exc}")

        if args.once:
            log("Single scan completed.")
            return 0

        try:
            sleep_with_heartbeat(args.interval)
        except KeyboardInterrupt:
            log("Stopped by user.")
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
