"""Account registry and quota-based rotation for Antigravity accounts."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .process import resolve_agy_command
except ImportError:
    from process import resolve_agy_command

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNTS_FILE = Path.home() / ".hermes" / "antigravity-accounts.json"
DEFAULT_ACCOUNTS_DIR = Path.home() / ".agy-accounts"

# Global in-memory cache for usage data: home_dir -> (timestamp, data)
_USAGE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_USAGE_CACHE_LOCK = threading.Lock()

# In-memory lease tracking to penalize busy accounts across concurrent turns
_LEASE_LOCK = threading.Lock()
_ACTIVE_LEASES: dict[str, int] = {}


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """Cross-platform file lock for atomic registry updates."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as f:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                yield
            finally:
                with contextlib.suppress(Exception):
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                with contextlib.suppress(Exception):
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def get_accounts_file_path() -> Path:
    """Return path to registry file, respecting ANTIGRAVITY_ACCOUNTS_FILE env var."""
    env_path = os.environ.get("ANTIGRAVITY_ACCOUNTS_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return DEFAULT_ACCOUNTS_FILE


def get_accounts_dir() -> Path:
    """Return path to accounts directory, respecting ANTIGRAVITY_ACCOUNTS_DIR env var."""
    env_dir = os.environ.get("ANTIGRAVITY_ACCOUNTS_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return DEFAULT_ACCOUNTS_DIR


def _default_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "accounts": [],
        "active_account": None,
    }


def load_accounts() -> dict[str, Any]:
    """Load account registry. Fails open on missing or corrupt files."""
    path = get_accounts_file_path()
    if not path.is_file():
        return _default_registry()

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            return _default_registry()
        data.setdefault("version", 1)
        if not isinstance(data.get("accounts"), list):
            data["accounts"] = []
        if "active_account" not in data:
            data["active_account"] = None
        return data
    except Exception as exc:
        logger.warning("Failed to read Antigravity accounts registry %s: %s (failing open)", path, exc)
        return _default_registry()


def save_accounts(data: dict[str, Any]) -> None:
    """Atomically save account registry with 0600 permissions under file lock."""
    path = get_accounts_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    lock_path = path.with_suffix(".lock")
    with _file_lock(lock_path):
        tmp_path = path.with_suffix(".tmp")
        try:
            content = json.dumps(data, indent=2)
            tmp_path.write_text(content, encoding="utf-8")
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass
            tmp_path.replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            if tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()


def sanitize_folder_name(name: str) -> str:
    """Sanitize a label or email for safe filesystem folder naming across OSes."""
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "-", name.strip())
    return sanitized or "account"


def find_account_token_path(home_dir: str | Path) -> Path | None:
    """Locate a valid OAuth token file inside an account home directory."""
    base = Path(home_dir).expanduser().resolve() / ".gemini" / "antigravity-cli"
    for filename in ("jetski-standalone-oauth-token", "antigravity-oauth-token"):
        cand = base / filename
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
        except OSError:
            pass
    return None


def extract_email_from_token_file(token_path: str | Path) -> str | None:
    """Decode JWT id_token payload from token file and extract email.

    Does not log or expose the raw token content.
    """
    try:
        content = Path(token_path).read_text(encoding="utf-8")
        data = json.loads(content)
        id_token = data.get("id_token")
        if not id_token or not isinstance(id_token, str):
            return None
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))
        email = payload.get("email")
        if email and isinstance(email, str) and email.strip():
            return email.strip()
    except Exception as exc:
        logger.debug("Failed to extract email from token file %s: %s", token_path, exc)
    return None


