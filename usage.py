"""Subscription usage and quota tracking for Google Antigravity / Gemini."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
else:
    try:
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    except ImportError:
        @dataclass(frozen=True)
        class AccountUsageWindow:
            label: str
            used_percent: Optional[float] = None
            reset_at: Optional[datetime] = None
            detail: Optional[str] = None

        @dataclass(frozen=True)
        class AccountUsageSnapshot:
            provider: str
            source: str
            fetched_at: datetime
            title: str = "Account limits"
            plan: Optional[str] = None
            windows: tuple[Any, ...] = ()
            details: tuple[str, ...] = ()
            unavailable_reason: Optional[str] = None
            raw: Optional[dict] = None

try:
    from .client import _force_rmtree
    from .process import build_child_env, resolve_agy_command, setup_isolated_home
except ImportError:
    from client import _force_rmtree
    from process import build_child_env, resolve_agy_command, setup_isolated_home

logger = logging.getLogger(__name__)

# Cache TTL in seconds (45-60s range requested; 60s avoids unnecessary agy subprocesses)
USAGE_CACHE_TTL_SECONDS = 60.0

_cache_lock = threading.Lock()
_cached_usage: SubscriptionUsage | None = None
_cached_timestamp: float = 0.0


@dataclass(frozen=True)
class UsageBucket:
    id: str
    name: str | None
    window: str
    remaining_fraction: float
    reset_time: str
    reset_at: datetime | None


@dataclass(frozen=True)
class UsageGroup:
    name: str
    description: str | None
    buckets: tuple[UsageBucket, ...]


@dataclass(frozen=True)
class SubscriptionUsage:
    groups: tuple[UsageGroup, ...]
    fetched_at: datetime
    raw: dict[str, Any]


def _parse_iso_datetime(val: Any) -> datetime | None:
    """Parse ISO 8601 timestamp string into timezone-aware UTC datetime."""
    if not val or not isinstance(val, str):
        return None
    val_str = val.strip()
    if not val_str:
        return None
    if val_str.endswith("Z"):
        val_str = val_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(val_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def format_countdown(dt: datetime | None, now: datetime | None = None) -> str:
    """Format remaining time until reset into a human-readable countdown string."""
    if dt is None:
        return "unknown"
    if now is None:
        now = datetime.now(timezone.utc)
    total_seconds = int((dt - now).total_seconds())
    if total_seconds <= 0:
        return "now"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def format_reset_time(dt: datetime | None) -> str:
    """Format reset datetime string in UTC."""
    if dt is None:
        return "unknown"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_agy_usage(payload: Any) -> SubscriptionUsage | None:
    """Parse raw agy CLI usage output (JSON object, string, or stream-json) into SubscriptionUsage."""
    if not payload:
        return None

    raw_dict: dict[str, Any] | None = None
    if isinstance(payload, dict):
        raw_dict = payload
    elif isinstance(payload, str):
        cleaned = payload.strip()
        if not cleaned:
            return None
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                raw_dict = parsed
        except json.JSONDecodeError:
            # Maybe stream-json (multiple JSON lines)
            for line in cleaned.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    item = json.loads(line_str)
                    if isinstance(item, dict):
                        # Look for event=command_result or result with command
                        if item.get("command", {}).get("name") == "usage":
                            raw_dict = item
                            break
                        if item.get("result", {}).get("command", {}).get("name") == "usage":
                            raw_dict = item["result"]
                            break
                except json.JSONDecodeError:
                    continue

    if not raw_dict:
        return None

    # Extract command node
    cmd_node = raw_dict.get("command")
    if not cmd_node and isinstance(raw_dict.get("result"), dict):
        cmd_node = raw_dict["result"].get("command")

    if not isinstance(cmd_node, dict):
        return None

    data = cmd_node.get("data")
    if not isinstance(data, dict):
        return None

    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        return None

    parsed_groups: list[UsageGroup] = []
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "Unknown").strip()
        group_desc = group.get("description")
        group_desc_str = str(group_desc).strip() if group_desc else None

        raw_buckets = group.get("buckets")
        parsed_buckets: list[UsageBucket] = []
        if isinstance(raw_buckets, list):
            for bucket in raw_buckets:
                if not isinstance(bucket, dict):
                    continue
                b_id = str(bucket.get("id") or "").strip()
                b_name = bucket.get("name")
                b_name_str = str(b_name).strip() if b_name else None
                b_window = str(bucket.get("window") or "").strip()
                try:
                    frac = float(bucket.get("remaining_fraction", 0.0))
                    # Clamp between 0.0 and 1.0
                    frac = max(0.0, min(1.0, frac))
                except (ValueError, TypeError):
                    frac = 0.0
                b_reset_time = str(bucket.get("reset_time") or "").strip()
                b_reset_at = _parse_iso_datetime(b_reset_time)

                parsed_buckets.append(
                    UsageBucket(
                        id=b_id,
                        name=b_name_str,
                        window=b_window,
                        remaining_fraction=frac,
                        reset_time=b_reset_time,
                        reset_at=b_reset_at,
                    )
                )

        parsed_groups.append(
            UsageGroup(
                name=group_name,
                description=group_desc_str,
                buckets=tuple(parsed_buckets),
            )
        )

    return SubscriptionUsage(
        groups=tuple(parsed_groups),
        fetched_at=datetime.now(timezone.utc),
        raw=raw_dict,
    )


def _query_agy_usage(timeout: float = 8.0) -> SubscriptionUsage | None:
    """Run `agy -p "/usage" --output-format json` under an isolated HOME environment."""
    cmd = resolve_agy_command()
    tmp_dir = tempfile.mkdtemp(prefix="hermes_agy_usage_")
    try:
        isolated_home, _ = setup_isolated_home(tmp_dir)
        env = build_child_env(isolated_home)
        res = subprocess.run(
            [cmd, "-p", "/usage", "--output-format", "json"],
            env=env,
            cwd=tmp_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode != 0:
            logger.warning(
                "Antigravity usage process returned non-zero (%d): %s",
                res.returncode,
                res.stderr.strip() or res.stdout.strip(),
            )
            return None
        return parse_agy_usage(res.stdout)
    except FileNotFoundError:
        logger.warning("Antigravity binary not found: %s", cmd)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Antigravity usage query timed out after %.1fs", timeout)
        return None
    except Exception as exc:
        logger.warning("Antigravity usage query failed: %s", exc)
        return None
    finally:
        _force_rmtree(tmp_dir)


def fetch_subscription_usage(
    force_refresh: bool = False,
    timeout: float = 8.0,
) -> SubscriptionUsage | None:
    """Thread-safe, cached fetch of Antigravity subscription usage.

    Caches results for USAGE_CACHE_TTL_SECONDS (default 60s) to avoid spawning
    subprocesses on every status bar tick or turn. Pass `force_refresh=True`
    when the user explicitly asks to refresh (e.g. /agy-usage --refresh or
    antigravity_usage tool force_refresh).
    """
    global _cached_usage, _cached_timestamp

    now = time.time()
    with _cache_lock:
        if not force_refresh and _cached_usage is not None and (now - _cached_timestamp) < USAGE_CACHE_TTL_SECONDS:
            return _cached_usage

        usage = _query_agy_usage(timeout=timeout)
        if usage is not None:
            _cached_usage = usage
            _cached_timestamp = time.time()
            return usage

        # Fallback to stale cache if available on transient failure
        if _cached_usage is not None:
            logger.debug("Using stale cached usage after query failure")
            return _cached_usage
        return None


def _window_label(group_name: str, window: str) -> str:
    """Generate a compact, readable label for AccountUsageWindow."""
    gn = group_name.strip()
    if "gemini" in gn.lower():
        prefix = "Gemini"
    elif "claude" in gn.lower() or "gpt" in gn.lower():
        prefix = "Claude/GPT"
    else:
        prefix = gn

    w = window.strip().lower()
    if w == "5h":
        w_label = "5h"
    elif w == "weekly":
        w_label = "Weekly"
    else:
        w_label = window.strip()
    return f"{prefix} ({w_label})"


def to_account_usage_snapshot(usage: SubscriptionUsage) -> AccountUsageSnapshot:
    """Convert SubscriptionUsage into a Hermes AccountUsageSnapshot."""
    windows: list[AccountUsageWindow] = []
    for group in usage.groups:
        for bucket in group.buckets:
            used_percent = max(0.0, min(100.0, (1.0 - bucket.remaining_fraction) * 100.0))
            label = _window_label(group.name, bucket.window)
            windows.append(
                AccountUsageWindow(
                    label=label,
                    used_percent=used_percent,
                    reset_at=bucket.reset_at,
                )
            )

    return AccountUsageSnapshot(
        provider="antigravity-subscription-directsdk",
        source="agy_cli",
        fetched_at=usage.fetched_at,
        title="Antigravity subscription",
        windows=tuple(windows),
        raw=usage.raw,
    )


def get_account_usage_snapshot(force_refresh: bool = False) -> AccountUsageSnapshot | None:
    """Official Hermes account usage entrypoint for ProviderProfile.fetch_account_usage."""
    usage = fetch_subscription_usage(force_refresh=force_refresh)
    if usage is None:
        return None
    return to_account_usage_snapshot(usage)


def render_usage_text(usage: SubscriptionUsage) -> str:
    """Render structured, human-readable quota text for agent tool and slash command.

    Displays groups (Gemini Models / Claude and GPT models), percentage remaining
    and used for 5-hour and weekly limits, reset times, and countdowns.
    """
    if not usage or not usage.groups:
        return "No Antigravity subscription quota data available."

    lines = ["Antigravity Subscription Quota:"]
    now = datetime.now(timezone.utc)

    for group in usage.groups:
        lines.append(f"\n{group.name}:")
        if not group.buckets:
            lines.append("  • No quota buckets reported.")
            continue
        for bucket in group.buckets:
            pct_remaining = bucket.remaining_fraction * 100.0
            pct_used = max(0.0, 100.0 - pct_remaining)

            w = bucket.window.lower()
            if "5h" in w:
                window_display = "5-hour limit"
            elif "week" in w:
                window_display = "Weekly limit"
            else:
                window_display = f"{bucket.window} limit"

            countdown = format_countdown(bucket.reset_at, now)
            reset_str = format_reset_time(bucket.reset_at)

            if bucket.reset_at:
                reset_info = f"resets {countdown} ({reset_str})"
            else:
                reset_info = "reset time unknown"

            lines.append(
                f"  • {window_display}: {pct_remaining:.1f}% remaining ({pct_used:.1f}% used) • {reset_info}"
            )

    return "\n".join(lines)


ANTIGRAVITY_USAGE_TOOL_SCHEMA: dict[str, Any] = {
    "name": "antigravity_usage",
    "description": (
        "Check current Google Antigravity / Gemini subscription quota and limits "
        "for Gemini and Claude/GPT models, including remaining percentages, reset times, "
        "and countdowns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force_refresh": {
                "type": "boolean",
                "description": (
                    "If true, bypass the local cache and query agy directly for fresh "
                    "quota data. Default false."
                ),
                "default": False,
            }
        },
        "required": [],
    },
}


def handle_antigravity_usage_tool(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Tool handler for agent tool `antigravity_usage`."""
    args = args or {}
    force_refresh = bool(args.get("force_refresh", False))
    usage = fetch_subscription_usage(force_refresh=force_refresh)
    if usage is None:
        return (
            "Unable to fetch Antigravity subscription quota. "
            "Ensure the agy CLI is installed, authenticated, and accessible."
        )
    return render_usage_text(usage)


