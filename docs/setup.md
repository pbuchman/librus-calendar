# Configure your own instance

[Polski](setup.pl.md) · [README](../README.md)

The [offline demo](demo.md) is the fastest way to see the interface. The steps below enable real account access and are not executed by the demo.

## Prerequisites

- Python 3.12+, an isolated virtual environment, and the pinned packages in `requirements.txt`.
- A Librus Synergia account that can read the required messages.
- An authenticated Codex CLI on the execution host, supporting the flags used in `app/codex_runtime.py`. The runtime invokes `codex exec` with isolated prompts and tightly selected tools; not every CLI release or account supports this combination.
- A Google Calendar connection available to that same Codex account and host, exposing the supported list/search/read/create/update/delete tools. The exact connector identifier must be configured for your installation.
- Writer or owner access to the exact calendar you choose. `calendar_owner` is the verified primary email of the authenticated calendar owner, distinct from a secondary calendar ID.
- For unattended deployment: Linux with systemd user services, Tailscale Serve with trusted identity headers, and optionally Netdata's Python collector.

The example model selections are `gpt-6.1-sol`/`high` for analysis and `gpt-6-luna`/`medium` for calendar tools. They reflect this code's defaults, not a promise of availability. Select models and efforts your runtime actually supports; this repository cannot create or grant a connector connection.

## Private configuration

Use `deploy/config.json.example` as a template for `~/.config/librus-calendar/config.json`. Every supplied JSON file must be an owned regular file with mode **0600**, inside a private directory. Keep configuration and credentials outside this repository. Replace all synthetic values before real synchronization.

| Field | Meaning |
| --- | --- |
| `calendar_id` | Exact Google Calendar ID, including a secondary calendar ID if used |
| `calendar_owner` | Verified authenticated owner's primary email; used for attendee safety checks |
| `calendar_connector` | Your connected app's `connector_...` identifier |
| `tailscale_owner` | Only allowed Tailscale login identity |
| `allowed_hosts` | Exact Tailscale hostname and port; no wildcard |
| `allow_local` | Keep `false` for the private production UI |
| `backup_enabled` | Keep `false`; automatic database backups are intentionally disabled |
| `state_path`, `credentials_path` | Private SQLite/credentials locations; default under your home directory |
| `codex_binary` | Executable path or `codex` on the service's PATH |
| `analysis_model`, `analysis_effort` | Analysis model and a supported reasoning effort |
| `tool_model`, `tool_effort` | Calendar tool model and a supported reasoning effort |
| `timeout` | Individual runtime timeout in seconds, from 1 to 300 |
| `monitor_url`, `monitor_output` | Optional localhost status endpoint and aggregate health output path |

`LIBRUS_CONFIG_FILE` selects a different JSON file; an explicitly selected missing file fails. An empty `LIBRUS_CONFIG_FILE` disables JSON discovery. `LIBRUS_<FIELD_IN_UPPERCASE>` overrides a field; for example `LIBRUS_CALENDAR_ID`, `LIBRUS_CODEX_BINARY` and `LIBRUS_ANALYSIS_MODEL`. `LIBRUS_ALLOWED_HOSTS` is comma-separated and `LIBRUS_ALLOW_LOCAL` is `0` or `1`. Unsupported JSON keys and malformed settings fail validation. There are no personal account defaults.

Create the credentials file in a protected editor, with this JSON shape:

```json
{"login": "YOUR_LIBRUS_LOGIN", "password": "<YOUR_LIBRUS_PASSWORD>"}
```

Use the private `credentials_path` and mode 0600. Never put a password in a shell argument, command history, repository or screenshot. `scripts/import_credentials.py` is an optional migration utility for an existing owned temporary `login password` file: it verifies the protected target before deleting the source. It does not log credentials.

## Validate before enabling writes

After preparing the reviewed package as described below, select its installed interpreter. Use `-I` for every installed runtime command so the current directory, inherited `PYTHONPATH` and user site cannot override release code:

```sh
release_python="$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv/bin/python"
"$release_python" -I -m app.cli status
"$release_python" -I -c 'from app.codex_runtime import CodexRuntime; CodexRuntime(read_only=True).probe()'
"$release_python" -I -m app.cli sync --dry-run
```

