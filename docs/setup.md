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

The example model selection is `gpt-6.1-sol`/`high` for both analysis and every calendar tool call, including access checks, reads, reconciliation and writes. It reflects this code's defaults, not a promise of availability. Select models and efforts your runtime actually supports; this repository cannot create or grant a connector connection. Explicit private `tool_model`/`tool_effort` overrides still take precedence, so update older private configuration deliberately when adopting the new default.

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

The probe is a **real calendar read** and checks access to the configured calendar. Dry-run uses the last complete stored Librus collection and a fresh Calendar read; supplied text goes to the configured planning service. It does not refresh Librus sources. Inspect the local pending proposals and review queue before resuming writes. An automatic update or cancellation of an event governed by a prior human approval, edit or rejection requires fresh approval of that change, including after reconstruction. Cancellation still requires explicit confirmation. New state starts with writes paused. After checking your account, source messages, dates, model and connector behavior:

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

Replace `FULL_SHA` with the exact reviewed 40-character SHA at clean checkout HEAD, reachable from a freshly fetched local `origin/main`. Preparation checks source and reachable-history privacy, runs offline tests in an isolated temporary home, builds/installs a wheel, and verifies installed imports/resources outside source. It never stops services, reads production state, makes a backup or changes the calendar. Release manifests record checked SHA, artifact/application hashes and the wheel's resource inventory. Validation binds installed application files and resources to that release's hash-verified wheel; older manifests recover the same contract from their recorded wheel, so additions in a later checkout do not invalidate a prior release. Preparation, including reuse, and target activation also require all resources declared by the invoking source checkout. Preparation must finish before a production outage. Re-running it reuses a verified existing release.

