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

Replace `FULL_SHA` with the exact reviewed 40-character SHA at clean checkout HEAD, reachable from a freshly fetched local `origin/main`. Preparation checks source and reachable-history privacy, runs offline tests in an isolated temporary home, builds/installs a wheel, and verifies installed imports/resources outside source. It never stops services, reads production state, makes a backup or changes the calendar. Release manifests record checked SHA, artifact/application hashes and the wheel's resource inventory. Validation binds installed application files and resources to that release's hash-verified wheel; older manifests recover the same contract from their recorded wheel, so additions in a later checkout do not invalidate a prior release. Preparation, including reuse, and target activation also require all resources declared by the invoking source checkout. Preparation must finish before a production outage. Re-running it reuses a verified existing release.

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

Five optional nullable fields extend the existing version-1 aggregate schema: `calendar_probe_result`, `calendar_probe_attempts`, `calendar_probe_recovered`, `calendar_probe_retries_24h` and `calendar_probe_recoveries_24h`. Older exporters/collectors can ignore these additions, and the updated collector accepts older snapshots. Missing or null evidence is **no data**, not success or zero retries. The new `librus_local.calendar_probe` chart shows absolute rolling 24-hour retry-start and successful-recovery counts; it is informational and adds no alarm rules. A successful second probe attempt does not increment sync failure state. Update the installed collector and restart its worker to see the new chart.

## Calendar probe and safe diagnostics

Before ordinary synchronization proceeds, a real read-only calendar-list probe verifies owner/writer access to the exact configured calendar. It makes at most two attempts, with a five-second delay before the second. A retry is permitted only for the structured unexpected completed tool-call-count error (zero or multiple) when all observed calls are the expected `list_calendars` read with the expected arguments. Unfinished calls, unexpected calls or arguments, mutations, malformed protocol/results, timeout, permissions failures and missing/non-writable calendars do not qualify. The second attempt has fresh per-call runtime state.

The panel reports the latest sync run's probe result (`running`, `ok`, `failed` or `interrupted`), attempts and recovery. A run with no recorded probe, or a legacy database without evidence, shows “Brak danych”. A successful retry reports access confirmed, two attempts and recovery, without a warning banner. A verified calendar read establishes access at that moment; inspect the final sync result and durable operation state separately.

Probe audit rows contain technical metadata only: sync run/attempt identity, timestamps, configured model/effort, validated CLI version, result, process/error codes, tool-call/protocol counts and expected-call validation flags. They contain no calendar IDs, tool arguments/results, messages, prompts, tokens or raw errors, and expire after 90 days. Monitoring exports only the five aggregate fields, never those rows. Check private installed CLI status, the panel, service state and sanitized aggregates first. Share only technical result/error categories and counts; do not dump the database or Codex transcripts into an issue. A standalone setup probe performs a calendar read but does not create sync-run audit evidence.

The retry policy covers this initial read probe only. It does not directly retry create/update/delete calls. When a write outcome is uncertain, retain the journal and use read-only reconciliation before any later decision to execute work.

## Isolated development and evaluation

The UI fixture uses temporary synthetic state and no accounts. Real-message evaluation requires an explicit private evaluation config and cases file outside Git:

```sh
.venv/bin/python -m scripts.evaluate_analysis --config PRIVATE_EVAL_CONFIG PRIVATE_CASES_JSON
```

Evaluation does not discover production config, opens no state DB, invokes an explicitly read-only runtime, and emits a bounded report without message bodies, quotes, titles or model free text. Real analysis still sends supplied messages to the configured AI service. For other real-data development, select a separate private state path and port, use read-only calendar runtime permissions, and never point developer commands at production state. Offline tests and preparation isolate HOME and configuration; they must not access live Librus, credentials or calendar APIs.

## Diagnose and recover

Check installed package status through `$HOME/.local/lib/librus-calendar/current/.venv/bin/python -I -m app.cli status`, user service status/timers, private logs and the read-only monitoring aggregates. Redact private identifiers before sharing. Full success, attempts, pending queues and unknown operations have different meanings; a completed timer does not prove successful synchronization.

Calendar results require verified evidence of the exact authorized MCP tool and arguments, whether the host uses ordinary MCP or code-mode transport; a model's own success statement is insufficient.

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

`prune --days 30` previews content retention; append `--apply` only under the intended retention authorization. It clears old message bodies, analysis records, obsolete automatic proposals and unnecessary event/operation content. Content-bearing source previews and source-analysis audits follow 30-day content retention; the 90-day history window applies only to technical sync/probe records. Minimal IDs, hashes, human decisions, ownership, conflict evidence, cancellation tombstones and every durable journal row remain. Unknown/inflight operation payload is retained in full; expired unexecuted work becomes review. In-place secure deletion and VACUUM remove freed content without making a backup. Preview and apply make zero calendar writes. Keep the old source only until the explicitly defined acceptance/cleanup point; deployment does not remove it automatically. See [the application contract](../app/CONTRACT.md).

