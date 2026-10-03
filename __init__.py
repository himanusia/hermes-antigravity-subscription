"""Antigravity Subscription DirectSDK provider plugin for Hermes Agent."""

from __future__ import annotations

import logging
import os
import re
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
        get_accounts_dir,
        get_active,
        list_accounts,
        remove_account,
        set_active,
        set_cooldown,
    )
    from .process import resolve_agy_command
except ImportError:
    from accounts import (
        DEFAULT_ACCOUNTS_DIR,
        add_account,
        fetch_usage_for_home,
        get_accounts_dir,
        get_active,
        list_accounts,
        remove_account,
        set_active,
        set_cooldown,
    )
    from process import resolve_agy_command

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

_FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.1-pro",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)


class AntigravitySubscriptionDirectSDKProfile(ProviderProfile):
    """Google Antigravity Subscription DirectSDK provider profile."""

    def create_client(self, **client_kwargs: Any) -> Any:
        """Create the Antigravity client facade."""
        from .client import AntigravityClient

        return AntigravityClient(**client_kwargs)

    def supported_reasoning_efforts(
        self, model: str | None
    ) -> tuple[str, ...] | None:
        """Declared reasoning-effort vocabulary for models on this provider.
        
        Enables Hermes /model picker and /reasoning commands to offer appropriate
        thinking effort options (low, medium, high) for reasoning-capable models.
        """
        m = (model or "").lower()
        if "gemini-3.1-pro" in m:
            return ("low", "high")
        if "gemini" in m or "flash" in m or "pro" in m:
            return ("low", "medium", "high")
        if "claude" in m:
            # agy rejects --effort for Claude models. Offering effort levels
            # here makes Hermes send the flag; the worker dies with a
            # BrokenPipeError and Hermes silently falls back to OpenRouter.
            # (Issue #15)
            return ()
        if "gpt" in m:
            return ()
        return ("low", "medium", "high")

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract reasoning effort and forward as top-level api_kwargs to AntigravityClient."""
        effort = None
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                effort = "low"
            else:
                effort = reasoning_config.get("effort")
        top_level: dict[str, Any] = {}
        if effort:
            top_level["reasoning_effort"] = str(effort).strip().lower()
        return {}, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> list[str] | None:
        """Query `agy models` and normalize to clean, deduplicated base models.
        
        Separates model families from thinking efforts (e.g. `gemini-3.8-flash-{low,medium,high}`
        becomes `gemini-3.8-flash`), allowing Hermes' native reasoning effort picker to handle
        the thinking depth cleanly.
        """
        try:
            from .client import resolve_agy_command
        except ImportError:
            # Loaded outside a package (e.g. a flat source tree under test):
            # the absolute name is the same module.
            from client import resolve_agy_command

        cmd = resolve_agy_command()
        try:
            res = subprocess.run(
                [cmd, "models"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            raw_models: list[str] = []
            for raw_line in res.stdout.strip().splitlines():
                line = raw_line.strip()
                if not line or "fetching" in line.lower():
                    continue
                parts = line.split()
                if parts:
                    model_id = parts[0]
                    if any(c in model_id.lower() for c in ("gemini", "claude", "gpt", "model")):
                        raw_models.append(model_id)

            if raw_models:
                clean_models: list[str] = []
                seen: set[str] = set()
                for m in raw_models:
                    base = m
                    for suffix in ("-high", "-medium", "-low"):
                        if m.endswith(suffix) and (m.startswith("gemini-") or "flash" in m or "pro" in m):
                            base = m[:-len(suffix)]
                            break
                    if base not in seen:
                        seen.add(base)
                        clean_models.append(base)
                return clean_models
        except Exception as exc:
            logger.debug("Antigravity fetch_models failed: %s", exc)

        return list(_FALLBACK_MODELS)

    def get_model_context_length(self, model: str) -> int | None:
        """Declared context window for Antigravity CLI.
        
        Defaults to 200,000 tokens (allowing conversations to comfortably pass
        140k-160k tokens before auto-compression). Configurable via the
        ANTIGRAVITY_CONTEXT_LENGTH environment variable.
        """
        env_val = os.environ.get("ANTIGRAVITY_CONTEXT_LENGTH")
        if env_val:
            try:
                val = int(env_val.strip())
                if val > 0:
                    return val
            except ValueError:
                pass
        return 200_000

    def classify_api_error(
        self,
        error: Exception,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        message: str = "",
        body: Any = None,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        return _classify_antigravity_error(
            error,
            status_code=status_code,
            error_code=error_code,
            message=message,
            body=body,
            model=model,
        )


def _classify_antigravity_error(
    error: Exception,
    *,
    status_code: int | None = None,
    error_code: str | None = None,
    message: str = "",
    body: Any = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Classify agy CLI specific runtime errors so Hermes' smart failover /
    recovery pipeline triggers auto-compression and retry instead of failing.
    """
    err_str = f"{error} {message}".lower()
    if any(
        pattern in err_str
        for pattern in (
            "subscriber fell behind updates",
            "stalled for 5s",
            "empty result (status='success')",
            "empty result (status=\"success\")",
            "context canceled",
            "max_trajectory_tokens",
            "max trajectory tokens",
        )
    ):
        return {
            "reason": "context_overflow",
            "retryable": True,
            "should_compress": True,
        }
    return None


