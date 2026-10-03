"""Account registry and quota helpers for the Antigravity companion CLI.

Self-contained on purpose: this plugin installs separately from the
``antigravity-subscription-directsdk`` provider, so it reads the same on-disk
contract (the account registry JSON plus one isolated HOME per account) instead
of importing the provider package.

Contract read here:
  * registry file: ``~/.hermes/antigravity-accounts.json`` (env ``ANTIGRAVITY_ACCOUNTS_FILE``)
  * accounts dir:  ``~/.agy-accounts/<label>/`` (env ``ANTIGRAVITY_ACCOUNTS_DIR``)
  * token file:    ``<home>/.gemini/antigravity-cli/antigravity-oauth-token``
  * quota probe:   ``HOME=<home> agy -p "/usage" --output-format json`` (num_turns=0, no token cost)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any

TOKEN_FILENAMES = ("jetski-standalone-oauth-token", "antigravity-oauth-token")
QUOTA_TIMEOUT_S = 45
HOST_LABEL = "host"
BINARY_ENV_VARS = ("ANTIGRAVITY_COMMAND", "AGY_CLI_PATH", "ANTIGRAVITY_CLI_PATH")


def accounts_dir() -> Path:
    raw = os.environ.get("ANTIGRAVITY_ACCOUNTS_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".agy-accounts"


def registry_file() -> Path:
    raw = os.environ.get("ANTIGRAVITY_ACCOUNTS_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes" / "antigravity-accounts.json"


def agy_command() -> str:
    for var in BINARY_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "agy"


def token_path_for(home: Path | str) -> Path | None:
    base = Path(home).expanduser()
    for name in TOKEN_FILENAMES:
        candidate = base / ".gemini" / "antigravity-cli" / name
        if candidate.is_file():
            return candidate
    return None


def email_from_token(path: Path | None) -> str:
    """Return the account email from the id_token JWT payload. Never returns or prints the token."""
    if path is None:
        return ""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return ""
    token = data.get("id_token") or (data.get("token") or {}).get("id_token") or ""
    parts = str(token).split(".")
    if len(parts) < 2:
        return ""
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return ""
    return str(claims.get("email") or "")


def load_registry() -> dict[str, Any]:
    try:
        data = json.loads(registry_file().read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def registry_accounts() -> list[dict[str, Any]]:
    entries = load_registry().get("accounts") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def list_accounts(include_scan: bool = True) -> list[dict[str, Any]]:
    """Host account first, then registry entries, then any extra account dirs on disk."""
    seen_homes: set[str] = set()
    accounts: list[dict[str, Any]] = [
        {
            "label": HOST_LABEL,
            "home": Path.home(),
            "email": "",
            "eligible": None,
            "registered": False,
            "host": True,
        }
    ]
    seen_homes.add(str(Path.home().resolve()))

    for entry in registry_accounts():
        home_raw = str(entry.get("home_dir") or "").strip()
        if not home_raw:
            continue
        home = Path(home_raw).expanduser()
        key = str(home.resolve()) if home.exists() else str(home)
        if key in seen_homes:
            continue
        seen_homes.add(key)
        accounts.append(
            {
                "label": str(entry.get("label") or home.name),
                "home": home,
                "email": str(entry.get("email") or "") or email_from_token(token_path_for(home)),
                "eligible": entry.get("eligible"),
                "registered": True,
                "host": False,
            }
        )

    if include_scan and accounts_dir().is_dir():
        for child in sorted(accounts_dir().iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            key = str(child.resolve())
            if key in seen_homes:
                continue
            seen_homes.add(key)
            accounts.append(
                {
                    "label": child.name,
                    "home": child,
                    "email": email_from_token(token_path_for(child)),
                    "eligible": None,
                    "registered": False,
                    "host": False,
                }
            )
    return accounts


def resolve_account(label: str) -> dict[str, Any] | None:
    wanted = (label or "").strip()
    if not wanted:
        return None
    for account in list_accounts():
        if account["label"] == wanted or account["home"].name == wanted:
            return account
    return None


def probe(home: Path | str) -> tuple[bool | None, dict[str, float | None], str]:
    """(eligible, windows, note). eligible is None when it cannot be determined."""
    env = os.environ.copy()
    env["HOME"] = str(Path(home).expanduser())
    env.pop("ANTIGRAVITY_CONFIG_DIR", None)
    try:
        result = subprocess.run(
            [agy_command(), "-p", "/usage", "--output-format", "json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=QUOTA_TIMEOUT_S,
        )
    except FileNotFoundError:
        return None, {}, "agy not found"
    except subprocess.TimeoutExpired:
        return None, {}, "timeout"

    stdout = (result.stdout or "").strip()
    blob = f"{stdout}\n{result.stderr or ''}".lower()
    if "not eligible" in blob:
        return False, {}, "not eligible for Antigravity"
    if "sign in" in blob:
        return None, {}, "not signed in"
    if not stdout:
        lines = (result.stderr or "").strip().splitlines()
        return None, {}, (lines[0][:60] if lines else f"exit {result.returncode}")

    try:
        data = json.loads(stdout)
    except Exception:
        return None, {}, stdout.splitlines()[0][:60]

    groups = ((data.get("command") or {}).get("data") or {}).get("groups") or []
    windows: dict[str, float | None] = {}
    for group in groups:
        key = "gemini" if "gemini" in str(group.get("name", "")).lower() else "claude_gpt"
        for bucket in group.get("buckets") or []:
            window = str(bucket.get("window") or "").lower()
            fraction = bucket.get("remaining_fraction")
            windows[f"{key}_{window}"] = float(fraction) if isinstance(fraction, (int, float)) else None
    return True, windows, ""


def run_agy(home: Path | str, args: list[str]) -> int:
    """Run the official agy binary with HOME pointed at one account. Returns its exit code."""
    env = os.environ.copy()
    env["HOME"] = str(Path(home).expanduser())
    env.pop("ANTIGRAVITY_CONFIG_DIR", None)
    try:
        return subprocess.call([agy_command(), *args], env=env)
    except FileNotFoundError:
        print(f"agy not found on PATH (looked for '{agy_command()}')")
        return 127


def pct(value: float | None) -> str:
    return f"{round(value * 100)}%" if isinstance(value, (int, float)) else "-"


def window_pair(windows: dict[str, float | None], key: str) -> str:
    five, weekly = windows.get(f"{key}_5h"), windows.get(f"{key}_weekly")
    if five is None and weekly is None:
        return "unknown"
    return f"{pct(five)} / {pct(weekly)}"