def check_account_eligibility(
    home_dir: str | Path,
    timeout: float = 30.0,
) -> tuple[bool, dict[str, Any] | None]:
    """Check account eligibility via `agy -p /usage --output-format json`.

    Returns (is_eligible, parsed_usage_or_None).
    """
    norm_home = str(Path(home_dir).expanduser().resolve())
    try:
        cmd = resolve_agy_command()
    except Exception:
        return False, None

    env = os.environ.copy()
    env["HOME"] = norm_home
    env.pop("ANTIGRAVITY_CONFIG_DIR", None)

    try:
        res = subprocess.run(
            [cmd, "-p", "/usage", "--output-format", "json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        combined = f"{res.stdout}\n{res.stderr}".lower()
        if "eligibility check failed" in combined or "not eligible" in combined:
            return False, None
        if res.returncode != 0:
            return False, None

        parsed = _parse_usage_json(res.stdout)
        if parsed is not None:
            with _USAGE_CACHE_LOCK:
                _USAGE_CACHE[norm_home] = (time.monotonic(), parsed)
            return True, parsed

        try:
            raw_data = json.loads(res.stdout)
            if isinstance(raw_data, dict) and raw_data.get("status") == "SUCCESS":
                return True, None
        except Exception:
            pass

        return False, None
    except Exception as exc:
        logger.debug("Eligibility check error for %s: %s", norm_home, exc)
        return False, None


def add_account(
    label: str,
    home_dir: str,
    enabled: bool = True,
    eligible: bool = True,
    email: str | None = None,
) -> dict[str, Any]:
    """Add or update an account entry in the registry."""
    norm_label = label.strip()
    norm_home = str(Path(home_dir).expanduser().resolve())
    data = load_accounts()
    accounts = data.get("accounts", [])

    target = None
    for acc in accounts:
        if acc.get("label") == norm_label:
            target = acc
            break

    if target is not None:
        target["home_dir"] = norm_home
        target["enabled"] = bool(enabled)
        target["eligible"] = bool(eligible)
        if email:
            target["email"] = email
    else:
        target = {
            "label": norm_label,
            "home_dir": norm_home,
            "enabled": bool(enabled),
            "eligible": bool(eligible),
            "last_used": 0.0,
            "cooldown_until": 0.0,
        }
        if email:
            target["email"] = email
        accounts.append(target)

    data["accounts"] = accounts
    save_accounts(data)
    return target


def remove_account(label: str) -> bool:
    """Remove an account from registry. Preserves home directory on disk."""
    norm_label = label.strip()
    data = load_accounts()
    accounts = data.get("accounts", [])
    filtered = [acc for acc in accounts if acc.get("label") != norm_label]

    if len(filtered) == len(accounts):
        return False

    data["accounts"] = filtered
    if data.get("active_account") == norm_label:
        data["active_account"] = None
    save_accounts(data)
    return True


def list_accounts() -> list[dict[str, Any]]:
    """List all accounts in registry."""
    return list(load_accounts().get("accounts", []))


def get_active() -> str | None:
    """Get currently active account label, if set."""
    return load_accounts().get("active_account")


def set_active(label: str | None) -> bool:
    """Set or clear the active account label."""
    data = load_accounts()
    if label is None:
        data["active_account"] = None
        save_accounts(data)
        return True

    norm_label = label.strip()
    accounts = data.get("accounts", [])
    if any(acc.get("label") == norm_label for acc in accounts):
        data["active_account"] = norm_label
        save_accounts(data)
        return True
    return False


def set_cooldown(label: str, duration_seconds: float = 900.0, until: float | None = None) -> None:
    """Place an account on cooldown until a timestamp or for duration_seconds."""
    norm_label = label.strip()
    data = load_accounts()
    accounts = data.get("accounts", [])
    until_ts = until if until is not None else (time.time() + duration_seconds)
    found = False
    for acc in accounts:
        if acc.get("label") == norm_label:
            acc["cooldown_until"] = float(until_ts)
            found = True
            break
    if found:
        save_accounts(data)


def update_last_used(label: str) -> None:
    """Record account usage timestamp."""
    norm_label = label.strip()
    data = load_accounts()
    accounts = data.get("accounts", [])
    found = False
    for acc in accounts:
        if acc.get("label") == norm_label:
            acc["last_used"] = time.time()
            found = True
            break
    if found:
        save_accounts(data)


def parse_reset_time(reset_time_str: str | None) -> float | None:
    """Parse ISO 8601 reset_time string to Unix epoch timestamp."""
    if not reset_time_str or not isinstance(reset_time_str, str):
        return None
    try:
        clean = reset_time_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.timestamp()
    except Exception:
        return None


def _parse_usage_json(raw_json: str) -> dict[str, Any] | None:
    """Parse `agy -p /usage --output-format json` payload into group buckets."""
    try:
        data = json.loads(raw_json)
        cmd_data = data.get("command", {}).get("data", {})
        groups = cmd_data.get("groups", [])
        if not groups and isinstance(data.get("groups"), list):
            groups = data["groups"]

        parsed: dict[str, Any] = {
            "gemini": {"5h": {"remaining_fraction": 1.0, "reset_time": None}, "weekly": {"remaining_fraction": 1.0, "reset_time": None}},
            "claude_gpt": {"5h": {"remaining_fraction": 1.0, "reset_time": None}, "weekly": {"remaining_fraction": 1.0, "reset_time": None}},
        }

        for grp in groups:
            grp_name = grp.get("name", "").lower()
            if "gemini" in grp_name:
                target = parsed["gemini"]
            elif any(k in grp_name for k in ("claude", "gpt", "3p")):
                target = parsed["claude_gpt"]
            else:
                continue

            for bucket in grp.get("buckets", []):
                bid = bucket.get("id", "").lower()
                window = bucket.get("window", "").lower()
                rem = bucket.get("remaining_fraction")
                reset_t = bucket.get("reset_time")
                try:
                    frac = float(rem) if rem is not None else 1.0
                except (ValueError, TypeError):
                    frac = 1.0

                if window == "5h" or "5h" in bid:
                    target["5h"] = {"remaining_fraction": frac, "reset_time": reset_t}
                elif window == "weekly" or "weekly" in bid:
                    target["weekly"] = {"remaining_fraction": frac, "reset_time": reset_t}

        return parsed
    except Exception as exc:
        logger.debug("Failed to parse Antigravity usage payload: %s", exc)
        return None


def fetch_usage_for_home(
    home_dir: str,
    timeout: float = 30.0,
    cached: bool = True,
    max_cache_age: float = 60.0,
) -> dict[str, Any] | None:
    """Query quota for home_dir using `agy -p /usage --output-format json`."""
    norm_home = str(Path(home_dir).expanduser().resolve())
    now = time.monotonic()

    if cached:
        with _USAGE_CACHE_LOCK:
            if norm_home in _USAGE_CACHE:
                ts, cached_data = _USAGE_CACHE[norm_home]
                if now - ts < max_cache_age:
                    return cached_data

    try:
        cmd = resolve_agy_command()
    except Exception:
        return None

    env = os.environ.copy()
    env["HOME"] = norm_home
    env.pop("ANTIGRAVITY_CONFIG_DIR", None)

    try:
        res = subprocess.run(
            [cmd, "-p", "/usage", "--output-format", "json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if res.returncode != 0:
            return None
        parsed = _parse_usage_json(res.stdout)
        if parsed is not None:
            with _USAGE_CACHE_LOCK:
                _USAGE_CACHE[norm_home] = (now, parsed)
        return parsed
    except Exception as exc:
        logger.debug("Usage check failed for %s: %s", norm_home, exc)
        return None


def get_rotation_mode() -> str:
    """Return rotation mode from ANTIGRAVITY_ROTATION env var ('quota', 'round_robin', 'off')."""
    return os.environ.get("ANTIGRAVITY_ROTATION", "off").strip().lower()


@contextlib.contextmanager
def lease_account(label: str):
    """Thread-safe context manager tracking in-flight turn leases per account."""
    norm = label.strip()
    with _LEASE_LOCK:
        _ACTIVE_LEASES[norm] = _ACTIVE_LEASES.get(norm, 0) + 1
    try:
        yield
    finally:
        with _LEASE_LOCK:
            _ACTIVE_LEASES[norm] = max(0, _ACTIVE_LEASES.get(norm, 1) - 1)
            if _ACTIVE_LEASES[norm] == 0:
                _ACTIVE_LEASES.pop(norm, None)


def get_lease_count(label: str) -> int:
    """Return active lease count for an account label."""
    with _LEASE_LOCK:
        return _ACTIVE_LEASES.get(label.strip(), 0)


def calculate_score(
    f_5h: float,
    f_weekly: float,
    in_cooldown: bool,
    lease_count: int = 0,
    eligible: bool = True,
) -> float:
    """Calculate account selection score with hard gate at 0%, cooldown, or ineligible."""
    if not eligible or in_cooldown or f_5h <= 0.0 or f_weekly <= 0.0:
        return 0.0
    raw_score = f_5h * (f_weekly ** 2)
    # Penalty for active leases ensures concurrent turns distribute evenly
    return raw_score / (1.0 + lease_count)


def is_quota_error(error: Exception | str) -> bool:
    """Detect quota exhaustion across error types, messages, and codes."""
    msg = str(error).lower()
    return any(
        term in msg
        for term in (
            "resource_exhausted",
            "individual quota reached",
            "quota exceeded",
            "quota limit",
            "rate limit",
            "429",
        )
    )


def pick_account(
    model: str | None = None,
    exclude_labels: set[str] | list[str] | None = None,
) -> dict[str, Any] | None:
    """Pick the best available Antigravity account based on configured rotation mode.

    Fails open by returning None on any registry missing/corrupt or mode 'off'.
    """
    try:
        mode = get_rotation_mode()
        if mode == "off":
            return None

        data = load_accounts()
        accounts = data.get("accounts", [])
        if not accounts:
            return None

        excluded = set(exclude_labels or [])
        enabled_accounts = [
            acc for acc in accounts
            if acc.get("enabled", True)
            and acc.get("eligible", True)
            and acc.get("label") not in excluded
        ]
        if not enabled_accounts:
            return None

        now = time.time()
        m_lower = (model or "").lower()
        group_key = "claude_gpt" if ("claude" in m_lower or "gpt" in m_lower) else "gemini"

        if mode == "round_robin":
            eligible = [
                acc for acc in enabled_accounts
                if acc.get("cooldown_until", 0.0) <= now
            ]
            if not eligible:
                return None
            # Sort by active leases first (idle accounts preferred), then least recently used
            eligible.sort(key=lambda a: (get_lease_count(a["label"]), a.get("last_used", 0.0)))
            return eligible[0]

        # Default mode: 'quota'
        scored: list[tuple[float, dict[str, Any]]] = []
        for acc in enabled_accounts:
            in_cooldown = acc.get("cooldown_until", 0.0) > now
            f_5h = 1.0
            f_weekly = 1.0
            usage = fetch_usage_for_home(acc.get("home_dir", ""), cached=True)
            if usage and group_key in usage:
                f_5h = usage[group_key].get("5h", {}).get("remaining_fraction", 1.0)
                f_weekly = usage[group_key].get("weekly", {}).get("remaining_fraction", 1.0)

            score = calculate_score(
                f_5h=f_5h,
                f_weekly=f_weekly,
                in_cooldown=in_cooldown,
                lease_count=get_lease_count(acc["label"]),
                eligible=acc.get("eligible", True),
            )
            if score > 0.0:
                scored.append((score, acc))

        if not scored:
            return None

        # Highest score first
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    except Exception as exc:
        logger.warning("pick_account failed open: %s", exc)
        return None