## Multiple-source rollout

`multi_source_mode` accepts `off` (default), `preview`, or `active`; the explicit environment override is `LIBRUS_MULTI_SOURCE_MODE`. Existing message synchronization remains available. Use the same protected local configuration and immutable-package deployment procedure as above; never put a learner identifier, credentials, source text or preview output into a public repository. Preserve the configured learner/calendar binding, owner authentication, write-pause state, hourly schedule, model/effort and intentionally disabled backups.

During private preview review, distinguish a literal recipient phrase from collector-verified class-calendar audience context. Only `school_event` items from a complete authenticated served class-calendar view with unchanged selected learner/class may use that separate context for an ordinary school action; the body still needs literal action and grounded date evidence. Missing/changed proof, conflicting recipients, parent-role restrictions and unknown or conditional participation do not authorize new writes. Revalidate the stored recipient basis against the current source before activation and queued writes; preserve every human decision. Audience-backed dispatch requires an activation receipt for the current class: old-class work and fresh new-class proof with an old receipt both remain deferred until fresh validated activation. Neither an authenticated route nor a successful fetch alone establishes audience. Attachments remain excluded.

Preview collection and analysis run after the existing message analysis, reconciliation and Calendar writes. This separate phase uses at most ten minutes, half the remaining whole-run time and 31 model calls shared by extraction, matching, legacy-evidence recovery and independent semantic verification. Small-source reservation allows one extraction and up to three calls per non-ignore action (matching, recovery and verification): `1 + 3N <= 31`, or at most ten such actions when recovery is available. Without that capability, `1 + 2N <= 31` applies. Each action can use at most one recovery batch of up to 12 candidates; the reservation always protects remaining primary matching calls. A source that fits a fresh budget is deferred whole when the current budget is insufficient; no incomplete result is cached as ready. Unusually large sources reserve calls for primary matching and may show explicit unverified review outcomes when the optional verifier budget runs out. Such outcomes establish no target, link, write authorization or confirmed action identity. Completed previews stay saved; deferred or interrupted source versions remain in the durable queue for later runs, with a visible reason. Preview expiry does not fail an otherwise successful message sync. The CLI main-thread worker runs this phase; background-thread callers defer it. Active mode still requires complete processing across every required channel for full sync success.

A planner contract version bump makes earlier preview results stale even when source content is unchanged. This explicit version is separate from the release SHA and must be bumped for extraction, prompt, matching or applicability changes. The panel and read-only API label earlier results stale, and readiness/monitoring and virtual matching use only the current planner version. Scheduled preview runs recompute stale results within the same budget; wait for full current coverage before acceptance or activation. A partial activation may refresh only its exact recorded current source selection, without replaying ordinary analyzed history or altering decisions, owned events or journals. Create a fresh activation plan after refresh; an older planner's plan is refused before collection. Candidate retrieval removes only explicitly proven incompatible typed actions before its unchanged 12-candidate bound. Remaining overflow requires review and supplies no duplicate/update authorization.

1. Validate the release with synthetic collectors, source/action replay, migration/retention, decision-preservation and UI tests. Review safe aggregate metadata and source capabilities. Offline success does not establish live school compatibility.
2. Stage the reviewed immutable release with new sources in `preview`. Collection must verify the selected authenticated learner, all pages and full bodies. Unsupported, partial or ambiguous source discovery stays visible as incomplete. Preview analyses remain separate from live proposals, operations and decisions; attachments are metadata only, with no image, OCR, PDF or attachment-content requests.
3. Observe a real scheduled hourly run in preview. Check each required source's successful fetch, backlog, errors, learner verification and complete pagination. Inspect source results and uncertain duplicates privately. Verify zero preview Calendar writes and no changed human decisions or owned-event identities. A manual run or a future timer timestamp does not satisfy this gate.
4. After the preview criteria and actual scheduled gate pass, switch only the approved mode to `active`. Activation must always re-fetch, reanalyze and revalidate current source content, learner scope, canonical matches and human decisions; a saved preview is not authorization to replay stale actions.
5. Observe a real full hourly active run. Require complete active-source collection, no automatic backlog or unknown writes, unchanged human decisions and no duplicate event on full replay. Confirm UI and aggregate monitoring separately. Report deployment, preview, activation and full-hourly acceptance as separate pending or complete states.

The source panel exposes outcomes added, linked, enriched, review, ignored and analysis incomplete, with reasons and provenance. Existing `/api/messages`, `/api/proposals` and `/api/proposal-groups` shapes stay compatible. `/api/sources?limit=50&offset=0&kind=announcement` returns `{items,limit,offset,next_offset}` (limit 1–100); kinds are `message`, `announcement`, `school_event`, `substitution` (`school_calendar` is an alias). `/api/actions/{id}` includes supporting sources, links and preserved decision metadata. `/api/source-previews` is paginated and read-only; there is no web activation endpoint.