The probe is a **real calendar read** and checks access to the configured calendar. Dry-run can fetch private Librus messages and send them to the analysis service; it skips writes. It is not offline. Inspect the local pending proposals and review queue before resuming writes. An automatic update or cancellation of an event governed by a prior human approval, edit or rejection requires fresh approval of that change, including after reconstruction. Cancellation still requires explicit confirmation. New state starts with writes paused. After checking your account, source messages, dates, model and connector behavior:

```sh
"$release_python" -I -m app.cli pause-writes --resume
"$release_python" -I -m app.cli sync
```

To stop calendar writes again, run `pause-writes` without `--resume`. This does not stop ingestion or analysis. CLI options include `--state PATH` before the subcommand, `sync --credentials PATH --limit N` (1–100). The private configuration is still used unless explicitly replaced by environment settings.

## Packaged Linux releases and private access

Keep the editable source checkout separate from production. Production imports a wheel installed into `$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv`; `$HOME/.local/lib/librus-calendar/current` selects the active release. Templates, CSS and schemas are included in the wheel. Python 3.12+ is required. Install development dependencies and build tools before preparing:

```sh
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python scripts/deploy.py prepare --sha FULL_SHA
```

Replace `FULL_SHA` with the exact reviewed 40-character SHA at clean checkout HEAD, reachable from a freshly fetched local `origin/main`. Preparation checks source and reachable-history privacy, runs offline tests in an isolated temporary home, builds/installs a wheel, and verifies installed imports/resources outside source. It never stops services, reads production state, makes a backup or changes the calendar. Release manifests record checked SHA and artifact/application hashes. Preparation must finish before a production outage. Re-running it reuses a verified existing release.

Private production files remain under `$HOME/.config/librus-calendar` and `$HOME/.local/share/librus-calendar`. Configure actual `config.json`, `credentials.json`, `web.env` and `monitor.env` before activation. The installer preserves existing values and refuses symlinks, unsafe paths and unrelated units before writing. `bash scripts/install_linux.sh --sha FULL_SHA` installs packaged units only and does not start them.

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA
.venv/bin/python scripts/deploy.py verify
```

Activation first stops the sync timer and refuses to switch while a sync worker is running; it never terminates a calendar writer. Retry after the worker completes. Then it installs known units, switches `current`, and enables web, hourly sync and minute monitoring. The sync timer uses Europe/Warsaw, is persistent and limits each worker to 45 minutes. Every deployment validates the prepared package before stopping services.

For an explicitly planned migration from the exact known legacy units, build and validate the new private target state first, then run:

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA --migrate-legacy --no-sync
.venv/bin/python scripts/deploy.py verify --no-sync
```

This starts web/monitor only. Keep writes paused until first-run acceptance, then enable the hourly timer and run normal verification. Only explicit migration replaces known legacy units and disables/removes their backup service/timer; unrelated administrator units are refused. No installer or deployment creates a database copy. Backups are intentionally disabled by `backup_enabled=false` and `LIBRUS_BACKUP_ENABLED=0`.

The web server binds to `127.0.0.1:8795`. `web.env` holds the exact `LIBRUS_TAILSCALE_OWNER`, `LIBRUS_ALLOWED_HOSTS` and `LIBRUS_ALLOW_LOCAL=0`. Preserve an existing private Tailscale Serve route on HTTPS 8445 and any other routes. For a newly configured host the route is:

```sh
tailscale serve --bg --https=8445 --yes http://127.0.0.1:8795
```

Restrict access to the configured owner, never enable Funnel, and use a separate port for development. Service Codex executable/model/connector configuration must match the authenticated runtime available on that host.

## Netdata monitoring

As root, `deploy/install-librus-monitor.sh APP_USER` installs only owned collector/health files after full preflight. `--migrate-legacy` permits only exact known prior file hashes. A root-owned installed-file hash registry permits later managed upgrades and compatible rollback; arbitrary differences are refused. The script does not restart Netdata. After collector changes, restart the Python worker or Netdata through a controlled host maintenance procedure; reloading health rules alone does not reload Python code.

The minute monitor uses nonsensitive `monitor.env`, with `LIBRUS_CONFIG_FILE=` empty and the same identity/host as `web.env`. Its sandbox cannot read private credentials/configuration. Set `LIBRUS_STATE_PATH` to the same private database path as production config; use a literal private absolute path or `~` because systemd environment files do not expand `$HOME`. Set `LIBRUS_BACKUP_ENABLED=0` explicitly.

