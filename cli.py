"""CLI commands for Antigravity multi-account management."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .accounts import (
        DEFAULT_ACCOUNTS_DIR,
        add_account,
        fetch_usage_for_home,
        get_active,
        list_accounts,
        remove_account,
        set_active,
    )
    from .process import _link_macos_keychains, resolve_agy_command
except ImportError:
    from accounts import (
        DEFAULT_ACCOUNTS_DIR,
        add_account,
        fetch_usage_for_home,
        get_active,
        list_accounts,
        remove_account,
        set_active,
    )
    from process import _link_macos_keychains, resolve_agy_command


def cmd_login(args: argparse.Namespace) -> int:
    """Interactive login for an Antigravity account using agy."""
    label = (args.label or "").strip()
    if not label:
        print("Error: --label is required and cannot be empty.", file=sys.stderr)
        return 1

    if not sys.stdin.isatty():
        print(
            "Error: 'hermes antigravity login' requires an interactive terminal (TTY) "
            "so you can complete the browser-based Google authentication.",
            file=sys.stderr,
        )
        return 1

    home_dir = DEFAULT_ACCOUNTS_DIR / label
    home_dir.mkdir(parents=True, exist_ok=True)
    try:
        home_dir.chmod(0o700)
    except OSError:
        pass

    if sys.platform == "darwin":
        _link_macos_keychains(home_dir)

    try:
        cmd = resolve_agy_command()
    except Exception as exc:
        print(f"Error resolving agy CLI: {exc}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env.pop("ANTIGRAVITY_CONFIG_DIR", None)

    print(f"Opening agy in {home_dir} for account '{label}'...")
    print("Please complete the sign-in prompt in your browser if requested.")
    res = subprocess.run([cmd], env=env, check=False)
    if res.returncode != 0:
        print(f"agy exited with code {res.returncode}.", file=sys.stderr)
        return res.returncode

    add_account(label=label, home_dir=str(home_dir), enabled=True)
    print(f"Successfully saved account '{label}' to registry.")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    """Display table of registered accounts and their quota status."""
    accounts = list_accounts()
    active = get_active()
    if not accounts:
        print(
            "No Antigravity accounts registered.\n"
            "Run 'hermes antigravity login --label <name>' to register an account."
        )
        return 0

    headers = ["Label", "Active", "Enabled", "Cooldown", "Gemini (5h / Wk)", "Claude/GPT (5h / Wk)"]
    row_format = "{:<16} {:<8} {:<9} {:<12} {:<20} {:<20}"
    print(row_format.format(*headers))
    print("-" * 85)

    now = time.time()
    for acc in accounts:
        lbl = acc.get("label", "")
        is_act = "*" if lbl == active else ""
        en = "yes" if acc.get("enabled", True) else "no"
        cd_until = acc.get("cooldown_until", 0.0)
        if cd_until > now:
            cd_str = f"{int(cd_until - now)}s"
        else:
            cd_str = "ready"

        usage = fetch_usage_for_home(acc.get("home_dir", ""), cached=False)
        if usage:
            g_5h = usage.get("gemini", {}).get("5h", {}).get("remaining_fraction")
            g_wk = usage.get("gemini", {}).get("weekly", {}).get("remaining_fraction")
            c_5h = usage.get("claude_gpt", {}).get("5h", {}).get("remaining_fraction")
            c_wk = usage.get("claude_gpt", {}).get("weekly", {}).get("remaining_fraction")

            gemini_str = (
                f"{int(g_5h * 100)}% / {int(g_wk * 100)}%"
                if g_5h is not None and g_wk is not None
                else "n/a"
            )
            claude_str = (
                f"{int(c_5h * 100)}% / {int(c_wk * 100)}%"
                if c_5h is not None and c_wk is not None
                else "n/a"
            )
        else:
            gemini_str = "unknown"
            claude_str = "unknown"

        print(row_format.format(lbl, is_act, en, cd_str, gemini_str, claude_str))
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    """Set the active account."""
    label = (args.label or "").strip()
    if not label:
        print("Error: label is required.", file=sys.stderr)
        return 1

    if set_active(label):
        print(f"Active account set to '{label}'.")
        return 0
    else:
        print(f"Error: Account '{label}' not found in registry.", file=sys.stderr)
        return 1


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove an account from the registry (preserves files on disk)."""
    label = (args.label or "").strip()
    if not label:
        print("Error: label is required.", file=sys.stderr)
        return 1

    if remove_account(label):
        print(f"Account '{label}' removed from registry. Home directory preserved.")
        return 0
    else:
        print(f"Error: Account '{label}' not found in registry.", file=sys.stderr)
        return 1


def setup_cli(parser: argparse.ArgumentParser) -> None:
    """Register subcommands under 'hermes antigravity'."""
    subparsers = parser.add_subparsers(dest="subcommand")

    # hermes antigravity login --label NAME
    p_login = subparsers.add_parser("login", help="Log in to a new Antigravity account")
    p_login.add_argument("--label", required=True, help="Unique name/label for the account")
    p_login.set_defaults(func=cmd_login)

    # hermes antigravity accounts
    p_accounts = subparsers.add_parser("accounts", help="List registered accounts and quota")
    p_accounts.set_defaults(func=cmd_accounts)

    # hermes antigravity use LABEL
    p_use = subparsers.add_parser("use", help="Set active account")
    p_use.add_argument("label", help="Account label")
    p_use.set_defaults(func=cmd_use)

    # hermes antigravity remove LABEL
    p_remove = subparsers.add_parser("remove", help="Remove an account from registry")
    p_remove.add_argument("label", help="Account label to remove")
    p_remove.set_defaults(func=cmd_remove)

    # Default handler if no subcommand given
    parser.set_defaults(func=lambda args: parser.print_help())