Netdata receives only whitelisted aggregates per source kind: mode, last successful fetch, pending/error counts and action-link counts, plus the separate ready-preview count. Older databases/exports keep missing source evidence unknown. Preview freshness never advances full active-sync success; every required active source must be complete. Existing alarm thresholds and routes remain unchanged, including the intentional disabled-backup policy.

### Exact activation and refusal recovery

After the real hourly preview gate, stop only the sync timer and wait for any active writer to finish. Do not stop or kill a running `librus-sync.service`. Protect the short plan/apply window from panel decisions. Keep the recorded write-pause policy unchanged. Both administrative commands acquire `sync.lock`; `already_running` means wait and retry after the existing owner finishes.

```sh
systemctl --user stop librus-sync.timer
systemctl --user show librus-sync.service -p ActiveState -p SubState -p Result -p ExecMainStatus
release_python="$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv/bin/python"
source_config="$HOME/.config/librus-calendar/config.json"
source_state="$HOME/.local/share/librus-calendar/current.sqlite3"
source_admin_dir="$HOME/.local/share/librus-calendar/admin"
install -d -m 0700 "$source_admin_dir"
activation_plan="$source_admin_dir/activation-$(date -u +%Y%m%dT%H%M%SZ).json"
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" source-activate --plan-file "$activation_plan"
```

Run the final command only after the service is inactive. Planning requires protected mode `preview`, complete verified collection for all three new channels and a ready preview for every current pending source version. It creates a new non-overwriting private 0600 plan bound to source versions, learner/calendar binding, decisions, events and journal state; output reports aggregates and `calendar_writes=0`. The plan contains private identifiers; never print, commit or share it.

Review the protected plan privately. Atomically change only `multi_source_mode` in the protected configuration to `active`, preserving file ownership/mode and every other setting. Check effective environment overrides too. The timer stays stopped. **Configuration alone does not supply a missing activation receipt.** Starting an activation apply durably suspends any older receipt until the new attempt completes; shared analysis, queue and claim stay blocked after failure or restart. A valid prior receipt persists across configuration mode changes when no new apply has started. Writes require a bound activation receipt and complete required source collection.

```sh
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" source-activate --plan-file "$activation_plan" --apply
```

Apply freshly collects and checks learner scope/content against the snapshot, reads the exact allowed Calendar and verifies owned-event fingerprints, then performs fresh analysis and matching. It never executes saved preview proposals. Before collection it durably marks activation incomplete. All receipt fields and removal of the marker commit atomically only when every step completes; the command itself reports zero Calendar writes. A partial or interrupted attempt keeps shared analysis, queue and claim blocked. A fresh retry plan must cover the failed attempt's exact selected sources and freshly analyze them; earlier preview payloads provide selection evidence only. On success, verify preservation and per-source queues before restoring the recorded timer state:

```sh
systemctl --user restart librus-web.service
systemctl --user start librus-monitor.service
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" status
systemctl --user start librus-sync.timer
systemctl --user list-timers librus-sync.timer
```

Restarting only web reloads its once-loaded mode; refreshing monitoring is separate. Start the timer only if it was active in the baseline, then require the actual scheduled full active run. No manual invocation substitutes for that gate. Preserve the existing timer rule, alarms and backup-disabled policy.

On changed source/version, learner, decision, journal, Calendar fingerprint, incomplete collection or analysis failure, leave the timer stopped and preserve the database/journals. Return only the protected mode to `preview`, restart web and refresh the monitor; do not alter decisions or restore a database. Resolve the specific cause, obtain fresh complete previews and create a new plan at a new path. The old snapshot is refused; do not edit its fingerprint or overwrite it. A lock refusal requires waiting, not another concurrent worker. Do not resume scheduled active writes after a refused/partial apply.

### Explicit local cleanup

Cleanup is separate from activation and never edits Calendar. Use it only for an explicitly reviewed bounded list of stale local proposals. Before the final activation plan, place a private 0600 request in the 0700 administration directory, using the shape below with actual reviewed identifiers supplied privately:

```json
{"changes":[{"proposal_id":"REVIEWED_PROPOSAL_ID","reason":"Reviewed local proposal is obsolete; retain existing Calendar and decisions."}]}
```

```sh
cleanup_request="$source_admin_dir/cleanup-request.json"
cleanup_plan="$source_admin_dir/cleanup-$(date -u +%Y%m%dT%H%M%SZ).json"
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" cleanup-existing --request-file "$cleanup_request" --plan-file "$cleanup_plan"
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" cleanup-existing --plan-file "$cleanup_plan" --apply
```

Review preview counts before apply; apply accepts only that saved plan, without `--request-file`. Snapshot changes, approved/edited/applied actions and attempted/inflight/unknown write evidence refuse cleanup. Explicit rejections remain rejected; only eligible obsolete local work is retired, with IDs, decisions and journals retained. Changed snapshots require a newly reviewed request/plan. Run cleanup before generating the final activation plan, then refresh preview evidence as needed; never bypass a refusal with SQL, state replacement or Calendar deletion.
