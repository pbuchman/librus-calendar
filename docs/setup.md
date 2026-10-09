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

Run from the repository, with the virtual environment activated or its Python explicitly selected:

```sh
.venv/bin/python -m app.cli status
.venv/bin/python -m scripts.probe_runtime
.venv/bin/python -m app.cli sync --dry-run
```

The probe is a **real calendar read** and checks access to the configured calendar. Dry-run can fetch private Librus messages and send them to the analysis service; it skips writes. It is not offline. Inspect the local proposals and review queue. New state starts with writes paused. After checking your account, source messages, dates, model and connector behavior:

```sh
.venv/bin/python -m app.cli pause-writes --resume
.venv/bin/python -m app.cli sync
```

To stop calendar writes again, run `pause-writes` without `--resume`. This does not stop ingestion or analysis. CLI options include `--state PATH` before the subcommand, `sync --credentials PATH --limit N` (1–100), and `backup --destination PATH`. The private configuration is still used unless explicitly replaced by environment settings.

## Linux services and private web access

The supplied units expect a checkout at **`~/librus-calendar`** and a virtual environment inside it. Run `bash scripts/install_linux.sh` there. It installs the sync/web/backup units and example configuration, refuses different existing units, and **does not start services**. Prepare the actual `config.json`, `credentials.json` and `web.env` in `~/.config/librus-calendar` before proceeding. Put the correct Codex executable path in `codex_binary`; an interactive shell PATH may differ from systemd's.

`web.env` sets the exact `LIBRUS_TAILSCALE_OWNER`, `LIBRUS_ALLOWED_HOSTS` and `LIBRUS_ALLOW_LOCAL=0`. The web server binds only to `127.0.0.1:8795`, ignores proxy address forwarding and trusts only the local Tailscale proxy with the exact owner identity. Never expose this port as a public reverse proxy or enable Tailscale Funnel. Tailscale ACLs and account access remain your responsibility.

After configuring and checking the instance, route Tailscale Serve and enable the units:

```sh
tailscale serve --bg --https=8445 --yes http://127.0.0.1:8795
systemctl --user enable --now librus-web.service librus-sync.timer librus-backup.timer
systemctl --user list-timers
```

The sync timer runs at every full hour in Europe/Warsaw; missed scheduled runs are persistent. The whole worker is limited to 45 minutes. The backup timer runs daily at 03:15 in Europe/Warsaw. Default backups retain the newest seven files; each SQLite copy receives an integrity check. Backups contain private messages and event details. Arrange host security, encrypted storage/offsite backups and systemd user-session persistence as needed.

## Optional Netdata monitoring

`deploy/install-librus-monitor.sh APP_USER` installs the collector and health rules as root; it requires an existing `netdata` group and Python collector directory. It does not restart Netdata or enable the user monitor. Read it before running with administrator rights.

Copy the monitor service/timer to the application's user-unit directory and the `deploy/monitor.env.example` to that user's `~/.config/librus-calendar/monitor.env`, mode 0600. Replace the synthetic Tailscale identity/host with the same values used by the web proxy. Keep `LIBRUS_CONFIG_FILE=` empty in this file: the monitor exports aggregate read-only database health and cannot access private credentials/configuration directories. Enable `librus-monitor.timer`, then reload Netdata through your installation's normal procedure.

The exporter runs every minute and writes aggregate timestamps, queue counts, technical states and local service health to `/var/lib/librus-monitor/status.json`. It does not export message bodies or credentials. Failure/queue-age/backup alarms indicate health signals; they do not establish calendar accuracy or prove live writes succeeded.

## Diagnose problems and restore backups

Check `.venv/bin/python -m app.cli status`, `systemctl --user list-timers`, and `systemctl --user status librus-sync.service librus-web.service`. `journalctl --user -u librus-sync.service -n 50` shows recent worker logs. Treat status and logs as private; redact account identifiers before sharing. Compare last full success with last attempt, pending queues and unknown operations. A paused write state or completed timer invocation alone does not prove synchronization succeeded.

Run `.venv/bin/python -m app.cli backup` to create an integrity-checked private copy, or supply `backup --destination /PRIVATE/NEW-BACKUP.sqlite3`. Existing destination files are never overwritten. Check a selected backup with `sqlite3 /PRIVATE/BACKUP.sqlite3 'PRAGMA integrity_check;'`; the expected result is `ok`. This confirms SQLite integrity, not consistency with the current external calendar.

Before restoring, pause writes and stop the sync timer, worker, web service and backup timer. Preserve the current database **and its operation journal** as a separate checked copy. Restore only while no process can write the database; preserve ownership and mode 0600. Run `pause-writes` on the restored state before restarting services because the backup may contain enabled writes. Compare its event/operation evidence with the current calendar and reconcile unknown outcomes before resuming writes. Blindly rolling SQLite back after external writes can discard idempotency evidence and cause duplicate or conflicting calendar operations; a backup is not a calendar rollback.

## Administrative commands

`reanalyze --message-id ID` previews selected cached messages with the analysis service; `--apply` replaces only untouched eligible proposals. It does not log into Librus or write the calendar. `metadata-refresh --event-id ID` previews branding/timestamp updates; `--apply` can update those existing external events. Both require explicit IDs and preserve the operation journal. Read [the application contract](../app/CONTRACT.md) before maintenance. Unknown outcomes require reconciliation, not blind retries or database rollback.
