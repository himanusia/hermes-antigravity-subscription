# Antigravity Companion (Hermes CLI plugin)

Adds `hermes antigravity ...` for the accounts managed by the
`antigravity-subscription-directsdk` provider plugin.

Why a second plugin: the provider is `kind: model-provider`, and Hermes never
calls `register(ctx)` on that kind (`hermes_cli/plugins_discovery.py` skips it;
`hermes_cli/plugin_validate.py` treats such an entry point as dead code), so it
cannot register a CLI command. A normal plugin can.

## Install

`hermes plugins install <repo-url>` installs the provider (the repo root is the
model-provider plugin). The companion is a subdirectory, so copy it in:

```sh
git clone --depth 1 https://github.com/himanusia/hermes-antigravity-subscription /tmp/agy-plugin
cp -r /tmp/agy-plugin/companion ~/.hermes/plugins/antigravity-companion
hermes plugins enable antigravity-companion
```

## Commands

```sh
hermes antigravity list              # accounts, eligibility, quota, home directory
hermes antigravity list --fast       # no quota probes (no agy calls)
hermes antigravity run <account>     # open agy interactively as that account
hermes antigravity run <account> -p "hello"   # one-shot; arguments pass through to agy
hermes antigravity usage [account]   # remaining quota for one account (default: host)
```

`host` is the original HOME account. Other accounts are read from
`~/.agy-accounts/<label>/` plus the registry at
`~/.hermes/antigravity-accounts.json` (override with `ANTIGRAVITY_ACCOUNTS_DIR`
and `ANTIGRAVITY_ACCOUNTS_FILE`).

## Behaviour

* Quota comes from `HOME=<account> agy -p "/usage" --output-format json`, which
  reports `num_turns: 0` and consumes no subscription quota.
* Accounts that are signed in but not eligible for Antigravity are marked `NO`
  and reported with a `WARNING`. Eligible accounts print no warning.
* Tokens are never printed. The account email is read from the `id_token` JWT
  payload inside the local token file.
* On macOS a keychain dialog may appear while `agy` stores its session; it can be
  cancelled, the credential is written to the account's isolated token file.

## Tests

```sh
PYTHONPATH=/usr/local/lib/hermes-agent:. python -m pytest companion/tests -q
```