def handle_agy_usage_slash_command(raw_args: str = "") -> str:
    """Handler for in-session slash command /agy-usage."""
    args_lower = (raw_args or "").strip().lower().split()
    force_refresh = any(
        arg in ("--refresh", "-r", "--force", "refresh", "force") for arg in args_lower
    )
    usage = fetch_subscription_usage(force_refresh=force_refresh)
    if usage is None:
        return (
            "Unable to fetch Antigravity subscription quota. "
            "Ensure the agy CLI is installed, authenticated, and accessible."
        )
    return render_usage_text(usage)


def setup_agy_usage_cli(subparser: Any) -> None:
    """Setup function for CLI subcommand `hermes agy-usage`."""
    subparser.add_argument(
        "--refresh",
        "-r",
        action="store_true",
        help="Bypass cache and query agy directly for fresh quota data",
    )
    subparser.add_argument(
        "--json",
        action="store_true",
        help="Output raw quota data as JSON",
    )


def handle_agy_usage_cli(args: Any) -> int:
    """Handler function for CLI subcommand `hermes agy-usage`."""
    force_refresh = getattr(args, "refresh", False)
    usage = fetch_subscription_usage(force_refresh=force_refresh)
    if usage is None:
        print(
            "Unable to fetch Antigravity subscription quota. "
            "Ensure the agy CLI is installed, authenticated, and accessible.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "json", False):
        print(json.dumps(usage.raw, indent=2))
    else:
        print(render_usage_text(usage))
    return 0