The exporter reads SQLite aggregates without initializing/migrating the DB and writes `/var/lib/librus-monitor/status.json`. Its closed schema includes `backup_enabled`; disabled backups produce no backup outage/staleness/failure alarms. Sync, queue, web, database and collector freshness alerts remain active. It never exports message bodies or credentials. If maintenance notifications are muted, preserve the prior silence rules, use a bounded Librus-only mute with guaranteed cleanup, and restore the exact previous rules afterward.

## Isolated development and evaluation

The UI fixture uses temporary synthetic state and no accounts. Real-message evaluation requires an explicit private evaluation config and cases file outside Git:

```sh
.venv/bin/python -m scripts.evaluate_analysis --config PRIVATE_EVAL_CONFIG PRIVATE_CASES_JSON
```

Evaluation does not discover production config, opens no state DB, invokes an explicitly read-only runtime, and emits a bounded report without message bodies, quotes, titles or model free text. Real analysis still sends supplied messages to the configured AI service. For other real-data development, select a separate private state path and port, use read-only calendar runtime permissions, and never point developer commands at production state. Offline tests and preparation isolate HOME and configuration; they must not access live Librus, credentials or calendar APIs.

## Diagnose and recover

Check installed package status through `$HOME/.local/lib/librus-calendar/current/.venv/bin/python -I -m app.cli status`, user service status/timers, private logs and the read-only monitoring aggregates. Redact private identifiers before sharing. Full success, attempts, pending queues and unknown operations have different meanings; a completed timer does not prove successful synchronization.

Prefer forward recovery: stop new scheduled runs, let a writer finish, preserve the current DB and operation journal, prepare a reviewed fix SHA, and activate/verify it. A compatible prior packaged release can be selected only after checking it supports the current schema/journal: check out that reviewed previous SHA cleanly and use `activate --sha PREVIOUS_FULL_SHA`. Do not roll SQLite backward or create a safety backup automatically. Database rollback can discard idempotency evidence after external calendar writes. Keep writes paused until unknown outcomes are reconciled.

## Decision migration, reconstruction and retention

Quiesce the old sync timer and let its worker finish before changing state. Use the prepared release Python before switching `current`; the target must be distinct from the old database. Configure the target private state path and keep writes paused:

```sh
release_python="$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv/bin/python"
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" pause-writes
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" migrate-decisions --from "$HOME/.local/share/librus-calendar/state.sqlite3"
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" rebuild --days 30
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" prune --days 30
```

Migration reads the old database directly with read-only SQLite and creates no copy/export. It requires matching bound calendar/owner and Librus account identity. Only for an explicitly reviewed unbound legacy source, append `--allow-legacy-binding`: the CLI first performs a read-only owner-access calendar probe and requires every transferred event's exact calendar to match. Arbitrary unbound sources are refused.

Migration retains minimal human approval/edit/rejection choices, exact event IDs and ownership markers, conflict fingerprints, cancellation tombstones and unresolved operation evidence. It does not copy message bodies or bulk analysis history. Existing chosen event associations are not discarded. Inflight writes become unknown; preserve their original intent for reconciliation.

`rebuild --days 30` scans all pages of recent Librus messages, retries cached fetch/analysis failures, discovers app-owned calendar candidates and reads exact transferred IDs. Its runtime permits calendar reads only; it never queues or executes calendar writes. It updates the target local database and can send message text to analysis. Foreign, missing, duplicate or ambiguous ownership requires review; partial/review results are not complete reconstruction. Days must be 1–30. Before resuming writes, run a paused dry-run and inspect pending proposals and review items. Reconstruction preserves prior human event choices; a later automatic update or cancellation needs fresh approval even when it comes from another source message.

`prune --days 30` previews content retention; append `--apply` only under the intended retention authorization. It clears old message bodies, analysis records, obsolete automatic proposals and unnecessary event/operation content. Minimal IDs, hashes, human decisions, ownership, conflict evidence, cancellation tombstones and every durable journal row remain. Unknown/inflight operation payload is retained in full; expired unexecuted work becomes review. In-place secure deletion and VACUUM remove freed content without making a backup. Preview and apply make zero calendar writes. Keep the old source only until the explicitly defined acceptance/cleanup point; deployment does not remove it automatically. See [the application contract](../app/CONTRACT.md).