def antigravity_auth_handler(action: str, args: Any) -> bool:
    """Provider-owned auth handler for `hermes auth add|status|logout|refresh`.

    Called by Hermes Agent when managing credentials for Antigravity.
    Returns True when the action was handled by this plugin, False to fall through.
    """
    if action == "add":
        if not sys.stdin.isatty():
            raise SystemExit(
                "Error: 'hermes auth add antigravity-subscription-directsdk' requires an interactive terminal (TTY) "
                "so you can complete the browser-based Google authentication."
            )

        label = (getattr(args, "label", None) or "").strip()
        if not label:
            try:
                label = input("Account label: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit(1)
        if not label:
            raise SystemExit("Error: --label is required and cannot be empty.")

        accounts_dir = get_accounts_dir()
        home_dir = accounts_dir / label
        home_dir.mkdir(parents=True, exist_ok=True)
        try:
            home_dir.chmod(0o700)
        except OSError:
            pass

        # Additional accounts stay fully isolated: no host keychain link. On macOS agy
        # keeps its session in $HOME/Library/Keychains (service "gemini", account
        # "antigravity"), so linking the host keychain here would let the new account
        # reuse or overwrite the existing account credential.

        try:
            cmd = resolve_agy_command()
        except Exception as exc:
            raise SystemExit(f"Error resolving agy CLI: {exc}")

        env = os.environ.copy()
        env["HOME"] = str(home_dir)
        env.pop("ANTIGRAVITY_CONFIG_DIR", None)

        print(f"Opening agy in {home_dir} for account '{label}'...")
        print("Please complete the sign-in prompt in your browser if requested.")
        res = subprocess.run([cmd], env=env, check=False)
        if res.returncode != 0:
            raise SystemExit(f"agy exited with code {res.returncode}.")

        add_account(label=label, home_dir=str(home_dir), enabled=True)
        print(f"Successfully saved account '{label}' to registry.")
        return True

    if action == "status":
        accounts = list_accounts()
        active = get_active()
        if not accounts:
            print(
                "No Antigravity accounts registered.\n"
                "Run `hermes auth add antigravity-subscription-directsdk --label <name>` to add an account."
            )
            return True

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
        return True

    if action in ("logout", "remove"):
        target = (getattr(args, "target", None) or getattr(args, "label", None) or "").strip()
        if target:
            if remove_account(target):
                print(f"Account '{target}' removed from registry. Home directory preserved.")
                return True
            else:
                raise SystemExit(f"Error: Account '{target}' not found in registry.")

        accounts = list_accounts()
        if not accounts:
            print("No Antigravity accounts registered.")
            return True

        if len(accounts) == 1:
            lbl = accounts[0]["label"]
            remove_account(lbl)
            print(f"Account '{lbl}' removed from registry. Home directory preserved.")
            return True

        if sys.stdin.isatty():
            print("Registered Antigravity accounts:")
            for i, acc in enumerate(accounts, 1):
                print(f"  {i}. {acc['label']}")
            try:
                choice = input("Enter label to remove (or 'all' to remove all): ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit(1)
            if choice.lower() == "all":
                for acc in accounts:
                    remove_account(acc["label"])
                print(f"All {len(accounts)} accounts removed from registry. Home directories preserved.")
                return True
            elif choice:
                if remove_account(choice):
                    print(f"Account '{choice}' removed from registry. Home directory preserved.")
                    return True
                else:
                    raise SystemExit(f"Error: Account '{choice}' not found in registry.")
            else:
                raise SystemExit("Error: No account selected.")

        for acc in accounts:
            remove_account(acc["label"])
        print(f"Logged out of Antigravity ({len(accounts)} account(s) removed from registry). Home directories preserved.")
        return True

    if action == "refresh":
        target = (getattr(args, "target", None) or getattr(args, "label", None) or "").strip()
        if target:
            accounts = {acc["label"]: acc for acc in list_accounts()}
            if target not in accounts:
                raise SystemExit(f"Error: Account '{target}' not found in registry.")
            set_cooldown(target, until=0.0)
            fetch_usage_for_home(accounts[target].get("home_dir", ""), cached=False)
            print(f"Cleared cooldown and refreshed quota for '{target}'.")
            return True

        accounts = list_accounts()
        if not accounts:
            print("No Antigravity accounts registered.")
            return True
        for acc in accounts:
            lbl = acc["label"]
            set_cooldown(lbl, until=0.0)
            fetch_usage_for_home(acc.get("home_dir", ""), cached=False)
        print(f"Cleared cooldowns and refreshed quota for {len(accounts)} account(s).")
        return True

    if action == "use":
        target = (getattr(args, "target", None) or getattr(args, "label", None) or "").strip()
        if not target:
            raise SystemExit("Error: target/label is required.")
        if set_active(target):
            print(f"Active account set to '{target}'.")
            return True
        else:
            raise SystemExit(f"Error: Account '{target}' not found in registry.")

    return False


antigravity_profile = AntigravitySubscriptionDirectSDKProfile(
    name="antigravity-subscription-directsdk",
    aliases=("antigravity", "agy", "antigravity-directsdk"),
    display_name="Antigravity Subscription DirectSDK",
    description="Use your Antigravity / Gemini subscription via the official agy CLI",
    base_url="agy://local",
    api_mode="chat_completions",
    auth_type="external_process",
    process_command="agy",
    process_args=("--output-format", "stream-json", "--disable-slash-commands"),
    process_command_env_vars=("ANTIGRAVITY_COMMAND", "AGY_CLI_PATH", "ANTIGRAVITY_CLI_PATH"),
    process_args_env_var="ANTIGRAVITY_ARGS",
    default_aux_model="gemini-3.8-flash",
    fallback_models=_FALLBACK_MODELS,
    supports_vision=True,
    classify_api_error=_classify_antigravity_error,
    auth_handler=antigravity_auth_handler,
)

auth_handler = antigravity_auth_handler
register_provider(antigravity_profile)