Private production files remain under `$HOME/.config/librus-calendar` and `$HOME/.local/share/librus-calendar`. Configure actual `config.json`, `credentials.json`, `web.env` and `monitor.env` before activation. The installer preserves existing values and refuses symlinks, unsafe paths and unrelated units before writing. `bash scripts/install_linux.sh --sha FULL_SHA` installs packaged units only and does not start them.

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA
.venv/bin/python scripts/deploy.py verify
```

Activation first stops the sync timer and refuses to switch while a sync worker is running; it never terminates a calendar writer. Retry after the worker completes. Then it installs known units, switches `current`, and enables web, hourly sync and minute monitoring. The sync timer uses Europe/Warsaw, is persistent and limits each worker to 45 minutes. Every deployment validates the prepared package before stopping services.

For an explicitly planned migration from the exact known legacy units, preserve and validate the existing private state first, then run:

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

Five optional nullable fields extend the existing version-1 aggregate schema: `calendar_probe_result`, `calendar_probe_attempts`, `calendar_probe_recovered`, `calendar_probe_retries_24h` and `calendar_probe_recoveries_24h`. Older exporters/collectors can ignore these additions, and the updated collector accepts older snapshots. Missing or null evidence is **no data**, not success or zero retries. The new `librus_local.calendar_probe` chart shows absolute rolling 24-hour retry-start and successful-recovery counts; it is informational and adds no alarm rules. A successful second probe attempt does not increment sync failure state. Update the installed collector and restart its worker to see the new chart.

## Calendar probe and safe diagnostics

Before ordinary synchronization proceeds, a real read-only calendar-list probe verifies owner/writer access to the exact configured calendar. It makes at most two attempts, with a five-second delay before the second. A retry is permitted only for the structured unexpected completed tool-call-count error (zero or multiple) when all observed calls are the expected `list_calendars` read with the expected arguments. Unfinished calls, unexpected calls or arguments, mutations, malformed protocol/results, timeout, permissions failures and missing/non-writable calendars do not qualify. The second attempt has fresh per-call runtime state.

The panel reports the latest sync run's probe result (`running`, `ok`, `failed` or `interrupted`), attempts and recovery. A run with no recorded probe, or a legacy database without evidence, shows “Brak danych”. A successful retry reports access confirmed, two attempts and recovery, without a warning banner. A verified calendar read establishes access at that moment; inspect the final sync result and durable operation state separately.

Probe audit rows contain technical metadata only: sync run/attempt identity, timestamps, configured model/effort, validated CLI version, result, process/error codes, tool-call/protocol counts and expected-call validation flags. They contain no calendar IDs, tool arguments/results, messages, prompts, tokens or raw errors, and expire after 90 days. Monitoring exports only the five aggregate fields, never those rows. Check private installed CLI status, the panel, service state and sanitized aggregates first. Share only technical result/error categories and counts; do not dump the database or Codex transcripts into an issue. A standalone setup probe performs a calendar read but does not create sync-run audit evidence.

The retry policy covers this initial read probe only. It does not directly retry create/update/delete calls. When a write outcome is uncertain, retain the journal and use read-only reconciliation before any later decision to execute work.

## Single-agenda operation

Normal `sync` collects all required source kinds and reads current owned Calendar state before planning. Collection failure is not an empty successful source list. The selected learner/class must stay verified. The configured planner is `gpt-6.1-sol/high`; no per-source extractor/matcher/verifier chain or activation receipt is used.

```sh
release_python="$HOME/.local/lib/librus-calendar/current/.venv/bin/python"
"$release_python" -I -m app.cli status
"$release_python" -I -m app.cli agenda-preview
```

`agenda-preview` and `sync --dry-run` use the last complete successful stored collection and fresh read-only Calendar state. They do not log into Librus or refresh sources. If no complete collection exists they fail visibly. They save only an isolated preview, not active decisions, groups, operations or Calendar mutations. Inspect `/preview` privately; it records when sources were collected. A normal sync can reuse an exact unchanged preview only after checking current source, agenda, Calendar and decision state. Normal CLI output contains aggregates, not plan text.

The private web API keeps `/api/messages`, `/api/proposals`, `/api/proposal-groups` and paginated `/api/sources`. `/api/actions/{id}` shows a shared action and all supporting sources. `/api/agenda` returns separate latest active-plan/preview metadata and the stored whole preview. `/api/source-previews` remains read-only historical data labeled `legacy-source-planner`; it is not current engine readiness. No preview inference or activation endpoint runs in the web process.

Upgrade in place with the existing database path. Stop new hourly runs, let any writer finish and reconcile unknown outcomes. Prepare the reviewed package, then use canonical `activate` and `verify`; preserve configuration, write pause and journals. Initialization idempotently links existing events/proposals/actions without resetting state or creating Calendar events. Do not create a fresh replacement DB, copy/restore SQLite, replay all historical messages or run the retired per-source activation/reanalysis commands. Resume the same hourly app timer after verifying retained IDs/decisions, no duplicates, no unknown operations and healthy source collection. No additional service or Codex acceptance automation is needed.

## Retention and recovery

The ordinary sync and `prune --days 30` retain minimal IDs, hashes, human decisions, ownership, future facts, cancellation tombstones and every durable journal. `prune` previews by default; `--apply` clears expired narrative source/plan/audit content in place. Full unknown/inflight operation intent survives. Technical sync/probe history has its separate 90-day window. Pruning never changes Calendar and creates no backup.

Prefer a reviewed forward fix after failure; keep the current database and operation journal. A prior code package is usable only after explicit schema/journal compatibility checks. Never restore an older SQLite database: it can lose proof of external writes and cause duplicates. Review visible `agenda_*` failure codes and pending/failed sources; context overflow and malformed/stale plans refuse writes rather than silently dropping input. The read-only monitor exports fixed technical plan outcomes and source counts only; existing charts and alarm thresholds remain.

For local development use `scripts/ui_fixture.py --port 8796` with temporary fictional data. Real school data, configuration, snapshots and plan output stay outside Git and public screenshots.
