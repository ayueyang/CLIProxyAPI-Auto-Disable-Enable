import os
import sys
import json
import time
import shutil
import zipfile
import threading
import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Dict, List
from pathlib import Path
import requests
from flask import Flask, jsonify, request, render_template_string

MANAGEMENT_BASE_PATH = "/v0/management"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USAGE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

stop_event = threading.Event()
monitor_state = None
monitor_lock = threading.Lock()
scan_thread = None

class ManagementClient:
    def __init__(self, base_url: str, management_key: str):
        self.base_url = base_url
        self.management_key = management_key
    def validate(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/config", headers={"Authorization": f"Bearer {self.management_key}"}, timeout=10)
            return response.status_code == 200
        except Exception:
            return False
    def patch_auth_file_status(self, auth_id: str, disabled: bool, message: str) -> bool:
        try:
            response = requests.patch(
                f"{self.base_url}/auth-files/{auth_id}/status",
                headers={"Authorization": f"Bearer {self.management_key}", "Content-Type": "application/json"},
                json={"disabled": disabled, "status_message": message},
                timeout=10
            )
            return response.status_code == 200
        except Exception:
            return False
    def api_call(self, method: str, url: str, headers: dict[str, str] | None = None, data: str | None = None, timeout: int | None = None) -> tuple[int, dict[str, Any], str]:
        payload = {
            "method": method,
            "url": url,
            "header": headers or {},
        }
        if data:
            payload["data"] = data
        response = requests.post(
            f"{self.base_url}/api-call",
            headers={"Authorization": f"Bearer {self.management_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout or 30
        )
        try:
            wrapper = response.json()
        except json.JSONDecodeError:
            wrapper = {}
        actual_status = int(wrapper.get("status_code") or wrapper.get("statusCode") or 0)
        raw_body = wrapper.get("body")
        if raw_body is None:
            actual_data = {}
            body_text = ""
        elif isinstance(raw_body, str):
            body_text = raw_body.strip()
            try:
                actual_data = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                actual_data = {}
        elif isinstance(raw_body, dict):
            actual_data = raw_body
            try:
                body_text = json.dumps(raw_body, ensure_ascii=False)
            except Exception:
                body_text = str(raw_body)
        else:
            actual_data = {}
            body_text = str(raw_body)
        return actual_status, actual_data, body_text

class AccountInfo:
    def __init__(self, filename: str, email: str = "", provider: str = "codex", status: str = "unknown", reason: str = "", last_check: str = "", disabled: bool = False, http_status: int | None = None, error_code: str | None = None, reset_at: str | None = None, refreshed: bool = False, plan_type: str | None = None):
        self.filename = filename
        self.email = email
        self.provider = provider
        self.status = status
        self.reason = reason
        self.last_check = last_check
        self.disabled = disabled
        self.http_status = http_status
        self.error_code = error_code
        self.reset_at = reset_at
        self.refreshed = refreshed
        self.plan_type = plan_type

class ProbeResult:
    def __init__(self, status: str, reason: str, http_status: int | None = None, error_code: str | None = None, reset_at: str | None = None):
        self.status = status
        self.reason = reason
        self.http_status = http_status
        self.error_code = error_code
        self.reset_at = reset_at
        self.refreshed = False

class MonitorState:
    def __init__(self):
        self.running = False
        self.scanning = False
        self.auto_disable = True
        self.auto_enable = True
        self.auto_backup = True
        self.auto_cleanup = False
        self.max_backups = 30
        self.last_backup_time: Optional[str] = None
        self.scan_count = 0
        self.last_scan_time: Optional[str] = None
        self.accounts: Dict[str, AccountInfo] = {}
        self.logs: List[Dict[str, str]] = []
        self.interval_valid = 120
        self.interval_no_quota = 600
        self.interval_invalid = 1800
        self.interval_unknown = 300
        self.retry_unknown = 3
        self.retry_invalid = 2
        self.last_scan_by_group: Dict[str, float] = {}
        self.new_file_check_interval = 30
        self.last_file_set: set = set()
        self.last_new_file_check: float = 0
        self._scan_lock = threading.Lock()

PERSIST_FILE = Path(__file__).resolve().parent / "monitor_state.json"

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "CLIProxyAPI-Monitor"

def _check_autostart() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            winreg.CloseKey(key)
            script = str(Path(__file__).resolve())
            return script in str(val)
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except Exception:
        return False

def _set_autostart(enable: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_WRITE)
        if enable:
            script = str(Path(__file__).resolve())
            python_exe = sys.executable
            cmd = f'"{python_exe}" "{script}"'
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            log_info(f"[AUTOSTART] Enabled: {cmd}")
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
                log_info("[AUTOSTART] Disabled")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        log_error(f"[AUTOSTART] Failed: {e}")
        return False

def _save_state() -> None:
    data = {
        "auto_disable": monitor_state.auto_disable,
        "auto_enable": monitor_state.auto_enable,
        "auto_backup": monitor_state.auto_backup,
        "auto_cleanup": monitor_state.auto_cleanup,
        "max_backups": monitor_state.max_backups,
        "interval_valid": monitor_state.interval_valid,
        "interval_no_quota": monitor_state.interval_no_quota,
        "interval_invalid": monitor_state.interval_invalid,
        "interval_unknown": monitor_state.interval_unknown,
        "retry_unknown": monitor_state.retry_unknown,
        "retry_invalid": monitor_state.retry_invalid,
        "new_file_check_interval": monitor_state.new_file_check_interval,
    }
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _load_state() -> None:
    if not PERSIST_FILE.exists():
        return
    try:
        with open(PERSIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in ("auto_disable", "auto_enable", "auto_backup", "auto_cleanup",
                     "max_backups", "interval_valid", "interval_no_quota",
                     "interval_invalid", "interval_unknown", "retry_unknown", "retry_invalid", "new_file_check_interval"):
            if key in data:
                setattr(monitor_state, key, data[key])
    except Exception:
        pass

def log_info(message: str):
    with monitor_lock:
        monitor_state.logs.append({"level": "info", "time": now_local().isoformat(), "message": message})
    print(f"[info] {message}")

def log_warn(message: str):
    with monitor_lock:
        monitor_state.logs.append({"level": "warn", "time": now_local().isoformat(), "message": message})
    print(f"[warn] {message}")

def log_error(message: str):
    with monitor_lock:
        monitor_state.logs.append({"level": "error", "time": now_local().isoformat(), "message": message})
    print(f"[error] {message}")

def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()

def read_config(config_path: str = "") -> Dict[str, Any]:
    if config_path:
        cp = Path(config_path)
    else:
        cp = Path(__file__).resolve().parent / "config.yaml"
    if not cp.exists():
        return {}
    import yaml
    with open(cp, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def get_email_from_account(account: Dict[str, Any]) -> str:
    if email := account.get("email"):
        return email
    try:
        access_token = account.get("access_token")
        if access_token:
            parts = access_token.split(".")
            if len(parts) >= 2:
                import base64
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                claims = json.loads(decoded)
                if email := claims.get("email") or claims.get("sub"):
                    return email
    except Exception:
        pass
    return ""

def get_account_id(account: Dict[str, Any]) -> str:
    if aid := account.get("account_id"):
        return aid
    try:
        access_token = account.get("access_token")
        if access_token:
            parts = access_token.split(".")
            if len(parts) >= 2:
                import base64
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                claims = json.loads(decoded)
                auth_claims = claims.get("https://api.openai.com/auth")
                if isinstance(auth_claims, dict):
                    if aid := auth_claims.get("chatgpt_account_id"):
                        return aid
    except Exception:
        pass
    return ""

def needs_refresh(account: Dict[str, Any]) -> bool:
    if expired := account.get("expired"):
        try:
            exp_time = datetime.fromisoformat(expired.replace("Z", "+00:00"))
            if exp_time.tzinfo is None:
                exp_time = exp_time.replace(tzinfo=timezone.utc)
            return now_local() >= exp_time
        except Exception:
            pass
    access_token = account.get("access_token")
    if access_token:
        try:
            parts = access_token.split(".")
            if len(parts) >= 2:
                import base64
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                claims = json.loads(decoded)
                if exp := claims.get("exp"):
                    exp_time = datetime.fromtimestamp(exp, tz=timezone.utc)
                    return now_local() >= exp_time
        except Exception:
            pass
    return True

def refresh_account(account: Dict[str, Any], management: ManagementClient, timeout: int) -> tuple[bool, str, Optional[str]]:
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return False, "missing refresh_token", "missing_refresh_token"
    try:
        status_code, data, body_text = management.api_call(
            method="POST",
            url="https://oauth.openai.com/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=f"grant_type=refresh_token&refresh_token={refresh_token}",
            timeout=timeout
        )
        if status_code != 200:
            error = data.get("error") or data.get("error_description") or body_text[:200]
            error_code = data.get("error")
            return False, f"refresh failed: {error}", error_code
        access_token = data.get("access_token")
        id_token = data.get("id_token")
        new_refresh_token = data.get("refresh_token")
        if not access_token or not id_token or not new_refresh_token:
            return False, "refresh succeeded but response missing tokens", "refresh_tokens_missing"
        refreshed_at = now_local()
        account["access_token"] = access_token
        account["id_token"] = id_token
        account["refresh_token"] = new_refresh_token
        account["last_refresh"] = refreshed_at.isoformat(timespec="seconds")
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            account["expired"] = (refreshed_at + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds")
        account["disabled"] = False
        return True, "refresh succeeded", None
    except Exception as exc:
        return False, f"refresh failed: {exc}", "refresh_failed"

def _api_call_with_retry(management: ManagementClient, *, method: str, url: str, headers: dict[str, str] | None = None, data: str | None = None, timeout: int | None = None, max_retries: int = 2) -> tuple[int, dict[str, Any], str]:
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return management.api_call(method=method, url=url, headers=headers, data=data, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = 3 * attempt
                log_warn(f"API call failed (attempt {attempt}/{max_retries}), retrying in {wait}s: {exc}")
                time.sleep(wait)
    raise last_exc

def extract_error_code(data: Dict[str, Any]) -> str:
    if error := data.get("error"):
        if isinstance(error, dict):
            if code := error.get("code"):
                return str(code)
        elif isinstance(error, str):
            return error
    return ""

def extract_error_message(data: Dict[str, Any]) -> str:
    if error := data.get("error"):
        if isinstance(error, dict):
            if message := error.get("message"):
                return str(message)
        elif isinstance(error, str):
            return error
    return ""

def extract_error(data: Dict[str, Any]) -> Dict[str, Any]:
    if error := data.get("error"):
        if isinstance(error, dict):
            return error
    return {}

def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def _window_reset_iso(window: Dict[str, Any]) -> Optional[str]:
    reset_at = window.get("reset_at") or window.get("resets_at")
    if reset_at is not None:
        try:
            if isinstance(reset_at, (int, float)):
                reset_time = datetime.fromtimestamp(int(reset_at), tz=timezone.utc).astimezone()
                return reset_time.isoformat(timespec="seconds")
            if isinstance(reset_at, str):
                return reset_at
        except Exception:
            pass
    reset_after = window.get("reset_after_seconds") or window.get("resetAfterSeconds")
    if isinstance(reset_after, (int, float)) and reset_after > 0:
        reset_time = now_local() + timedelta(seconds=int(reset_after))
        return reset_time.isoformat(timespec="seconds")
    return None

def _window_exhausted(window: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    if not isinstance(window, dict):
        return False, None
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
    if exhausted:
        return True, _window_reset_iso(window)
    return False, None

def classify_usage_payload(data: dict[str, Any]) -> ProbeResult:
    error_code = extract_error_code(data)
    error_message = extract_error_message(data)
    if error_code or error_message:
        lower_msg = (error_message or "").lower()
        lower_code = (error_code or "").lower()
        if any(kw in lower_msg for kw in ("account", "credential", "unauthorized", "invalid")):
            return ProbeResult("invalid", error_message or "account error in usage response", http_status=200, error_code=error_code or "account_error")
        if "limit" in lower_msg or "quota" in lower_msg or "usage_limit" in lower_code:
            return ProbeResult("no_quota", error_message or "usage limit in response", http_status=200, error_code=error_code or "usage_limit_reached")
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = data.get("rateLimit")
    if not isinstance(rate_limit, dict):
        plan_type = data.get("plan_type") or data.get("planType")
        if isinstance(plan_type, str) and plan_type:
            return ProbeResult("valid", f"plan={plan_type}", http_status=200)
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
            return ProbeResult("no_quota", "usage window exhausted", http_status=200, error_code="usage_limit_reached", reset_at=reset_at)
    plan_type = data.get("plan_type") or data.get("planType")
    if isinstance(plan_type, str) and plan_type:
        return ProbeResult("valid", f"plan={plan_type}", http_status=200)
    return ProbeResult("valid", "usage fetch succeeded", http_status=200)

def probe_once(account: dict[str, Any], management: ManagementClient, timeout: int) -> ProbeResult:
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
    try:
        status_code, data, body_text = _api_call_with_retry(management, method="GET", url=CODEX_USAGE_URL, headers=headers, timeout=timeout)
    except Exception as exc:
        return ProbeResult("unknown", f"api call error: {exc}", error_code="api_call_error")
    error_code = extract_error_code(data)
    error_message = extract_error_message(data)
    if status_code == 200:
        if error_code or error_message:
            lower_msg = (error_message or "").lower()
            lower_code = (error_code or "").lower()
            if "account" in lower_msg or "account_id" in lower_code or "credential" in lower_msg:
                return ProbeResult("invalid", error_message or "account error", http_status=200, error_code=error_code or "account_error")
        return classify_usage_payload(data)
    if status_code == 429:
        message = error_message or body_text[:200] or "usage limit reached"
        if error_code == "usage_limit_reached" or "usage limit" in message.lower():
            reset_at = None
            error = extract_error(data)
            resets_at = error.get("resets_at")
            if isinstance(resets_at, (int, float)):
                reset_at = datetime.fromtimestamp(int(resets_at), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
            return ProbeResult("no_quota", message, http_status=status_code, error_code=error_code or "usage_limit_reached", reset_at=reset_at)
        return ProbeResult("unknown", message, http_status=status_code, error_code=error_code)
    if status_code in (401, 402, 404):
        return ProbeResult("invalid", error_message or body_text[:200] or "unauthorized", http_status=status_code, error_code=error_code)
    if status_code == 403:
        return ProbeResult("unknown", error_message or body_text[:200] or "forbidden", http_status=status_code, error_code=error_code)
    return ProbeResult("unknown", error_message or body_text[:200] or "request failed", http_status=status_code, error_code=error_code)

def classify_account(account: dict[str, Any], management: ManagementClient, timeout: int, extra_retries: int = 0) -> ProbeResult:
    if account.get("type") != "codex":
        return ProbeResult("skip", "not a codex account")
    access_token = account.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return ProbeResult("invalid", "missing access_token", error_code="missing_access_token")
    account_id = get_account_id(account)
    if not account_id:
        return ProbeResult("invalid", "Codex credential missing ChatGPT account ID", error_code="missing_account_id")
    refresh_token = account.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        return ProbeResult("invalid", "Codex credential missing refresh_token (cannot renew)", error_code="missing_refresh_token")
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
    if result.status == "unknown" and extra_retries > 0:
        for i in range(extra_retries):
            log_warn(f"[RETRY] unknown result, retry {i+1}/{extra_retries}")
            time.sleep(5)
            result = probe_once(account, management, timeout)
            if result.status not in ("unknown",):
                break
        result.refreshed = refreshed
    if result.status == "invalid" and extra_retries > 0 and not refreshed:
        ok, reason, error_code = refresh_account(account, management, timeout)
        if ok:
            result = probe_once(account, management, timeout)
            result.refreshed = True
            if result.status == "unknown":
                for i in range(extra_retries):
                    log_warn(f"[RETRY] unknown after refresh, retry {i+1}/{extra_retries}")
                    time.sleep(5)
                    result = probe_once(account, management, timeout)
                    if result.status not in ("unknown",):
                        break
                result.refreshed = True
        else:
            return ProbeResult("invalid", reason, error_code=error_code)
    elif result.status != "invalid" or refreshed:
        return result
    ok, reason, error_code = refresh_account(account, management, timeout)
    if not ok:
        return ProbeResult("invalid", reason, error_code=error_code)
    result = probe_once(account, management, timeout)
    result.refreshed = True
    return result

STATUS_SUFFIXES = {".invalid", ".no_quota", ".unknown"}

def get_base_name(path: Path) -> str:
    name = path.name
    for sfx in STATUS_SUFFIXES:
        if name.endswith(sfx):
            return name[:-len(sfx)]
    return name

def get_status_from_path(path: Path) -> str:
    name = path.name
    if name.endswith(".json.invalid"):
        return "invalid"
    if name.endswith(".json.no_quota"):
        return "no_quota"
    if name.endswith(".json.unknown"):
        return "unknown"
    return "unknown"

def get_auth_id_from_path(path: Path) -> str:
    return Path(get_base_name(path)).stem

def rename_for_status(path: Path, status: str) -> Optional[Path]:
    base = get_base_name(path)
    if status in ("valid", "skip"):
        new_name = base
    elif status in ("invalid", "no_quota", "unknown"):
        new_name = base + "." + status
    else:
        new_name = base
    new_path = path.parent / new_name
    if path.resolve() == new_path.resolve():
        return path
    try:
        path.replace(new_path)
        log_info(f"[RENAME] {path.name} -> {new_name}")
        return new_path
    except Exception as e:
        log_error(f"[RENAME] Failed: {path.name} -> {new_name}: {e}")
        return None

def migrate_subfolders(auth_dir: Path) -> None:
    mapping = {
        "invalid_accounts": "invalid",
        "no_quota_accounts": "no_quota",
        "unknown_accounts": "unknown",
    }
    for folder, status in mapping.items():
        subfolder = auth_dir / folder
        if not subfolder.exists():
            continue
        for f in list(subfolder.glob("*.json")):
            if not f.is_file():
                continue
            new_name = f.name + "." + status
            new_path = auth_dir / new_name
            base_json = auth_dir / f.name
            if base_json.exists():
                try:
                    base_json.unlink()
                    log_info(f"[MIGRATE] Removed duplicate: {f.name} from data/")
                except Exception as e:
                    log_error(f"[MIGRATE] Failed to remove {base_json}: {e}")
            try:
                shutil.move(str(f), str(new_path))
                log_info(f"[MIGRATE] {f.name} -> data/{new_name}")
            except Exception as e:
                log_error(f"[MIGRATE] Failed: {f.name}: {e}")

def resolve_files(auth_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    if not auth_dir.exists() or not auth_dir.is_dir():
        return []
    for pattern in ("*.json", "*.json.invalid", "*.json.no_quota", "*.json.unknown"):
        for path in auth_dir.glob(pattern):
            if path.is_file():
                candidates.append(path)
    seen: set[str] = set()
    deduped: list[Path] = []
    for p in sorted(candidates):
        base = get_base_name(p)
        if base not in seen:
            seen.add(base)
            deduped.append(p)
    return deduped

def _get_group_for_status(status: str) -> str:
    if status in ("valid", "skip"):
        return "valid"
    if status == "no_quota":
        return "no_quota"
    if status == "invalid":
        return "invalid"
    return "unknown"

def _get_interval_for_group(group: str) -> int:
    mapping = {
        "valid": monitor_state.interval_valid,
        "no_quota": monitor_state.interval_no_quota,
        "invalid": monitor_state.interval_invalid,
        "unknown": monitor_state.interval_unknown,
    }
    return mapping.get(group, 300)

MAX_BACKUPS = 10

def auto_backup_data(auth_dir: Path) -> None:
    if not monitor_state.auto_backup:
        return
    ts = datetime.now().strftime("backup_%Y%m%d_%H%M%S")
    backup_path = auth_dir / "backups" / f"{ts}.zip"
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for pattern in ("*.json", "*.json.invalid", "*.json.no_quota", "*.json.unknown"):
                for f in auth_dir.glob(pattern):
                    if f.is_file():
                        zf.write(f, f.name)
                        count += 1
        monitor_state.last_backup_time = now_local().isoformat(timespec="seconds")
        size_kb = backup_path.stat().st_size / 1024
        log_info(f"[BACKUP] {count} files -> backups/{ts}.zip ({size_kb:.0f}KB)")
    except Exception as e:
        log_error(f"[BACKUP] Failed: {e}")
    if monitor_state.auto_cleanup:
        _cleanup_old_backups(auth_dir)

def _cleanup_old_backups(auth_dir: Path) -> None:
    backups_root = auth_dir / "backups"
    if not backups_root.exists():
        return
    try:
        zips = sorted([f for f in backups_root.iterdir() if f.is_file() and f.name.startswith("backup_") and f.name.endswith(".zip")])
        max_keep = monitor_state.max_backups
        if len(zips) > max_keep:
            for old_zip in zips[:-max_keep]:
                try:
                    old_zip.unlink()
                    log_info(f"[CLEANUP] Removed old backup: {old_zip.name}")
                except Exception as e:
                    log_warn(f"[CLEANUP] Failed to remove {old_zip.name}: {e}")
    except Exception as e:
        log_warn(f"[CLEANUP] Failed to list backups: {e}")

def scan_accounts(auth_dir: Path, management: ManagementClient, timeout: int, scan_filter: str | None = None, force: bool = False) -> None:
    with monitor_state._scan_lock:
        with monitor_lock:
            monitor_state.scanning = True
    try:
        _do_scan(auth_dir, management, timeout, scan_filter, force)
    finally:
        with monitor_lock:
            monitor_state.scanning = False

def _do_scan(auth_dir: Path, management: ManagementClient, timeout: int, scan_filter: str | None = None, force: bool = False) -> None:
    migrate_subfolders(auth_dir)
    auto_backup_data(auth_dir)
    files = resolve_files(auth_dir)
    now_ts = time.time()
    due_groups: set[str] = set()
    if force:
        due_groups = {"valid", "no_quota", "invalid", "unknown"}
    else:
        for g in ("valid", "no_quota", "invalid", "unknown"):
            last = monitor_state.last_scan_by_group.get(g, 0)
            interval = _get_interval_for_group(g)
            if (now_ts - last) >= interval:
                due_groups.add(g)
    for path in files:
        if stop_event.is_set():
            break
        base_name = get_base_name(path)
        try:
            account = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            info = AccountInfo(filename=base_name, status="invalid", reason=f"invalid json: {exc}", last_check=now_local().isoformat(timespec="seconds"))
            with monitor_lock:
                monitor_state.accounts[base_name] = info
            log_warn(f"[INVALID] {path.name} | invalid json: {exc}")
            continue
        prev_status = None
        with monitor_lock:
            if base_name in monitor_state.accounts:
                prev_status = monitor_state.accounts[base_name].status
        if prev_status is None:
            prev_status = get_status_from_path(path)
        group = _get_group_for_status(prev_status)
        if scan_filter and group != scan_filter and prev_status != scan_filter:
            continue
        if group not in due_groups:
            continue
        extra_retries = 0
        if prev_status == "unknown":
            extra_retries = monitor_state.retry_unknown
        elif prev_status == "invalid":
            extra_retries = monitor_state.retry_invalid
        elif prev_status == "no_quota":
            extra_retries = 1
        try:
            result = classify_account(account, management, timeout, extra_retries=extra_retries)
            email = get_email_from_account(account)
            new_group = _get_group_for_status(result.status)
            info = AccountInfo(
                filename=base_name,
                email=email,
                provider=account.get("type", "codex"),
                status=result.status,
                reason=result.reason,
                last_check=now_local().isoformat(timespec="seconds"),
                disabled=account.get("disabled", False),
                http_status=result.http_status,
                error_code=result.error_code,
                reset_at=result.reset_at,
                refreshed=result.refreshed,
                plan_type=account.get("plan_type")
            )
            status_changed = prev_status != result.status
            change_log = f" (was {prev_status})" if status_changed else ""
            log_info(f"[{result.status.upper()}] {path.name} ({email or '-'}) | {result.reason} | code={result.error_code or ''}{change_log}")
            auth_id = get_auth_id_from_path(path)
            current_path = path
            if result.status in ("invalid", "no_quota", "unknown"):
                if monitor_state.auto_disable:
                    if result.status in ("invalid", "no_quota") and not account.get("disabled"):
                        if management.patch_auth_file_status(auth_id, True, result.reason):
                            account["disabled"] = True
                            try:
                                current_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
                                log_warn(f"[DISABLE] {path.name} - {result.status}, disabling")
                            except Exception as e:
                                log_error(f"Failed to update disabled status: {e}")
                    new_path = rename_for_status(current_path, result.status)
                    if new_path:
                        current_path = new_path
            elif result.status in ("valid", "skip"):
                if monitor_state.auto_enable:
                    if account.get("disabled"):
                        if management.patch_auth_file_status(auth_id, False, "account valid"):
                            account["disabled"] = False
                            try:
                                current_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
                                log_warn(f"[ENABLE] {path.name} - account valid, enabling")
                            except Exception as e:
                                log_error(f"Failed to update enabled status: {e}")
                    new_path = rename_for_status(current_path, "valid")
                    if new_path:
                        current_path = new_path
            with monitor_lock:
                monitor_state.accounts[base_name] = info
        except Exception as exc:
            info = AccountInfo(
                filename=base_name,
                email=get_email_from_account(account),
                provider=account.get("type", "codex"),
                status="unknown",
                reason=f"scan failed: {exc}",
                last_check=now_local().isoformat(timespec="seconds"),
                disabled=account.get("disabled", False)
            )
            with monitor_lock:
                monitor_state.accounts[base_name] = info
            log_error(f"Error processing {path.name}: {exc}")
            continue
        time.sleep(1)
    with monitor_lock:
        for g in due_groups:
            monitor_state.last_scan_by_group[g] = time.time()

def _check_new_files(auth_dir: Path, management: ManagementClient, timeout: int) -> None:
    now_ts = time.time()
    with monitor_lock:
        interval = monitor_state.new_file_check_interval
        if (now_ts - monitor_state.last_new_file_check) < interval:
            return
        monitor_state.last_new_file_check = now_ts
    current_files = {f.name for f in resolve_files(auth_dir)}
    with monitor_lock:
        known = monitor_state.last_file_set
        new_names = current_files - known
    if new_names:
        log_info(f"[NEW] Detected {len(new_names)} new file(s): {', '.join(sorted(new_names))}")
        scan_accounts(auth_dir, management, timeout, force=False)
    with monitor_lock:
        monitor_state.last_file_set = current_files

def monitor_loop(auth_dir: Path, management: ManagementClient, timeout: int) -> None:
    first_scan = True
    while not stop_event.is_set():
        with monitor_lock:
            if not monitor_state.running:
                break
            monitor_state.scan_count += 1
            monitor_state.last_scan_time = now_local().isoformat()
        if first_scan:
            log_info(f"=== Scan cycle #{monitor_state.scan_count} started (initial scan) ===")
            scan_accounts(auth_dir, management, timeout, force=True)
            with monitor_lock:
                monitor_state.last_file_set = {f.name for f in resolve_files(auth_dir)}
            log_info(f"=== Scan cycle #{monitor_state.scan_count} completed ===")
            first_scan = False
        else:
            now_ts = time.time()
            groups_due = []
            for g in ("valid", "no_quota", "invalid", "unknown"):
                last = monitor_state.last_scan_by_group.get(g, 0)
                interval = _get_interval_for_group(g)
                if (now_ts - last) >= interval:
                    groups_due.append(g)
            if groups_due:
                log_info(f"=== Scan cycle #{monitor_state.scan_count} started (groups due: {', '.join(groups_due)}) ===")
                scan_accounts(auth_dir, management, timeout, force=False)
                with monitor_lock:
                    monitor_state.last_file_set = {f.name for f in resolve_files(auth_dir)}
                log_info(f"=== Scan cycle #{monitor_state.scan_count} completed ===")
            else:
                with monitor_lock:
                    monitor_state.scan_count -= 1
                _check_new_files(auth_dir, management, timeout)
        tick = 10
        for _ in range(tick):
            if stop_event.is_set():
                break
            time.sleep(1)

def _resolve_auth_dir(cfg: Dict[str, Any], override: str = "") -> Path:
    auth_dir_str = override or os.environ.get("AUTH_DIR", "") or cfg.get("auth_dir", "data")
    if Path(auth_dir_str).is_absolute():
        return Path(auth_dir_str).resolve()
    return (Path(__file__).resolve().parent / auth_dir_str).resolve()

def _count_backups(auth_dir: Path) -> int:
    backups_root = auth_dir / "backups"
    if not backups_root.exists():
        return 0
    return sum(1 for f in backups_root.iterdir() if f.is_file() and f.name.startswith("backup_") and f.name.endswith(".zip"))

def _backup_size(auth_dir: Path) -> float:
    backups_root = auth_dir / "backups"
    if not backups_root.exists():
        return 0.0
    total = sum(f.stat().st_size for f in backups_root.iterdir() if f.is_file() and f.name.startswith("backup_") and f.name.endswith(".zip"))
    return round(total / 1024, 1)

def _get_management_base_url(cfg: Dict[str, Any]) -> str:
    override = os.environ.get("CLIPROXYAPI_URL", "").strip()
    if override:
        return override.rstrip("/") + MANAGEMENT_BASE_PATH
    port = cfg.get("port", 8317)
    return f"http://127.0.0.1:{port}{MANAGEMENT_BASE_PATH}"

def create_app(config_path: str = "", auth_dir_override: str = "") -> Flask:
    app = Flask(__name__)
    cfg = read_config(config_path)
    app.config["AUTH_DIR"] = _resolve_auth_dir(cfg, auth_dir_override)
    app.config["CLI_CONFIG"] = cfg
    app.config["CONFIG_PATH"] = config_path
    _load_state()
    @app.route("/")
    def index():
        return render_template_string(HTML_TEMPLATE)
    @app.route("/api/status")
    def api_status():
        with monitor_lock:
            now_ts = time.time()
            next_scan = {}
            for g in ("valid", "no_quota", "invalid", "unknown"):
                last = monitor_state.last_scan_by_group.get(g, 0)
                interval = _get_interval_for_group(g)
                remaining = max(0, interval - (now_ts - last))
                next_scan[g] = int(remaining)
            return jsonify({
                "running": monitor_state.running,
                "scanning": monitor_state.scanning,
                "auto_disable": monitor_state.auto_disable,
                "auto_enable": monitor_state.auto_enable,
                "auto_backup": monitor_state.auto_backup,
                "auto_cleanup": monitor_state.auto_cleanup,
                "max_backups": monitor_state.max_backups,
                "last_backup_time": monitor_state.last_backup_time,
                "scan_count": monitor_state.scan_count,
                "last_scan_time": monitor_state.last_scan_time,
                "interval_valid": monitor_state.interval_valid,
                "interval_no_quota": monitor_state.interval_no_quota,
                "interval_invalid": monitor_state.interval_invalid,
                "interval_unknown": monitor_state.interval_unknown,
                "retry_unknown": monitor_state.retry_unknown,
                "retry_invalid": monitor_state.retry_invalid,
                "new_file_check_interval": monitor_state.new_file_check_interval,
                "next_scan": next_scan,
                "accounts": {k: {"filename": v.filename, "email": v.email, "provider": v.provider, "status": v.status, "reason": v.reason, "last_check": v.last_check, "disabled": v.disabled, "http_status": v.http_status, "error_code": v.error_code, "reset_at": v.reset_at, "refreshed": v.refreshed, "plan_type": v.plan_type} for k, v in monitor_state.accounts.items()},
                "auth_dir": str(app.config["AUTH_DIR"]),
                "backup_count": _count_backups(app.config["AUTH_DIR"]),
                "backup_size_kb": _backup_size(app.config["AUTH_DIR"]),
            })
    @app.route("/api/auth-dir", methods=["GET"])
    def api_get_auth_dir():
        return jsonify({"auth_dir": str(app.config["AUTH_DIR"])})
    @app.route("/api/auth-dir", methods=["POST"])
    def api_set_auth_dir():
        data = request.get_json(silent=True) or {}
        new_dir = data.get("auth_dir", "").strip()
        if not new_dir:
            return jsonify({"status": "error", "message": "empty path"})
        p = Path(new_dir)
        if not p.exists():
            return jsonify({"status": "error", "message": "directory does not exist"})
        if not p.is_dir():
            return jsonify({"status": "error", "message": "not a directory"})
        app.config["AUTH_DIR"] = p
        log_info(f"Auth dir changed to: {p}")
        return jsonify({"status": "ok", "auth_dir": str(p)})

    @app.route("/api/autostart", methods=["GET"])
    def api_get_autostart():
        enabled = _check_autostart()
        return jsonify({"enabled": enabled})

    @app.route("/api/autostart", methods=["POST"])
    def api_set_autostart():
        data = request.get_json(silent=True) or {}
        enable = data.get("enable", False)
        ok = _set_autostart(enable)
        if ok:
            return jsonify({"status": "ok", "enabled": enable})
        return jsonify({"status": "error", "message": "failed to update autostart"})

    @app.route("/api/logs")
    def api_logs():
        after = int(request.args.get("after", 0))
        with monitor_lock:
            logs = monitor_state.logs[after:]
        return jsonify({"logs": logs})
    @app.route("/api/start", methods=["POST"])
    def api_start():
        global scan_thread
        with monitor_lock:
            if monitor_state.running:
                return jsonify({"status": "already_running"})
            monitor_state.running = True
        cfg = app.config["CLI_CONFIG"]
        auth_dir = app.config["AUTH_DIR"]
        management_key = os.environ.get("CLIPROXYAPI_MANAGEMENT_KEY", "")
        if not management_key:
            log_error("Management key not set. Set CLIPROXYAPI_MANAGEMENT_KEY env var.")
            with monitor_lock:
                monitor_state.running = False
            return jsonify({"status": "error", "message": "management key not set"})
        management = ManagementClient(
            base_url=_get_management_base_url(cfg),
            management_key=management_key,
        )
        if not management.validate():
            log_error("Cannot connect to CLIProxyAPI management API.")
            with monitor_lock:
                monitor_state.running = False
            return jsonify({"status": "error", "message": "cannot connect to management API"})
        stop_event.set()
        time.sleep(0.5)
        if scan_thread and scan_thread.is_alive():
            scan_thread.join(timeout=3)
        stop_event.clear()
        with monitor_lock:
            monitor_state.last_scan_by_group.clear()
            monitor_state.scan_count = 0
        scan_thread = threading.Thread(target=monitor_loop, args=(auth_dir, management, 90), daemon=True)
        scan_thread.start()
        log_info("Monitor started")
        return jsonify({"status": "started"})
    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        stop_event.set()
        with monitor_lock:
            monitor_state.running = False
        log_info("Monitor stopped")
        return jsonify({"status": "stopped"})
    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        with monitor_lock:
            if monitor_state.scanning:
                return jsonify({"status": "already_scanning"})
        cfg = app.config["CLI_CONFIG"]
        auth_dir = app.config["AUTH_DIR"]
        management_key = os.environ.get("CLIPROXYAPI_MANAGEMENT_KEY", "")
        if not management_key:
            log_error("Management key not set. Set CLIPROXYAPI_MANAGEMENT_KEY env var.")
            return jsonify({"status": "error", "message": "management key not set"})
        management = ManagementClient(
            base_url=_get_management_base_url(cfg),
            management_key=management_key,
        )
        if not management.validate():
            log_error("Cannot connect to CLIProxyAPI management API.")
            return jsonify({"status": "error", "message": "cannot connect to management API"})
        scan_filter = request.json.get("filter") if request.json else None
        force_scan = request.json.get("force", True) if request.json else True
        t = threading.Thread(target=scan_accounts, args=(auth_dir, management, 90, scan_filter, force_scan), daemon=True)
        t.start()
        return jsonify({"status": "scanning"})
    @app.route("/api/toggle", methods=["POST"])
    def api_toggle():
        data = request.json or {}
        key = data.get("key")
        if key not in ["auto_disable", "auto_enable", "auto_backup", "auto_cleanup"]:
            return jsonify({"status": "error", "message": "invalid key"})
        with monitor_lock:
            current = getattr(monitor_state, key)
            setattr(monitor_state, key, not current)
        log_info(f"Toggled {key} to {not current}")
        _save_state()
        return jsonify({"status": "ok", "value": not current})
    @app.route("/api/intervals", methods=["POST"])
    def api_intervals():
        data = request.json or {}
        updated = {}
        for key in ("interval_valid", "interval_no_quota", "interval_invalid", "interval_unknown", "retry_unknown", "retry_invalid", "max_backups", "new_file_check_interval"):
            if key in data:
                val = data[key]
                if key == "max_backups":
                    if not isinstance(val, int) or val < 1:
                        continue
                elif key == "new_file_check_interval":
                    if not isinstance(val, int) or val < 5:
                        continue
                elif not isinstance(val, int) or val < 10:
                    if key.startswith("retry"):
                        if val < 0:
                            continue
                    else:
                        continue
                with monitor_lock:
                    setattr(monitor_state, key, val)
                updated[key] = val
        if updated:
            log_info(f"Updated scan config: {updated}")
            _save_state()
        return jsonify({"status": "ok", "updated": updated})
    @app.route("/api/backups")
    def api_backups():
        auth_dir = app.config["AUTH_DIR"]
        backups_root = auth_dir / "backups"
        result = []
        if backups_root.exists():
            for f in sorted(backups_root.iterdir(), reverse=True):
                if f.is_file() and f.name.startswith("backup_") and f.name.endswith(".zip"):
                    try:
                        with zipfile.ZipFile(f, 'r') as zf:
                            file_count = len(zf.namelist())
                    except Exception:
                        file_count = 0
                    size_kb = f.stat().st_size / 1024
                    result.append({"name": f.name, "files": file_count, "size_kb": round(size_kb, 1)})
        return jsonify({"backups": result})
    @app.route("/api/restore", methods=["POST"])
    def api_restore():
        data = request.json or {}
        backup_name = data.get("backup")
        if not backup_name:
            return jsonify({"status": "error", "message": "backup name required"})
        auth_dir = app.config["AUTH_DIR"]
        backup_path = auth_dir / "backups" / backup_name
        if not backup_path.exists():
            return jsonify({"status": "error", "message": f"backup {backup_name} not found"})
        current_bases = {get_base_name(f) for f in resolve_files(auth_dir)}
        restored = 0
        skipped = 0
        try:
            with zipfile.ZipFile(backup_path, 'r') as zf:
                for name in zf.namelist():
                    base = get_base_name(Path(name))
                    if base not in current_bases:
                        try:
                            zf.extract(name, auth_dir)
                            restored += 1
                            log_info(f"[RESTORE] {name} from {backup_name}")
                            current_bases.add(base)
                        except Exception as e:
                            log_error(f"[RESTORE] Failed to restore {name}: {e}")
                    else:
                        skipped += 1
        except Exception as e:
            return jsonify({"status": "error", "message": f"failed to read backup: {e}"})
        log_info(f"[RESTORE] Done: {restored} restored, {skipped} already exist, from {backup_name}")
        return jsonify({"status": "ok", "restored": restored, "skipped": skipped})

    @app.route("/api/delete-backup", methods=["POST"])
    def api_delete_backup():
        data = request.json or {}
        backup_name = data.get("backup")
        if not backup_name:
            return jsonify({"status": "error", "message": "backup name required"})
        if not backup_name.startswith("backup_") or not backup_name.endswith(".zip"):
            return jsonify({"status": "error", "message": "invalid backup name"})
        auth_dir = app.config["AUTH_DIR"]
        backup_path = auth_dir / "backups" / backup_name
        if not backup_path.exists():
            return jsonify({"status": "error", "message": f"backup {backup_name} not found"})
        try:
            backup_path.unlink()
            log_info(f"[BACKUP] Deleted: {backup_name}")
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
    @app.route("/api/backup-now", methods=["POST"])
    def api_backup_now():
        auth_dir = app.config["AUTH_DIR"]
        auto_backup_data(auth_dir)
        return jsonify({"status": "ok", "last_backup_time": monitor_state.last_backup_time})

    @app.route("/api/enable-all", methods=["POST"])
    def api_enable_all():
        auth_dir = app.config["AUTH_DIR"]
        files = resolve_files(auth_dir)
        enabled = 0
        failed = 0
        for f in files:
            status = get_status_from_path(f)
            if status in ("invalid", "no_quota", "unknown"):
                new_path = rename_for_status(f, "valid")
                if new_path is not None:
                    enabled += 1
                else:
                    failed += 1
        log_info(f"[ENABLE-ALL] {enabled} accounts re-enabled, {failed} failed")
        return jsonify({"status": "ok", "enabled": enabled, "failed": failed})
    @app.route("/api/export")
    def api_export():
        fmt = request.args.get("format", "csv")
        with monitor_lock:
            accounts = dict(monitor_state.accounts)
        auth_dir = app.config["AUTH_DIR"]
        if fmt == "json":
            data = []
            for name, info in accounts.items():
                entry = {
                    "filename": info.filename,
                    "email": info.email or "",
                    "status": info.status,
                    "reason": (info.reason or "")[:200],
                    "disabled": info.disabled,
                    "last_check": info.last_check or "",
                    "plan_type": info.plan_type or "",
                    "reset_at": info.reset_at or "",
                }
                for suffix in ("", ".invalid", ".no_quota", ".unknown"):
                    p = auth_dir / (info.filename + suffix) if suffix else auth_dir / info.filename
                    if p.exists():
                        try:
                            raw = json.loads(p.read_text(encoding="utf-8"))
                            for key in ("access_token", "refresh_token", "session_key", "session_token", "type", "proxy_url"):
                                if key in raw and key not in entry:
                                    entry[key] = raw[key]
                        except Exception:
                            pass
                        break
                data.append(entry)
            from flask import Response
            return Response(json.dumps(data, ensure_ascii=False, indent=2), mimetype="application/json", headers={"Content-Disposition": "attachment; filename=accounts_export.json"})
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["filename", "email", "status", "reason", "disabled", "last_check", "plan_type", "reset_at", "access_token", "refresh_token"])
        for name, info in accounts.items():
            reason = (info.reason or "")[:200]
            at = ""
            rt = ""
            for suffix in ("", ".invalid", ".no_quota", ".unknown"):
                p = auth_dir / (info.filename + suffix) if suffix else auth_dir / info.filename
                if p.exists():
                    try:
                        raw = json.loads(p.read_text(encoding="utf-8"))
                        at = raw.get("access_token", "")
                        rt = raw.get("refresh_token", "")
                    except Exception:
                        pass
                    break
            writer.writerow([info.filename, info.email or "", info.status, reason, info.disabled, info.last_check or "", info.plan_type or "", info.reset_at or "", at, rt])
        from flask import Response
        return Response(output.getvalue(), mimetype="text/csv; charset=utf-8-sig", headers={"Content-Disposition": "attachment; filename=accounts_export.csv"})
    @app.route("/api/import", methods=["POST"])
    def api_import():
        if "file" not in request.files:
            return jsonify({"status": "error", "message": "no file uploaded"})
        uploaded = request.files["file"]
        if not uploaded.filename:
            return jsonify({"status": "error", "message": "empty filename"})
        auth_dir = app.config["AUTH_DIR"]
        try:
            content = uploaded.read().decode("utf-8")
        except Exception as e:
            return jsonify({"status": "error", "message": f"read failed: {e}"})
        imported = 0
        skipped = 0
        errors = 0
        fname = uploaded.filename.lower()
        if fname.endswith(".json"):
            try:
                data = json.loads(content)
            except Exception as e:
                return jsonify({"status": "error", "message": f"invalid JSON: {e}"})
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return jsonify({"status": "error", "message": "JSON must be an object or array of objects"})
            for item in data:
                if not isinstance(item, dict):
                    errors += 1
                    continue
                email = item.get("email", "")
                acc_type = item.get("type", "codex")
                if email:
                    prefix = "codex-" if acc_type == "codex" else ""
                    plan = item.get("plan_type", "")
                    suffix = f"-{plan}" if acc_type == "codex" and plan else ""
                    filename = f"{prefix}{email}{suffix}.json"
                else:
                    token = item.get("access_token", "") or item.get("refresh_token", "")
                    filename = f"imported_{hash(token) % 100000}.json" if token else None
                if not filename:
                    errors += 1
                    continue
                target = auth_dir / filename
                if target.exists():
                    skipped += 1
                    continue
                try:
                    target.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                    imported += 1
                except Exception as e:
                    log_error(f"[IMPORT] Failed to write {filename}: {e}")
                    errors += 1
        elif fname.endswith(".csv"):
            import csv
            import io
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                email = row.get("email", "").strip()
                if not email:
                    errors += 1
                    continue
                acc_type = row.get("type", "codex")
                prefix = "codex-" if acc_type == "codex" else ""
                plan = row.get("plan_type", "").strip()
                suffix = f"-{plan}" if acc_type == "codex" and plan else ""
                filename = f"{prefix}{email}{suffix}.json"
                target = auth_dir / filename
                if target.exists():
                    skipped += 1
                    continue
                account = {"email": email, "type": acc_type}
                for key in ("access_token", "refresh_token", "plan_type", "disabled"):
                    if key in row and row[key].strip():
                        account[key] = row[key].strip()
                try:
                    target.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
                    imported += 1
                except Exception as e:
                    log_error(f"[IMPORT] Failed to write {filename}: {e}")
                    errors += 1
        else:
            return jsonify({"status": "error", "message": "unsupported format, use .json or .csv"})
        log_info(f"[IMPORT] {imported} imported, {skipped} skipped, {errors} errors from {uploaded.filename}")
        return jsonify({"status": "ok", "imported": imported, "skipped": skipped, "errors": errors})
    return app

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CLIProxyAPI - 自动禁用解禁</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #1e1e2e; border-radius: 3px; }
::-webkit-scrollbar-thumb { background: #45475a; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #585b70; }
* { scrollbar-width: thin; scrollbar-color: #45475a #1e1e2e; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f0f1a; color: #cdd6f4; min-height: 100vh; }
.header { background: linear-gradient(135deg, #1e1e2e 0%, #313244 100%); padding: 16px 24px; border-bottom: 2px solid #45475a; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
.header h1 { color: #f5c2e7; font-size: 20px; white-space: nowrap; }
.header .status { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 5px; }
.status-dot.running { background: #22c55e; box-shadow: 0 0 8px #22c55e; }
.status-dot.stopped { background: #ef4444; }
.status-dot.scanning { background: #eab308; box-shadow: 0 0 8px #eab308; animation: pulse 1s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
.controls { background: #1e1e2e; padding: 12px 24px; border-bottom: 1px solid #313244; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.btn { padding: 6px 14px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; transition: all 0.2s; white-space: nowrap; }
.btn:hover { transform: translateY(-1px); }
.btn-start { background: #22c55e; color: #000; }
.btn-stop { background: #ef4444; color: #fff; }
.btn-scan { background: #3b82f6; color: #fff; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.toggle-group { display: flex; align-items: center; gap: 5px; font-size: 12px; color: #a6adc8; white-space: nowrap; }
.toggle { position: relative; width: 32px; height: 18px; background: #45475a; border-radius: 9px; cursor: pointer; transition: background 0.3s; flex-shrink: 0; }
.toggle.active { background: #22c55e; }
.toggle::after { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: #fff; border-radius: 50%; transition: transform 0.3s; }
.toggle.active::after { transform: translateX(14px); }
.stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; padding: 10px 24px; background: #1e1e2e; }
@media (max-width: 700px) { .stats { grid-template-columns: repeat(3, 1fr); } }
.stat-card { background: #313244; border-radius: 8px; padding: 8px; text-align: center; border: 1px solid #45475a; }
.stat-card .number { font-size: 22px; font-weight: 700; }
.stat-card .label { font-size: 11px; color: #a6adc8; margin-top: 2px; }
.stat-valid .number { color: #22c55e; }
.stat-noquota .number { color: #eab308; }
.stat-invalid .number { color: #ef4444; }
.stat-unknown .number { color: #6b7280; }
.stat-skip .number { color: #3b82f6; }
.stat-total .number { color: #f5c2e7; }
.config-panel { background: #1e1e2e; padding: 10px 24px; border-bottom: 1px solid #313244; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.config-group { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #a6adc8; white-space: nowrap; }
.config-group label { white-space: nowrap; }
.config-group input { width: 52px; padding: 3px 5px; border-radius: 4px; border: 1px solid #45475a; background: #313244; color: #cdd6f4; font-size: 12px; text-align: center; }
.config-group .unit { color: #6b7280; font-size: 11px; }
.btn-sm { padding: 3px 10px; border: none; border-radius: 4px; cursor: pointer; font-size: 11px; font-weight: 600; background: #45475a; color: #cdd6f4; white-space: nowrap; }
.main-content { display: grid; grid-template-columns: 1fr 1fr; gap: 0; min-height: calc(100vh - 340px); }
@media (max-width: 900px) { .main-content { grid-template-columns: 1fr; } }
.panel { padding: 12px; overflow: hidden; }
.panel-title { font-size: 14px; font-weight: 700; color: #f5c2e7; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid #45475a; }
.accounts-table { width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; min-width: 600px; }
.accounts-table th { text-align: left; padding: 6px 6px; color: #a6adc8; border-bottom: 1px solid #45475a; font-weight: 600; position: sticky; top: 0; background: #0f0f1a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; position: relative; user-select: none; }
.accounts-table th .resize-handle { position: absolute; right: 0; top: 0; bottom: 0; width: 4px; cursor: col-resize; background: transparent; }
.accounts-table th .resize-handle:hover, .accounts-table th .resize-handle.active { background: #89b4fa; }
.accounts-table td { padding: 4px 6px; border-bottom: 1px solid #1e1e2e; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 0; }
.accounts-table tr:hover { background: #1e1e2e; }
.badge { padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.badge-valid { background: #22c55e22; color: #22c55e; }
.badge-no_quota { background: #eab30822; color: #eab308; white-space: nowrap; }
.badge-invalid { background: #ef444422; color: #ef4444; }
.badge-unknown { background: #6b728022; color: #6b7280; }
.badge-skip { background: #3b82f622; color: #3b82f6; }
.log-container { height: calc(100vh - 380px); overflow-y: auto; background: #11111b; border-radius: 6px; padding: 8px; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; }
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-info { color: #3b82f6; }
.log-warn { color: #eab308; }
.log-error { color: #ef4444; }
.log-time { color: #6b7280; margin-right: 6px; }
.accounts-scroll { max-height: calc(100vh - 380px); overflow: auto; }
.last-check-time { position: relative; }
.relative-time { font-size: 10px; color: #6b7280; display: block; margin-top: 2px; }
.countdown { font-size: 10px; color: #6b7280; margin-top: 2px; }
.countdown.ready { color: #22c55e; }
.modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:1000; justify-content:center; align-items:center; }
.modal-overlay.show { display:flex; }
.modal { background:#1e1e2e; border:1px solid #45475a; border-radius:12px; padding:20px; width:500px; max-width:90vw; max-height:80vh; overflow-y:auto; }
.modal h2 { color:#f5c2e7; font-size:16px; margin-bottom:12px; }
.modal-close { float:right; background:none; border:none; color:#a6adc8; font-size:20px; cursor:pointer; }
.backup-item { display:flex; justify-content:space-between; align-items:center; padding:8px 12px; background:#313244; border-radius:6px; margin-bottom:6px; flex-wrap:wrap; gap:6px; }
.backup-item .name { color:#cdd6f4; font-size:13px; }
.backup-item .count { color:#a6adc8; font-size:12px; }
.btn-delete-backup { background:#f38ba8; color:#1e1e2e; border:none; padding:4px 10px; border-radius:4px; cursor:pointer; font-size:12px; margin-left:6px; }
.btn-delete-backup:hover { background:#eba0ac; }
.btn-restore { padding:4px 12px; border:none; border-radius:4px; cursor:pointer; font-size:11px; font-weight:600; background:#22c55e; color:#000; }
.btn-restore:hover { background:#16a34a; }
.restore-result { margin-top:10px; padding:8px; border-radius:6px; font-size:12px; display:none; }
.restore-result.ok { display:block; background:#22c55e22; color:#22c55e; }
.restore-result.error { display:block; background:#ef444422; color:#ef4444; }
.lang-switch { display:inline-flex; border-radius:6px; overflow:hidden; border:1px solid #45475a; margin-left:12px; flex-shrink:0; }
.lang-btn { padding:2px 10px; font-size:12px; cursor:pointer; border:none; background:#313244; color:#a6adc8; transition:all .15s; }
.lang-btn.active { background:#89b4fa; color:#1e1e2e; font-weight:600; }
.lang-btn:hover:not(.active) { background:#45475a; }
.auth-dir-bar { background:#1e1e2e; padding:8px 24px; border-bottom:1px solid #313244; display:flex; align-items:center; gap:8px; font-size:13px; }
.auth-dir-label { color:#a6adc8; white-space:nowrap; }
.auth-dir-path { color:#89b4fa; font-family:'Cascadia Code','Fira Code',monospace; font-size:12px; background:#313244; padding:3px 10px; border-radius:4px; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.btn-dir-change { background:#45475a; color:#cdd6f4; padding:4px 12px; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:600; white-space:nowrap; }
.btn-dir-change:hover { background:#585b70; }
.btn-dir-help { background:#313244; color:#f9e2af; padding:4px 12px; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:600; white-space:nowrap; }
.btn-dir-help:hover { background:#45475a; }
.help-modal-content { color:#cdd6f4; font-size:13px; line-height:1.8; }
.help-modal-content code { background:#313244; padding:2px 6px; border-radius:3px; font-family:'Cascadia Code','Fira Code',monospace; font-size:12px; color:#f9e2af; }
.help-modal-content .step { margin:8px 0; padding:8px 12px; background:#313244; border-radius:6px; border-left:3px solid #89b4fa; }
.help-modal-content .warn { background:#f59e0b22; border-left-color:#f59e0b; color:#f9e2af; }
</style>
</head>
<body>
<div class="header">
    <h1>🛡️ CLIProxyAPI codex自动禁用解禁</h1>
    <div class="status">
        <span id="monitorStatus"><span class="status-dot stopped"></span> 已停止</span>
        <span id="scanStatus" style="display:none"><span class="status-dot scanning"></span> 扫描中...</span>
        <span style="color:#a6adc8; font-size:12px" id="lastScan"></span>
        <div class="lang-switch">
            <button class="lang-btn active" id="langZh" onclick="setLang('zh')">中文</button>
            <button class="lang-btn" id="langEn" onclick="setLang('en')">English</button>
        </div>
    </div>
</div>
<div class="controls">
    <button class="btn btn-start" id="btnStart" onclick="startMonitor()">▶ 启动</button>
    <button class="btn btn-stop" id="btnStop" onclick="stopMonitor()" disabled>■ 停止</button>
    <button class="btn btn-scan" id="btnScan" onclick="runScan()">🔄 全量扫描</button>
    <button class="btn" style="background:#8b5cf6;color:#fff" onclick="backupNow()">💾 立即备份</button>
    <button class="btn" style="background:#22c55e;color:#fff" onclick="enableAll()">🔓 一键解禁所有</button>
    <button class="btn" style="background:#f59e0b;color:#000" onclick="showRestore()">♻️ 恢复文件</button>
    <button class="btn" style="background:#06b6d4;color:#fff" onclick="exportAccounts('csv')">📊 导出CSV</button>
    <button class="btn" style="background:#06b6d4;color:#fff" onclick="exportAccounts('json')">📊 导出JSON</button>
    <button class="btn" style="background:#ec4899;color:#fff" onclick="document.getElementById('importFile').click()">📥 导入账号</button>
    <input type="file" id="importFile" accept=".json,.csv" style="display:none" onchange="importAccounts(this)">
    <div class="toggle-group">
        <span>自动禁用</span>
        <div class="toggle active" id="toggleDisable" onclick="toggleSetting('auto_disable')"></div>
    </div>
    <div class="toggle-group">
        <span>自动解禁</span>
        <div class="toggle active" id="toggleEnable" onclick="toggleSetting('auto_enable')"></div>
    </div>
    <div class="toggle-group">
        <span>自动备份</span>
        <div class="toggle active" id="toggleBackup" onclick="toggleSetting('auto_backup')"></div>
    </div>
    <div class="toggle-group">
        <span>自动清理</span>
        <div class="toggle" id="toggleCleanup" onclick="toggleSetting('auto_cleanup')"></div>
        <span style="font-size:11px;color:#6b7280;margin-left:4px" id="maxBackupsLabel">保留</span>
        <input type="number" id="cfgMaxBackups" value="30" min="1" max="9999" style="width:48px;font-size:11px;padding:2px 4px;border:1px solid #4b5563;border-radius:4px;background:#1e1e2e;color:#cdd6f4;text-align:center" onchange="saveMaxBackups()">
        <span style="font-size:11px;color:#6b7280" id="maxBackupsUnit">份</span>
    </div>
    <div class="toggle-group">
        <span style="font-size:11px;color:#6b7280" id="lastBackupTime"></span>
    </div>
    <div class="toggle-group">
        <span id="autostartLabel">开机自启</span>
        <div class="toggle" id="toggleAutostart" onclick="toggleAutostart()"></div>
    </div>
</div>
<div class="auth-dir-bar">
    <span class="auth-dir-label" id="authDirLabel">📂 账号目录：</span>
    <span class="auth-dir-path" id="authDirPath">-</span>
    <button class="btn btn-dir-change" onclick="changeAuthDir()">✏️ <span id="btnChangeDirText">修改路径</span></button>
    <button class="btn btn-dir-help" onclick="showDirHelp()">❓ <span id="btnHelpText">教程</span></button>
</div>
<div class="stats" id="statsArea">
    <div class="stat-card stat-valid"><div class="number" id="countValid">0</div><div class="label">✅ 有效</div><div class="countdown" id="cdValid"></div></div>
    <div class="stat-card stat-noquota"><div class="number" id="countNoQuota">0</div><div class="label">⚠️ 无额度</div><div class="countdown" id="cdNoQuota"></div></div>
    <div class="stat-card stat-invalid"><div class="number" id="countInvalid">0</div><div class="label">❌ 失效</div><div class="countdown" id="cdInvalid"></div></div>
    <div class="stat-card stat-unknown"><div class="number" id="countUnknown">0</div><div class="label">❓ 未知</div><div class="countdown" id="cdUnknown"></div></div>
    <div class="stat-card stat-skip"><div class="number" id="countSkip">0</div><div class="label">⏭️ 跳过</div></div>
    <div class="stat-card stat-total"><div class="number" id="countTotal">0</div><div class="label">📊 总计</div></div>
</div>
<div class="config-panel">
    <div class="config-group"><label>✅有效</label><input type="number" id="cfgValid" min="10" step="10" value="120"><span class="unit">秒</span></div>
    <div class="config-group"><label>⚠️无额度</label><input type="number" id="cfgNoQuota" min="10" step="30" value="600"><span class="unit">秒</span></div>
    <div class="config-group"><label>❌失效</label><input type="number" id="cfgInvalid" min="10" step="60" value="1800"><span class="unit">秒</span></div>
    <div class="config-group"><label>❓未知</label><input type="number" id="cfgUnknown" min="10" step="30" value="300"><span class="unit">秒</span></div>
    <div class="config-group"><label>未知重试</label><input type="number" id="cfgRetryUnknown" min="0" step="1" value="3"><span class="unit">次</span></div>
    <div class="config-group"><label>失效重试</label><input type="number" id="cfgRetryInvalid" min="0" step="1" value="2"><span class="unit">次</span></div>
    <div class="config-group"><label>新文件检测</label><input type="number" id="cfgNewFileCheck" min="5" step="5" value="30"><span class="unit">秒</span></div>
    <button class="btn-sm" onclick="saveConfig()">💾 保存配置</button>
</div>
<div class="main-content">
    <div class="panel">
        <div class="panel-title">📋 账号列表</div>
        <div class="accounts-scroll">
            <table class="accounts-table">
                <thead><tr><th data-col="0">文件名</th><th data-col="1">邮箱</th><th data-col="2">状态</th><th data-col="3">原因</th><th data-col="4">重置时间</th><th data-col="5">上次检查</th></tr></thead>
                <tbody id="accountsBody"></tbody>
            </table>
        </div>
    </div>
    <div class="panel">
        <div class="panel-title">📝 运行日志</div>
        <div class="log-container" id="logContainer"></div>
    </div>
</div>
<div class="modal-overlay" id="restoreModal">
    <div class="modal">
        <button class="modal-close" onclick="closeRestore()">&times;</button>
        <h2>♻️ 从备份恢复文件</h2>
        <p style="color:#a6adc8;font-size:12px;margin-bottom:12px">只恢复 data/ 目录中不存在的文件，已有文件不会被覆盖</p>
        <div id="backupList"></div>
        <div class="restore-result" id="restoreResult"></div>
    </div>
</div>
<div class="modal-overlay" id="dirHelpModal">
    <div class="modal" style="width:560px">
        <button class="modal-close" onclick="closeDirHelp()">&times;</button>
        <h2 id="dirHelpTitle">📂 账号目录设置教程</h2>
        <div class="help-modal-content" id="dirHelpContent"></div>
    </div>
</div>
<div class="modal-overlay" id="cleanupWarnModal">
    <div class="modal" style="width:480px">
        <h2 id="cleanupWarnTitle">⚠️ 开启自动清理</h2>
        <p style="color:#f87171;font-size:13px;margin:12px 0" id="cleanupWarnText"></p>
        <p style="color:#a6adc8;font-size:12px;margin:8px 0" id="cleanupWarnDetail"></p>
        <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">
            <button class="btn" style="background:#4b5563;color:#fff" onclick="cancelCleanup()" id="cleanupCancelBtn">取消</button>
            <button class="btn" style="background:#ef4444;color:#fff" onclick="confirmCleanup()" id="cleanupConfirmBtn">确认开启</button>
        </div>
    </div>
</div>
<script>
let lastLogCount = 0;
let currentLang = localStorage.getItem('lang') || 'zh';

const i18n = {
    zh: {
        title: '🛡️ CLIProxyAPI codex自动禁用解禁',
        running: '运行中', stopped: '已停止', scanning: '扫描中...',
        lastScan: '上次扫描',
        btnStart: '▶ 启动', btnStop: '■ 停止', btnScan: '🔄 全量扫描',
        btnBackup: '💾 立即备份', btnEnableAll: '🔓 一键解禁所有',
        btnRestore: '♻️ 恢复文件', btnExportCsv: '📊 导出CSV', btnExportJson: '📊 导出JSON',
        btnImport: '📥 导入账号',
        autoDisable: '自动禁用', autoEnable: '自动解禁', autoBackup: '自动备份', autoCleanup: '自动清理',
        autostartOn: '开机自启: 开', autostartOff: '开机自启: 关', autostartEnable: '开启开机自启', autostartDisable: '关闭开机自启',
        autostartWarn: '将添加启动项到注册表，开机后自动运行 Monitor 服务。确定开启吗？',
        autostartOnlyWindows: '开机自启仅支持 Windows',
        valid: '✅ 有效', noQuota: '⚠️ 无额度', invalid: '❌ 失效', unknown: '❓ 未知', skip: '⏭️ 跳过', total: '📊 总计',
        cfgValid: '✅有效', cfgNoQuota: '⚠️无额度', cfgInvalid: '❌失效', cfgUnknown: '❓未知',
        cfgRetryUnknown: '未知重试', cfgRetryInvalid: '失效重试',
        cfgNewFileCheck: '新文件检测', cfgNewFileCheckUnit: '秒',
        unitSec: '秒', unitTimes: '次', btnSave: '💾 保存配置',
        accountList: '📋 账号列表', runLog: '📝 运行日志',
        thFilename: '文件名', thEmail: '邮箱', thStatus: '状态', thReason: '原因', thResetTime: '重置时间', thLastCheck: '上次检查',
        justNow: '刚刚', minAgo: '分钟前', hourAgo: '小时前', dayAgo: '天前',
        daysLater: '天后', hoursLater: '小时后', aboutToReset: '即将重置', daysAgo: '天前',
        backupTitle: '♻️ 从备份恢复文件', backupHint: '只恢复 data/ 目录中不存在的文件，已有文件不会被覆盖',
        noBackups: '暂无备份', btnRestoreFile: '恢复', btnDeleteBackup: '删除', restoring: '恢复中...',
        deleteBackupConfirm: '确定要删除此备份吗？此操作不可恢复。',
        deleteBackupOk: '✅ 已删除', deleteBackupFail: '❌ 删除失败',
        backupRunning: '备份中...', backupOk: '✅ 备份成功', backupFail: '❌ 失败', networkError: '❌ 网络错误',
        enableRunning: '解禁中...', enableOk: '✅ 已解禁', enableFail: '❌ 失败',
        enableConfirm: '确定要解禁所有账号吗？这将把所有 .invalid/.no_quota/.unknown 文件恢复为 .json',
        scanRunning: '扫描中...', scanOk: '✅ 扫描完成', scanFail: '❌ 失败',
        restoreOk: '✅ 恢复成功', restoreFail: '❌ 恢复失败',
        intervalSaved: '✅ 配置已保存',
        authDirLabel: '📂 账号目录：',
        btnChangeDir: '✏️ 修改路径', btnHelp: '❓ 教程',
        dirHelpTitle: '📂 账号目录设置教程',
        dirHelpStep1: '1️⃣ 默认账号目录',
        dirHelpStep1Text: '账号文件通常位于 CLIProxyAPI 安装目录下的 <code>data</code> 文件夹。<br>当前路径：<code id="helpCurrentDir">-</code>',
        dirHelpStep2: '2️⃣ 如何修改路径',
        dirHelpStep2Text: '点击「修改路径」按钮，输入新的账号目录完整路径，按回车确认。<br>路径必须是已存在的文件夹。',
        dirHelpStep3: '3️⃣ 启动时指定路径（推荐）',
        dirHelpStep3Text: '方式一：启动参数 <code>--auth-dir</code><br><code>python account_monitor_web.py --auth-dir "你的路径"</code><br>方式二：环境变量 <code>AUTH_DIR</code><br><code>set AUTH_DIR=D:\\path\\to\\data</code><br>方式三：配置文件 <code>config.yaml</code> 中设置 <code>auth-dir</code>',
        dirHelpStep4: '4️⃣ Docker 容器部署',
        dirHelpStep4Text: '使用 <code>docker-compose.yml</code> 部署时，通过环境变量配置：<br><code>AUTH_DIR=/app/data</code>（容器内路径）<br><code>CLIPROXYAPI_URL=http://cliproxyapi:8317</code>（容器间通信）<br>数据目录通过 volume 映射共享。',
        dirHelpWarn: '⚠️ 注意',
        dirHelpWarnText: '修改路径后需要重新启动监控才能扫描新目录的账号。<br>路径错误会导致扫描 0 个账号。<br>如果启动后显示 0 个账号，请检查路径是否正确。<br>Docker 中路径为容器内路径，不是宿主机路径。',
        changeDirPrompt: '请输入新的账号目录路径：',
        dirChanged: '✅ 路径已更新', dirChangeFail: '❌ 路径更新失败',
        statusValid: '有效', statusNoQuota: '无额度', statusInvalid: '失效', statusUnknown: '未知', statusSkip: '跳过',
        backupTime: '备份',
        backupCopies: '份',
        totalBackup: '总备份',
        cleanupWarnTitle: '⚠️ 开启自动清理',
        cleanupWarnText: '开启自动清理后，当备份数量超过上限时，最旧的备份将被自动删除，此操作不可恢复！',
        cleanupWarnDetail: '当前设置：最多保留 {max} 份备份，超出部分将按时间从旧到新删除。',
        cleanupWarnConfirm: '确认开启',
        cleanupWarnCancel: '取消',
        maxBackupsLabel: '保留',
        maxBackupsUnit: '份',
        importRunning: '导入中...', importOk: '✅ 导入', importFail: '❌ 失败', importNetworkError: '❌ 网络错误',
        scanSoon: '⏱ 即将扫描',
    },
    en: {
        title: '🛡️ CLIProxyAPI Codex Auto Disable/Enable',
        running: 'Running', stopped: 'Stopped', scanning: 'Scanning...',
        lastScan: 'Last scan',
        btnStart: '▶ Start', btnStop: '■ Stop', btnScan: '🔄 Full Scan',
        btnBackup: '💾 Backup Now', btnEnableAll: '🔓 Enable All',
        btnRestore: '♻️ Restore', btnExportCsv: '📊 Export CSV', btnExportJson: '📊 Export JSON',
        btnImport: '📥 Import',
        autoDisable: 'Auto Disable', autoEnable: 'Auto Enable', autoBackup: 'Auto Backup', autoCleanup: 'Auto Cleanup',
        autostartOn: 'Autostart: On', autostartOff: 'Autostart: Off', autostartEnable: 'Enable Autostart', autostartDisable: 'Disable Autostart',
        autostartWarn: 'This will add a startup entry to the registry to auto-run Monitor on boot. Enable?',
        autostartOnlyWindows: 'Autostart only supports Windows',
        valid: '✅ Valid', noQuota: '⚠️ No Quota', invalid: '❌ Invalid', unknown: '❓ Unknown', skip: '⏭️ Skip', total: '📊 Total',
        cfgValid: '✅Valid', cfgNoQuota: '⚠️No Quota', cfgInvalid: '❌Invalid', cfgUnknown: '❓Unknown',
        cfgRetryUnknown: 'Retry Unknown', cfgRetryInvalid: 'Retry Invalid',
        cfgNewFileCheck: 'New File Check', cfgNewFileCheckUnit: 'sec',
        unitSec: 'sec', unitTimes: 'times', btnSave: '💾 Save',
        accountList: '📋 Accounts', runLog: '📝 Logs',
        thFilename: 'Filename', thEmail: 'Email', thStatus: 'Status', thReason: 'Reason', thResetTime: 'Reset Time', thLastCheck: 'Last Check',
        justNow: 'just now', minAgo: 'm ago', hourAgo: 'h ago', dayAgo: 'd ago',
        daysLater: 'd later', hoursLater: 'h later', aboutToReset: 'Reset soon', daysAgo: 'd ago',
        backupTitle: '♻️ Restore from Backup', backupHint: 'Only restore files that do not exist in data/. Existing files will not be overwritten.',
        noBackups: 'No backups', btnRestoreFile: 'Restore', btnDeleteBackup: 'Delete', restoring: 'Restoring...',
        deleteBackupConfirm: 'Are you sure you want to delete this backup? This action cannot be undone.',
        deleteBackupOk: '✅ Deleted', deleteBackupFail: '❌ Delete failed',
        backupRunning: 'Backing up...', backupOk: '✅ Backup OK', backupFail: '❌ Failed', networkError: '❌ Network Error',
        enableRunning: 'Enabling...', enableOk: '✅ Enabled', enableFail: '❌ Failed',
        enableConfirm: 'Are you sure to enable all accounts? This will rename all .invalid/.no_quota/.unknown files back to .json',
        scanRunning: 'Scanning...', scanOk: '✅ Scan done', scanFail: '❌ Failed',
        restoreOk: '✅ Restored', restoreFail: '❌ Restore failed',
        intervalSaved: '✅ Config saved',
        authDirLabel: '📂 Auth Dir: ',
        btnChangeDir: '✏️ Change Path', btnHelp: '❓ Help',
        dirHelpTitle: '📂 Auth Directory Setup Guide',
        dirHelpStep1: '1️⃣ Default Auth Directory',
        dirHelpStep1Text: 'Account files are usually in the <code>data</code> folder under CLIProxyAPI installation.<br>Current path: <code id="helpCurrentDir">-</code>',
        dirHelpStep2: '2️⃣ How to Change Path',
        dirHelpStep2Text: 'Click "Change Path" button, enter the full path of the new auth directory, press Enter to confirm.<br>The path must be an existing folder.',
        dirHelpStep3: '3️⃣ Specify Path at Startup (Recommended)',
        dirHelpStep3Text: 'Option 1: Startup argument <code>--auth-dir</code><br><code>python account_monitor_web.py --auth-dir "your_path"</code><br>Option 2: Environment variable <code>AUTH_DIR</code><br><code>export AUTH_DIR=/path/to/data</code><br>Option 3: Config file <code>config.yaml</code> set <code>auth-dir</code>',
        dirHelpStep4: '4️⃣ Docker Container Deployment',
        dirHelpStep4Text: 'When deploying with <code>docker-compose.yml</code>, configure via environment variables:<br><code>AUTH_DIR=/app/data</code> (container path)<br><code>CLIPROXYAPI_URL=http://cliproxyapi:8317</code> (inter-container communication)<br>Data directory shared via volume mapping.',
        dirHelpWarn: '⚠️ Note',
        dirHelpWarnText: 'After changing the path, restart the monitor to scan accounts in the new directory.<br>Wrong path will result in 0 accounts scanned.<br>If 0 accounts are shown after startup, check if the path is correct.<br>In Docker, use container paths, not host paths.',
        changeDirPrompt: 'Enter new auth directory path:',
        dirChanged: '✅ Path updated', dirChangeFail: '❌ Path update failed',
        statusValid: 'Valid', statusNoQuota: 'No Quota', statusInvalid: 'Invalid', statusUnknown: 'Unknown', statusSkip: 'Skip',
        backupTime: 'Backup',
        backupCopies: 'copies',
        totalBackup: 'Total Backups',
        cleanupWarnTitle: '⚠️ Enable Auto Cleanup',
        cleanupWarnText: 'When auto cleanup is enabled, the oldest backups will be automatically deleted when the count exceeds the limit. This action cannot be undone!',
        cleanupWarnDetail: 'Current setting: keep up to {max} backups. Excess backups will be deleted from oldest to newest.',
        cleanupWarnConfirm: 'Confirm Enable',
        cleanupWarnCancel: 'Cancel',
        maxBackupsLabel: 'Keep',
        maxBackupsUnit: 'copies',
        importRunning: 'Importing...', importOk: '✅ Imported', importFail: '❌ Failed', importNetworkError: '❌ Network Error',
        scanSoon: '⏱ Scanning soon',
    }
};

function t(key) { return i18n[currentLang][key] || key; }

function setLang(lang) {
    currentLang = lang;
    localStorage.setItem('lang', lang);
    document.getElementById('langZh').className = 'lang-btn' + (lang === 'zh' ? ' active' : '');
    document.getElementById('langEn').className = 'lang-btn' + (lang === 'en' ? ' active' : '');
    applyLang();
}

function applyLang() {
    document.querySelector('.header h1').textContent = t('title');
    document.getElementById('btnStart').textContent = t('btnStart');
    document.getElementById('btnStop').textContent = t('btnStop');
    document.getElementById('btnScan').textContent = t('btnScan');
    document.querySelectorAll('.controls .btn')[3].textContent = t('btnBackup');
    document.querySelectorAll('.controls .btn')[4].textContent = t('btnEnableAll');
    document.querySelectorAll('.controls .btn')[5].textContent = t('btnRestore');
    document.querySelectorAll('.controls .btn')[6].textContent = t('btnExportCsv');
    document.querySelectorAll('.controls .btn')[7].textContent = t('btnExportJson');
    document.querySelectorAll('.controls .btn')[8].textContent = t('btnImport');
    document.querySelector('#toggleDisable').previousElementSibling.textContent = t('autoDisable');
    document.querySelector('#toggleEnable').previousElementSibling.textContent = t('autoEnable');
    document.querySelector('#toggleBackup').previousElementSibling.textContent = t('autoBackup');
    document.querySelector('#toggleCleanup').previousElementSibling.textContent = t('autoCleanup');
    document.getElementById('maxBackupsLabel').textContent = t('maxBackupsLabel');
    document.getElementById('maxBackupsUnit').textContent = t('maxBackupsUnit');
    document.querySelectorAll('.stat-card')[0].querySelector('.label').textContent = t('valid');
    document.querySelectorAll('.stat-card')[1].querySelector('.label').textContent = t('noQuota');
    document.querySelectorAll('.stat-card')[2].querySelector('.label').textContent = t('invalid');
    document.querySelectorAll('.stat-card')[3].querySelector('.label').textContent = t('unknown');
    document.querySelectorAll('.stat-card')[4].querySelector('.label').textContent = t('skip');
    document.querySelectorAll('.stat-card')[5].querySelector('.label').textContent = t('total');
    document.querySelectorAll('.config-group label')[0].textContent = t('cfgValid');
    document.querySelectorAll('.config-group label')[1].textContent = t('cfgNoQuota');
    document.querySelectorAll('.config-group label')[2].textContent = t('cfgInvalid');
    document.querySelectorAll('.config-group label')[3].textContent = t('cfgUnknown');
    document.querySelectorAll('.config-group label')[4].textContent = t('cfgRetryUnknown');
    document.querySelectorAll('.config-group label')[5].textContent = t('cfgRetryInvalid');
    document.querySelectorAll('.config-group label')[6].textContent = t('cfgNewFileCheck');
    document.querySelectorAll('.unit').forEach((el, i) => el.textContent = i < 4 ? t('unitSec') : (i === 6 ? t('cfgNewFileCheckUnit') : t('unitTimes')));
    document.querySelector('.config-panel .btn-sm').textContent = t('btnSave');
    document.querySelectorAll('.panel-title')[0].textContent = t('accountList');
    document.querySelectorAll('.panel-title')[1].textContent = t('runLog');
    const ths = document.querySelectorAll('.accounts-table thead th');
    if (ths.length >= 6) { ths[0].textContent = t('thFilename'); ths[1].textContent = t('thEmail'); ths[2].textContent = t('thStatus'); ths[3].textContent = t('thReason'); ths[4].textContent = t('thResetTime'); ths[5].textContent = t('thLastCheck'); }
    document.querySelector('.modal h2').textContent = t('backupTitle');
    document.querySelector('.modal p').textContent = t('backupHint');
    document.getElementById('authDirLabel').textContent = t('authDirLabel');
    document.getElementById('btnChangeDirText').textContent = t('btnChangeDir').split(' ').slice(1).join(' ');
    document.getElementById('btnHelpText').textContent = t('btnHelp').split(' ').slice(1).join(' ');
}

setLang(currentLang);

(function initResize() {
    const table = document.querySelector('.accounts-table');
    if (!table) return;
    const colWidths = [180, 150, 70, 150, 90, 100];
    const colgroup = document.createElement('colgroup');
    const cols = [];
    colWidths.forEach(w => {
        const col = document.createElement('col');
        col.style.width = w + 'px';
        colgroup.appendChild(col);
        cols.push(col);
    });
    table.insertBefore(colgroup, table.firstChild);
    const ths = table.querySelectorAll('thead th');
    ths.forEach((th, i) => {
        const handle = document.createElement('div');
        handle.className = 'resize-handle';
        th.appendChild(handle);
        let startX = 0, startW = 0;
        handle.addEventListener('mousedown', e => {
            e.preventDefault();
            startX = e.clientX;
            startW = colWidths[i];
            handle.classList.add('active');
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });
        function onMove(e) {
            const diff = e.clientX - startX;
            const newW = Math.max(40, startW + diff);
            colWidths[i] = newW;
            cols[i].style.width = newW + 'px';
        }
        function onUp() {
            handle.classList.remove('active');
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        }
    });
})();

function formatRelativeTime(timeStr) {
    if (!timeStr) return '';
    const now = new Date();
    const checkTime = new Date(timeStr);
    const diff = now - checkTime;
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return t('justNow');
    if (minutes < 60) return minutes + t('minAgo');
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + t('hourAgo');
    const days = Math.floor(hours / 24);
    return days + t('dayAgo');
}

function updateUI() {
    fetch('/api/status').then(r => r.json()).then(data => {
        document.getElementById('monitorStatus').innerHTML = '<span class="status-dot ' + (data.running ? 'running' : 'stopped') + '"></span> ' + (data.running ? t('running') : t('stopped'));
        document.getElementById('scanStatus').style.display = data.scanning ? 'inline' : 'none';
        document.getElementById('btnStart').disabled = data.running;
        document.getElementById('btnStop').disabled = !data.running;
        document.getElementById('btnScan').disabled = data.scanning;
        document.getElementById('lastScan').textContent = data.last_scan_time ? t('lastScan') + ': ' + data.last_scan_time.replace('T', ' ').substring(0, 19) : '';
        document.getElementById('toggleDisable').className = 'toggle' + (data.auto_disable ? ' active' : '');
        document.getElementById('toggleEnable').className = 'toggle' + (data.auto_enable ? ' active' : '');
        document.getElementById('toggleBackup').className = 'toggle' + (data.auto_backup ? ' active' : '');
        document.getElementById('toggleCleanup').className = 'toggle' + (data.auto_cleanup ? ' active' : '');
        fetch('/api/autostart').then(r=>r.json()).then(d => { _autostartEnabled = d.enabled; updateAutostartUI(); });
        const lbt = document.getElementById('lastBackupTime');
        const bSize = data.backup_size_kb >= 1024 ? (data.backup_size_kb / 1024).toFixed(1) + 'MB' : (data.backup_size_kb || 0) + 'KB';
        const bInfo = t('totalBackup') + ': ' + (data.backup_count || 0) + t('backupCopies') + ' ' + bSize;
        if (data.last_backup_time) {
            lbt.textContent = t('backupTime') + ': ' + data.last_backup_time.substring(11, 19) + ' | ' + bInfo;
        } else {
            lbt.textContent = bInfo;
        }
        document.getElementById('cfgValid').value = data.interval_valid;
        document.getElementById('cfgNoQuota').value = data.interval_no_quota;
        document.getElementById('cfgInvalid').value = data.interval_invalid;
        document.getElementById('cfgUnknown').value = data.interval_unknown;
        document.getElementById('cfgRetryUnknown').value = data.retry_unknown;
        document.getElementById('cfgRetryInvalid').value = data.retry_invalid;
        document.getElementById('cfgNewFileCheck').value = data.new_file_check_interval;
        document.getElementById('cfgMaxBackups').value = data.max_backups;
        document.getElementById('authDirPath').textContent = data.auth_dir || '-';

        let valid=0, noQuota=0, invalid=0, unknown=0, skip=0;
        const tbody = document.getElementById('accountsBody');
        let rows = '';
        const accounts = Object.values(data.accounts).sort((a,b) => {
            const order = {invalid:0, no_quota:1, unknown:2, valid:3, skip:4};
            return (order[a.status]??5) - (order[b.status]??5);
        });
        for (const a of accounts) {
            if (a.status === 'valid') valid++;
            else if (a.status === 'no_quota') noQuota++;
            else if (a.status === 'invalid') invalid++;
            else if (a.status === 'skip') skip++;
            else unknown++;
            const statusMap = {valid:t('statusValid'),no_quota:t('statusNoQuota'),invalid:t('statusInvalid'),unknown:t('statusUnknown'),skip:t('statusSkip')};
            const badge = '<span class="badge badge-' + a.status + '">' + (statusMap[a.status]||a.status) + '</span>';
            const checkTime = a.last_check ? a.last_check.replace('T',' ').substring(11,19) : '-';
            const relativeTime = a.last_check ? formatRelativeTime(a.last_check) : '';
            const resetTime = a.reset_at ? a.reset_at.replace('T',' ').substring(0,16) : '-';
            const resetDisplay = a.reset_at ? (() => { const d = new Date(a.reset_at); const now = new Date(); const diffMs = d - now; const diffDays = Math.ceil(diffMs / 86400000); const diffHours = Math.ceil(diffMs / 3600000); const dateStr = (d.getMonth()+1) + '/' + d.getDate(); if (diffDays > 1) return dateStr + '(' + diffDays + t('daysLater') + ')'; if (diffDays === 1) return dateStr + '(1' + t('daysLater') + ')'; if (diffHours > 0) return diffHours + t('hoursLater'); if (diffHours === 0) return t('aboutToReset'); return dateStr + '(' + Math.abs(diffDays) + t('daysAgo') + ')'; })() : '-';
            rows += '<tr><td title="' + a.filename + '">' + (a.filename.length>25 ? a.filename.substring(0,25)+'...' : a.filename) + '</td><td>' + (a.email||'-') + '</td><td>' + badge + '</td><td title="' + a.reason + '">' + (a.reason.length>20 ? a.reason.substring(0,20)+'...' : a.reason) + '</td><td class="reset-time" title="' + resetTime + '">' + resetDisplay + '</td><td class="last-check-time">' + checkTime + '<span class="relative-time">' + relativeTime + '</span></td></tr>';
        }
        tbody.innerHTML = rows;
        document.getElementById('countValid').textContent = valid;
        document.getElementById('countNoQuota').textContent = noQuota;
        document.getElementById('countInvalid').textContent = invalid;
        document.getElementById('countUnknown').textContent = unknown;
        document.getElementById('countSkip').textContent = skip;
        document.getElementById('countTotal').textContent = accounts.length;

        const ns = data.next_scan || {};
        function fmtCD(sec) {
            if (!sec && sec !== 0) return '';
            if (sec <= 0) return t('scanSoon');
            if (sec < 60) return sec + 's';
            return Math.floor(sec/60) + 'm' + (sec%60) + 's';
        }
        function setCD(id, sec) {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = fmtCD(sec);
            el.className = 'countdown' + (sec <= 0 ? ' ready' : '');
        }
        setCD('cdValid', ns.valid);
        setCD('cdNoQuota', ns.no_quota);
        setCD('cdInvalid', ns.invalid);
        setCD('cdUnknown', ns.unknown);
    });

    fetch('/api/logs?after=' + lastLogCount).then(r => r.json()).then(data => {
        if (data.logs && data.logs.length > 0) {
            const container = document.getElementById('logContainer');
            for (const log of data.logs) {
                const div = document.createElement('div');
                div.className = 'log-line log-' + log.level;
                div.innerHTML = '<span class="log-time">' + log.time.substring(11,19) + '</span>' + escapeHtml(log.message);
                container.appendChild(div);
            }
            lastLogCount += data.logs.length;
            container.scrollTop = container.scrollHeight;
        }
    });
}
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
function startMonitor() { fetch('/api/start', {method:'POST'}).then(()=>updateUI()); }
function stopMonitor() { fetch('/api/stop', {method:'POST'}).then(()=>updateUI()); }
function runScan() { fetch('/api/scan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({force:true})}).then(()=>updateUI()); }
function toggleSetting(key) {
    if (key === 'auto_cleanup') {
        var el = document.getElementById('toggleCleanup');
        if (!el.classList.contains('active')) {
            showCleanupWarn();
            return;
        }
    }
    fetch('/api/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:key})}).then(()=>updateUI());
}
function showCleanupWarn() {
    document.getElementById('cleanupWarnTitle').textContent = t('cleanupWarnTitle');
    document.getElementById('cleanupWarnText').textContent = t('cleanupWarnText');
    document.getElementById('cleanupWarnDetail').textContent = t('cleanupWarnDetail').replace('{max}', document.getElementById('cfgMaxBackups').value);
    document.getElementById('cleanupCancelBtn').textContent = t('cleanupWarnCancel');
    document.getElementById('cleanupConfirmBtn').textContent = t('cleanupWarnConfirm');
    document.getElementById('cleanupWarnModal').classList.add('show');
}
function cancelCleanup() {
    document.getElementById('cleanupWarnModal').classList.remove('show');
}
function confirmCleanup() {
    document.getElementById('cleanupWarnModal').classList.remove('show');
    fetch('/api/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:'auto_cleanup'})}).then(()=>updateUI());
}
var _autostartEnabled = false;
function toggleAutostart() {
    if (!_autostartEnabled) {
        if (!confirm(t('autostartWarn'))) return;
    }
    fetch('/api/autostart', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enable:!_autostartEnabled})}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            _autostartEnabled = data.enabled;
            updateAutostartUI();
        } else {
            alert(data.message || 'error');
        }
    });
}
function updateAutostartUI() {
    var el = document.getElementById('toggleAutostart');
    var label = document.getElementById('autostartLabel');
    if (_autostartEnabled) {
        el.classList.add('active');
        label.textContent = t('autostartOn');
    } else {
        el.classList.remove('active');
        label.textContent = t('autostartOff');
    }
}
function saveMaxBackups() {
    var val = parseInt(document.getElementById('cfgMaxBackups').value);
    if (isNaN(val) || val < 1) { val = 1; document.getElementById('cfgMaxBackups').value = 1; }
    fetch('/api/intervals', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({max_backups:val})});
}
function saveConfig() {
    const cfg = {
        interval_valid: parseInt(document.getElementById('cfgValid').value) || 120,
        interval_no_quota: parseInt(document.getElementById('cfgNoQuota').value) || 600,
        interval_invalid: parseInt(document.getElementById('cfgInvalid').value) || 1800,
        interval_unknown: parseInt(document.getElementById('cfgUnknown').value) || 300,
        retry_unknown: parseInt(document.getElementById('cfgRetryUnknown').value) || 3,
        retry_invalid: parseInt(document.getElementById('cfgRetryInvalid').value) || 2,
        new_file_check_interval: parseInt(document.getElementById('cfgNewFileCheck').value) || 30,
    };
    fetch('/api/intervals', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)}).then(()=>updateUI());
}
function backupNow() {
    const btn = event.target;
    const orig = t('btnBackup');
    btn.disabled = true;
    btn.textContent = t('backupRunning');
    fetch('/api/backup-now', {method:'POST'}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            btn.textContent = t('backupOk');
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
            updateUI();
        } else {
            btn.textContent = t('backupFail');
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
        }
    }).catch(() => {
        btn.textContent = t('networkError');
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
    });
}
function enableAll() {
    if (!confirm(t('enableConfirm'))) return;
    const btn = event.target;
    const orig = t('btnEnableAll');
    btn.disabled = true;
    btn.textContent = t('enableRunning');
    fetch('/api/enable-all', {method:'POST'}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            btn.textContent = t('enableOk') + data.enabled;
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
            updateUI();
        } else {
            btn.textContent = t('enableFail');
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
        }
    }).catch(() => {
        btn.textContent = t('networkError');
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
    });
}
function showRestore() {
    document.getElementById('restoreModal').classList.add('show');
    document.getElementById('restoreResult').className = 'restore-result';
    document.getElementById('restoreResult').textContent = '';
    fetch('/api/backups').then(r=>r.json()).then(data => {
        const list = document.getElementById('backupList');
        if (!data.backups || data.backups.length === 0) {
            list.innerHTML = '<p style="color:#a6adc8;font-size:12px">' + t('noBackups') + '</p>';
            return;
        }
        let html = '';
        for (const b of data.backups) {
            const label = b.name.replace('backup_','').replace('.zip','').replace(/_/g,' ').replace(/(\\d{4}) (\\d{2})(\\d{2}) (\\d{2})(\\d{2})(\\d{2})/, '$1-$2-$3 $4:$5:$6');
            html += '<div class="backup-item"><span class="name">' + label + '</span><span class="count">' + b.files + ' files ' + (b.size_kb ? b.size_kb + 'KB' : '') + '</span><button class="btn-restore" onclick="doRestore(&apos;' + b.name + '&apos;)">' + t('btnRestoreFile') + '</button><button class="btn-delete-backup" onclick="doDeleteBackup(&apos;' + b.name + '&apos;)">' + t('btnDeleteBackup') + '</button></div>';
        }
        list.innerHTML = html;
    });
}
function closeRestore() {
    document.getElementById('restoreModal').classList.remove('show');
}
function doRestore(backupName) {
    const resultEl = document.getElementById('restoreResult');
    resultEl.className = 'restore-result';
    resultEl.textContent = t('restoring');
    resultEl.style.display = 'block';
    fetch('/api/restore', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({backup:backupName})}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            resultEl.className = 'restore-result ok';
            resultEl.textContent = t('restoreOk') + ': ' + data.restored + ' restored, ' + data.skipped + ' existed';
        } else {
            resultEl.className = 'restore-result error';
            resultEl.textContent = t('restoreFail') + ': ' + (data.message || 'error');
        }
        updateUI();
    });
}
function doDeleteBackup(backupName) {
    if (!confirm(t('deleteBackupConfirm'))) return;
    fetch('/api/delete-backup', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({backup:backupName})}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            log_info_local(t('deleteBackupOk') + ': ' + backupName);
        } else {
            log_info_local(t('deleteBackupFail') + ': ' + (data.message || 'error'));
        }
        loadBackups();
        updateUI();
    });
}
function log_info_local(msg) {
    var el = document.getElementById('logArea');
    if (el) { el.textContent += '\\n' + msg; el.scrollTop = el.scrollHeight; }
}
function exportAccounts(fmt) {
    window.open('/api/export?format=' + fmt, '_blank');
}
function importAccounts(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);
    const btn = document.querySelector('[onclick*="importFile"]');
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = t('importRunning');
    fetch('/api/import', {method:'POST', body: formData}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            btn.textContent = t('importOk') + data.imported;
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
            updateUI();
        } else {
            btn.textContent = t('importFail') + ' ' + (data.message || '');
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
        }
    }).catch(() => {
        btn.textContent = t('importNetworkError');
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
    });
    input.value = '';
}
function changeAuthDir() {
    const current = document.getElementById('authDirPath').textContent;
    const newDir = prompt(t('changeDirPrompt'), current);
    if (!newDir || newDir === current) return;
    fetch('/api/auth-dir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({auth_dir:newDir})}).then(r=>r.json()).then(data => {
        if (data.status === 'ok') {
            document.getElementById('authDirPath').textContent = data.auth_dir;
            log_info_msg(t('dirChanged') + ': ' + data.auth_dir);
        } else {
            alert(t('dirChangeFail') + ': ' + (data.message || ''));
        }
    }).catch(() => {
        alert(t('dirChangeFail'));
    });
}
function log_info_msg(msg) {
    const container = document.getElementById('logContainer');
    const div = document.createElement('div');
    div.className = 'log-line log-info';
    const now = new Date();
    div.innerHTML = '<span class="log-time">' + now.toTimeString().substring(0,8) + '</span>' + escapeHtml(msg);
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}
function showDirHelp() {
    document.getElementById('dirHelpTitle').textContent = t('dirHelpTitle');
    document.getElementById('dirHelpContent').innerHTML =
        '<div class="step">' + t('dirHelpStep1') + '</div>' +
        '<div style="margin-left:12px;margin-bottom:8px">' + t('dirHelpStep1Text') + '</div>' +
        '<div class="step">' + t('dirHelpStep2') + '</div>' +
        '<div style="margin-left:12px;margin-bottom:8px">' + t('dirHelpStep2Text') + '</div>' +
        '<div class="step">' + t('dirHelpStep3') + '</div>' +
        '<div style="margin-left:12px;margin-bottom:8px">' + t('dirHelpStep3Text') + '</div>' +
        '<div class="step">' + t('dirHelpStep4') + '</div>' +
        '<div style="margin-left:12px;margin-bottom:8px">' + t('dirHelpStep4Text') + '</div>' +
        '<div class="step warn">' + t('dirHelpWarn') + '</div>' +
        '<div style="margin-left:12px">' + t('dirHelpWarnText') + '</div>';
    var helpDirEl = document.getElementById('helpCurrentDir');
    if (helpDirEl) {
        helpDirEl.textContent = document.getElementById('authDirPath').textContent;
    }
    document.getElementById('dirHelpModal').classList.add('show');
}
function closeDirHelp() {
    document.getElementById('dirHelpModal').classList.remove('show');
}
setInterval(updateUI, 2000);
updateUI();
</script>
</body>
</html>
"""

def _get_management_key(config_path: str = "") -> str:
    key = os.environ.get("CLIPROXYAPI_MANAGEMENT_KEY", "")
    if key:
        return key
    cfg = read_config(config_path)
    key = cfg.get("management_key", "")
    if key:
        return key
    rm = cfg.get("remote-management", {})
    secret = rm.get("secret-key", "") if isinstance(rm, dict) else ""
    if secret:
        return secret
    return ""

def main():
    global monitor_state
    monitor_state = MonitorState()
    parser = argparse.ArgumentParser(description="CLIProxyAPI Codex Account Monitor")
    parser.add_argument("--port", type=int, default=8320, help="Web UI port (default: 8320)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Web UI host (default: 0.0.0.0)")
    parser.add_argument("--management-key", type=str, default="", help="CLIProxyAPI management key")
    parser.add_argument("--config", type=str, default="", help="Path to CLIProxyAPI config.yaml")
    parser.add_argument("--auth-dir", type=str, default="", help="Path to CLIProxyAPI auth directory (overrides config)")
    args = parser.parse_args()
    if args.management_key:
        os.environ["CLIPROXYAPI_MANAGEMENT_KEY"] = args.management_key
    app = create_app(config_path=args.config, auth_dir_override=args.auth_dir)
    cfg = app.config["CLI_CONFIG"]
    auth_dir = app.config["AUTH_DIR"]
    print(f"CLIProxyAPI Codex Account Monitor starting on http://{args.host}:{args.port}")
    print(f"Auth dir: {auth_dir}")
    auth_dir_source = "--auth-dir" if args.auth_dir else ("AUTH_DIR env" if os.environ.get("AUTH_DIR") else ("config" if cfg.get("auth_dir") else "default"))
    print(f"Auth dir source: {auth_dir_source}")
    print(f"Management API: {_get_management_base_url(cfg)}")
    mgmt_key = _get_management_key(args.config)
    if mgmt_key:
        print(f"Management key: {'*' * 8}{mgmt_key[-4:]}")
    else:
        print("WARNING: Management key not set! Use --management-key or CLIPROXYAPI_MANAGEMENT_KEY env var")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
