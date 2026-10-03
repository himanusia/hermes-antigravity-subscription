"""CLI surface for the Antigravity companion plugin: `hermes antigravity ...`.

The provider plugin (`antigravity-subscription-directsdk`) is a
``kind: model-provider`` plugin, and Hermes never calls ``register(ctx)`` on that
kind, so it cannot offer a CLI command. This companion plugin is a normal
standalone plugin, so it can. It only reads the same on-disk contract.
"""

from __future__ import annotations

import argparse
import sys

try:
    from . import accounts
except ImportError:  # loaded as a top-level module
    import accounts  # type: ignore[no-redef]


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Wire `hermes antigravity <action>` into the Hermes CLI."""
    subs = subparser.add_subparsers(dest="antigravity_action")

    p_list = subs.add_parser("list", help="List Antigravity accounts with eligibility and quota")
    p_list.add_argument("--fast", action="store_true", help="Skip quota probes (no agy calls)")

    p_run = subs.add_parser("run", help="Run the agy CLI as a stored account (interactive by default)")
    p_run.add_argument("label", help="Account label, or 'host' for the original HOME")
    p_run.add_argument("agy_args", nargs=argparse.REMAINDER, help="Arguments passed through to agy")

    p_usage = subs.add_parser("usage", help="Show remaining quota for one account")
    p_usage.add_argument("label", nargs="?", default=accounts.HOST_LABEL, help="Account label (default: host)")

    subparser.set_defaults(func=antigravity_command)


def antigravity_command(args: argparse.Namespace) -> int:
    action = getattr(args, "antigravity_action", None)
    if not action:
        print("Usage: hermes antigravity {list|run|usage}")
        return 2
    if action == "list":
        return _cmd_list(fast=bool(getattr(args, "fast", False)))
    if action == "run":
        return _cmd_run(str(getattr(args, "label", "")), list(getattr(args, "agy_args", []) or []))
    if action == "usage":
        return _cmd_usage(str(getattr(args, "label", accounts.HOST_LABEL) or accounts.HOST_LABEL))
    print(f"Unknown antigravity action: {action}")
    return 2


def _cmd_list(fast: bool) -> int:
    rows: list[list[str]] = [["ACCOUNT", "EMAIL", "ELIGIBLE", "GEMINI 5h/wk", "CLAUDE+GPT 5h/wk", "HOME"]]
    ineligible: list[str] = []

    for account in accounts.list_accounts():
        if fast:
            eligible_text = "-"
            if account.get("eligible") is False:
                eligible_text = "NO"
                ineligible.append(account["label"])
            gemini = claude = "-"
        else:
            eligible, windows, note = accounts.probe(account["home"])
            if eligible is True:
                eligible_text = "yes"
            elif eligible is False:
                eligible_text = "NO"
                ineligible.append(account["label"])
            else:
                eligible_text = f"? ({note})" if note else "?"
            gemini = accounts.window_pair(windows, "gemini")
            claude = accounts.window_pair(windows, "claude_gpt")
        rows.append(
            [
                account["label"],
                account["email"] or "-",
                eligible_text,
                gemini,
                claude,
                str(account["home"]),
            ]
        )

    _print_table(rows)

    # Warn only about accounts that cannot be used; eligible accounts stay quiet.
    if ineligible:
        print(f"\nWARNING: not eligible for Antigravity (skipped by quota rotation): {', '.join(ineligible)}")

    print("\nRun an account:  hermes antigravity run <account>")
    print("One-shot:        hermes antigravity run <account> -p \"...\"")
    print("Quota:           hermes antigravity usage [account]")
    return 0


def _cmd_run(label: str, agy_args: list[str]) -> int:
    account = accounts.resolve_account(label)
    if account is None:
        print(f"Unknown account '{label}'. Run `hermes antigravity list` to see accounts.")
        return 2
    if agy_args and agy_args[0] == "--":
        agy_args = agy_args[1:]
    return accounts.run_agy(account["home"], agy_args)


def _cmd_usage(label: str) -> int:
    account = accounts.resolve_account(label)
    if account is None:
        print(f"Unknown account '{label}'. Run `hermes antigravity list` to see accounts.")
        return 2

    eligible, windows, note = accounts.probe(account["home"])
    print(f"Account:  {account['label']}")
    if account["email"]:
        print(f"Email:    {account['email']}")
    print(f"Home:     {account['home']}")

    if eligible is False:
        print("Eligible: NO")
        print(f"\nWARNING: not eligible for Antigravity: {note or 'subscription check failed'}")
        return 1
    if eligible is None:
        print(f"Eligible: unknown ({note or 'could not determine'})")
        return 1

    print("Eligible: yes")
    print(f"  Gemini     5h {accounts.pct(windows.get('gemini_5h'))}   weekly {accounts.pct(windows.get('gemini_weekly'))}")
    print(f"  Claude/GPT 5h {accounts.pct(windows.get('claude_gpt_5h'))}   weekly {accounts.pct(windows.get('claude_gpt_weekly'))}")
    return 0


def _print_table(rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))


def main(argv: list[str] | None = None) -> int:
    """Direct entry point, useful for local testing without the Hermes CLI."""
    parser = argparse.ArgumentParser(prog="hermes antigravity")
    register_cli(parser)
    return antigravity_command(parser.parse_args(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
