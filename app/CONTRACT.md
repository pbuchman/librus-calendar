# Shared application contract

`from app.core import AppStore`; default `AppStore()` uses `~/.local/share/librus-calendar/state.sqlite3`. All public return values are JSON serializable dictionaries/lists. Technical health timestamps use ISO 8601 UTC; school dates use Europe/Warsaw. The sole allowed calendar and verified owner are explicit `AppConfig` values. No calendar or account is selected by default.

## Store API for web/runtime controller

- `status()` -> {messages, proposals_pending, proposals_review, events, operations_unknown, writes_paused, last_sync, last_error, sync_requested_at, ...health fields below}. `last_sync` is the start of the last attempt, including failures, and is never migrated into a successful timestamp.
- `list_messages(limit=100)` -> list of {id, subject, sender, sent_at, text, status, last_error, analysis_metadata, analyzed_at, ...}. Legacy provenance is null until an explicit successful analysis; opening the store never invents provenance or reanalyzes historical messages.
- `get_message(id)` -> dictionary above or None.
- `list_proposals(status=None, limit=100)` -> proposal dictionaries including id, message_id, status, kind, temporal_kind, activity_scope, due_at, title, description, start, end, all_day, confidence, needs_review, review_reason, source_quote, event_id (optional). `superseded` proposals retain their original payload and cannot be approved, queued or replayed.
- `get_analysis_history(message_id, limit=20)` -> private audit dictionaries with source_hash, analyzed_at, mode (`sync` / `cached_reanalysis`), metadata, proposal_ids and superseded_ids. Successful analysis and its proposals/provenance are one transaction, including zero-proposal analyses.
- `list_events(limit=100, include_cancelled=False)` -> dictionaries with id (Google event ID), calendar_id, message_id, proposal_id, snapshot (Google event), fingerprint, marker, updated_at.
- `resolve_proposal(id, decision, patch=None)` -> updated proposal or raises ValueError. decision `approve` / `ignore` / `confirm_cancel`; patch keys title,description,start,end,all_day only. Approval remains subject to core hard checks (future, calendar, no attendees), but clears uncertainty and uses confidence high.
- `set_writes_paused(bool)` -> current status dictionary. Default paused until the operator explicitly enables writes.
- `request_sync()` -> {requested: true, requested_at}; UI should also invoke supplied callback to start fixed systemd service.
- `backup(destination=None)` -> private backup path string after SQLite integrity check and file sync. Attempts/results are recorded separately; failed/incomplete backups never advance `last_backup_success_at`. Existing destination files are never overwritten.

## Synchronization lifecycle and health reader

`SyncController(store, client=None, runtime=None, *, client_factory=None, runtime_factory=None).run(...)` acquires the process lock and persists a technical run before calling `client_factory(stage_callback)` (credentials/login) or `runtime_factory()`. Callers must use factories for external client initialization. A concurrent invocation returns `already_running` without changing the running attempt. Each attempt has a random ID, UTC start/end, stage, result, allowlisted error code and duration; history expires after 90 days. There are no message IDs, contents or free text in this history.

The calendar probe must verify the exact calendar with owner/writer access before ordinary synchronization analysis or execution. `ok` requires successful fetch/analysis, an established historical discovery cursor, and no pending messages, automatic proposals/operations or unknown outcomes. Empty inboxes and proposals awaiting manual review can succeed. Initial discovery exceeding the per-run bound is partial until the history cursor reaches its end/window; later bounded audits of known history can succeed, while a bound exhausted with new/changed data remains partial. Analysis uses `gpt-6.1-sol` / `high`; calendar tool execution remains `gpt-6-luna` / `medium`.

`dry_run` and `paused` are explicit successful administrative outcomes that do not advance `last_successful_sync` or reset previous failure counts. `partial`, `failed` and `interrupted` increment the consecutive failure count and produce nonzero CLI exit status. `ok` resets the count and alone advances `last_successful_sync`. SIGTERM/SIGINT and a whole-run 45 minute deadline finish the attempt as interrupted; subprocess groups are stopped and inflight writes become unknown. The next locked worker recovers any abandoned running attempt. Unknown outcomes only undergo read-only reconciliation.

`from app.health_state import read_monitoring_state; read_monitoring_state(path, now=None)` uses SQLite `mode=ro`, `query_only` and a consistent transaction. It never calls AppStore, creates files, changes permissions or migrates schema. An unusable/missing database raises an exception. Legacy databases return available attempt/queue data and null new timestamps; they never invent a full success.

Schema version 1 fields: `schema_version`, `initialized_at`, `last_attempt_at`, `last_successful_sync`, `last_result`, `last_stage`, `last_error_code`, `last_duration_seconds`, `running_since`, `consecutive_failures`, `writes_paused`, `writes_paused_since`, `messages_pending`, `operations_pending`, `operations_unknown`, `oldest_pending_at`, `review_count`, `last_backup_success_at`, `last_backup_attempt_at`, `last_backup_result`.

Timestamps are UTC ISO strings or null, counters are integers, pause is boolean. Results are `idle`, `running`, `ok`, `partial`, `failed`, `interrupted`, `paused`, `dry_run`. Backup result is null, `running`, `ok` or `failed`. Pending messages include fetch and analysis work; pending operations include automatic proposals still lacking a queued operation and metadata_refresh rows in pending/inflight. Only metadata_refresh status unknown contributes to operations_unknown: a metadata CLI runs outside the systemd sync run, so its live inflight write is pending rather than an immediate unknown alarm. The next ordinary worker holding sync.lock converts abandoned metadata inflight to unknown; it can never reclassify a live locked CLI. Normal calendar operation inflight remains counted as unknown. `oldest_pending_at` includes pending messages and automatic operations/proposals plus pending/inflight metadata preparation times, excludes manual review, and uses the monitoring initialization time for legacy pending rows without an enqueue timestamp. Message `pending_since` is set when work enters its queue, preserved through retries, and cleared after analysis; editing a previously analyzed message starts a new queue age. The reader returns metadata only, never credentials, message IDs, bodies or runtime evidence.

## Runtime analysis

`CodexRuntime(config: AppConfig | None = None).analyze(message:dict, related_events:list[dict]) -> list[dict]`.
`last_analysis_metadata` is reset before each call and set only after that call succeeds: {model, effort, prompt_version: `school-actions-v3`, decision_reason}. The model supplies a nonblank decision reason even when proposals is empty. The store persists only these documented fields, not arbitrary runtime metadata. A compatible legacy runtime with missing metadata is recorded with unknown model/effort/prompt_version and an explicit missing-reason explanation; it never inherits provenance from a previous call.
Message shape is Store message row (id,subject,sender,sent_at,text). Related events are Store list_events dictionaries; user modifications must be checked again by execute, never inferred safe from cached fingerprints.

Analysis proposal schema: {kind: "create"|"update"|"cancel"|"ignore", temporal_kind: "event"|"deadline"|"reminder", activity_scope: "school"|"extracurricular"|"unknown", due_at: ISO timestamp|null, title: str, description: str, start: ISO date or datetime, end: ISO date or datetime|null, all_day: bool, confidence:"high"|"medium"|"low", needs_review:bool, review_reason:str, source_quote:str, event_id:str|null}. Core fills message_id/id and validates. Existing proposals lacking temporal_kind default to `event`; missing activity_scope defaults to `unknown`. `ignore` may have empty date fields. Update requires event_id of an app-owned event. Cancellation is always initial manual review; only explicit confirm_cancel from the panel grants trusted deletion_approved=true. Analysis cannot grant this. Uncertain proposals -> review, past -> ignored. App titles start `[Librus]`; normalization strips repeated legacy `[Szkoła]` and current prefixes before adding one `[Librus]`.

Ordinary school actions with high or medium confidence and needs_review=false may be automatic, including optional classroom crafts and uniquely established preparation dates. Genuine ambiguity, missing source evidence, duplicate/conflicting dates and cancellation remain review. Extracurricular creates require participation approval; any nonignored unknown-scope action requires review unless already human-approved. Scope is per action, so an extra-activity offer in the same message cannot block a separately classified school action. A narrow enrollment-expression guard applies only to unknown legacy proposals; `chętni` alone is not a registration requirement. Existing human-approved proposals are not reanalyzed or reclassified by migration.

A deadline is a task due by a date/time, persisted as all_day=true with date-only start and exclusive next-day end. The prompt emits end=null; core normalizes a stale end to exactly one calendar day. due_at is nullable, but if present must include an explicit Europe/Warsaw-valid numeric offset and fall on the start day. The exact timestamp is retained; title and description show `do HH:MM`, and the description includes its date. No one-hour duration is inferred for deadlines. A timed deadline or invalid/mismatched due_at becomes review and cannot reach Calendar. Date-only reminders likewise require all_day=true. A uniquely inferred preparation date connected to an ordinary school activity may be automatic at medium confidence. Ambiguous inferred dates require review. Ordinary timed events retain the explicitly noted one-hour fallback. Source-quote, optional-participation, duplicate and unknown-write safeguards apply independently of temporal classification.

## Explicit cached reanalysis

`python -m app.cli [--state PATH] reanalyze --message-id ID [--message-id ID ...] [--apply]` selects 1–30 unique explicit cached IDs. Default preview runs analysis and returns temporal classifications, dates/times, review reasons and current-call provenance without saving its result. `--apply` persists successful source-level replacements. No automatic history-wide backfill exists.

`SyncController.reanalyze(message_ids, apply=False, current=None)` acquires the same nonblocking sync.lock before scope checks or runtime initialization. The CLI `reanalyze_cached(...)` also locks before opening/migrating the database. Every selected message is prevalidated before LLM initialization. A source with any historical event/tombstone, applied/ignored proposal, human approval/edit/rejection, source-hash mismatch or attempted/review/inflight/unknown operation refuses the whole request. Only untouched pending/review proposals on the current cached source and pending operations with attempts=0 and no result are replaceable. Reanalysis does not assume participation has been confirmed.

Apply reserves the SQLite write lock with BEGIN IMMEDIATE before rechecking scope and source hash, then atomically supersedes retired proposals/unattempted operations and saves replacement proposals plus analysis audit. Web decisions reserve that lock before reading the proposal too: a decision committed first prevents replacement, and a waiting decision cannot resurrect a superseded payload. Old evidence is retained; an unchanged rerun keeps the same active proposal IDs. Superseded operations are excluded from queue, claims and health counts. Replacement does not queue writes: a later ordinary sync retains all usual calendar guards.

Cached reanalysis initializes no Librus client, performs no discovery/login or Calendar probe/write/reconciliation, does not recover inflight operations, and never starts/finishes a sync run or advances last_sync/last_successful_sync. Only the analysis LLM may use network access. Preview/apply/refused are administrative reanalysis results, never synchronization health successes.

## Runtime execution

`CodexRuntime.execute(operation:dict) -> dict`.
Operation: {operation_id, action:"create"|"update"|"cancel", deletion_approved:bool, calendar_id:"calendar@example.test", marker:"librus-calendar:<stable-operation-id>", event_id:null|string, expected_fingerprint:null|string, event:Google-event-body, source_message_ids:[id], proposal_id, message_id, synchronization_started_at: UTC ISO timestamp}.
Event body: summary,description,start/end date or dateTime+timeZone, reminders.useDefault=false with popup minutes timed1440/60 or all-day360, no attendees. Description includes source ID, quote, operation marker and explicitly noted default one-hour duration when inferred. The label `Synchronizacja rozpoczęta: DD.MM.YYYY HH:MM:SS (Europe/Warsaw)` means the durable operation's preparation/start time, never a completed write. It is frozen once in synchronization_started_at before external execution. Queue reruns, definitely-unwritten retries and unknown reconciliation retain the exact body/timestamp/fingerprint; the label is not appended repeatedly. Legacy already-attempted intents remain immutable.

Result: {status:"applied"|"review"|"unknown"|"failed", event_id:string|null, event_url:string|null, snapshot:Google-event|null, fingerprint:string|null, error:string|null}. `failed` means definitely no write happened; only that state is retryable. `unknown` means possible write, do not blindly repeat. Runtime should reconcile by operation marker before any create and compare expected fingerprint before update/cancel. Never touch events lacking app marker; never send invitations. A review result records conflict and leaves manual decision.

Core operation lifecycle pending -> inflight -> applied/review/unknown; safe failed -> pending. Interrupted inflight is reconciled as unknown by default. Event writes skip completely with --dry-run or writes_paused.

Unknown/inflight recovery uses operation.reconcile_only=true: runtime may only read/search; never create/update/delete. Description marker is the authoritative ownership marker because connector create does not support extendedProperties.

Cancelled events remain private tombstones (`state=cancelled`) and are excluded from default list_events/status active counts. Backups retain the newest seven default backup files. Any identical start instant and all-day mode across messages or tombstones requires review, even when titles differ. Source quotes for automatic create/update must appear literally after whitespace normalization in the cached message body.

Cancellation operations include trusted prior_snapshot, prior_fingerprint and prior_marker from the persisted owned event. Read-only recovery may restore these fields for a legacy unknown operation only if its expected fingerprint matches that stored event. Runtime validates the snapshot ID, canonical fingerprint and marker before using authoritative 404/cancelled evidence to synthesize a tombstone preserving the old ownership marker. Core accepts cancellation as applied only for the exact target ID with status cancelled and the prior marker.

## Existing event metadata refresh

`python -m app.cli [--state PATH] metadata-refresh --event-id ID [--event-id ID ...] [--apply]` selects 1–30 unique explicit existing event IDs. Default preview shows cached before/after titles, frozen preparation time and fingerprints without initializing Calendar access or writing journal intent. Apply uses the same sync.lock, validates every target, verifies Calendar access, then updates each exact existing ID through CodexRuntime.execute(action=update). No Librus login, analysis, creation, deletion or invitation is involved.

Only active, owned events on the allowed calendar with valid canonical cached fingerprints, original ownership markers and no external attendees/recurrence/outstanding event operation are eligible. The migration changes the title prefix and adds one preparation-time label to the existing description. Dates, duration, reminders, location, original marker, event ID, privacy and attendees are preserved. A live manual edit causes fingerprint-conflict review; migration never restores a stale snapshot. It never modifies original proposals or their human approvals. Verified persistence updates only the existing events snapshot/fingerprint/updated_at and preserves message/proposal associations.

Separate metadata_refresh journal identity is digest(version,event_id), version `librus-branding-v1`; runtime operation_id remains the original marker suffix. Intent plus timestamp is durable before claim/write. Lifecycle pending -> inflight -> applied/review/unknown; only a definitely-unwritten failed result returns to pending. Inflight left by interruption is unknown, and any unknown/inflight resume performs read-only reconciliation through the validated executor. An ambiguous absent write never authorizes retry. Every safe failed retry performs a fresh live fingerprint read. Completed reruns retain the same timestamp and perform no Calendar calls.

Metadata pending/inflight/unknown rows contribute to health queues as defined above and prevent ordinary sync from claiming full success. A process crash after claim is detected by the next worker holding sync.lock, which marks the abandoned metadata row unknown. Recovery requires the explicit metadata-refresh --apply command for the same IDs; ordinary sync does not execute/reconcile migration journal rows. Metadata refresh does not start/finish a sync run or advance last_sync/last_successful_sync. There is no database rollback and no event recreation after partial completion; the durable journal is resumed.

## Instance configuration

`AppConfig` is immutable and validated. `load_config()` loads an optional private
`~/.config/librus-calendar/config.json` (owned regular file, mode 600), then applies
explicit `LIBRUS_<FIELD_NAME>` overrides. `LIBRUS_CONFIG_FILE` selects another
private file; an explicitly supplied missing file is an error. Blank
`LIBRUS_CONFIG_FILE` skips file discovery. Unknown fields and malformed values
fail closed, and errors never echo supplied values. Paths, models, reasoning
efforts, the Codex executable, timeout, calendar ID/owner/connector, and trusted
web identity/hosts are configurable.

`AppStore(path=None, config=None)` and `CodexRuntime(config=None, run_process=None)`
accept explicit instance configuration. Calendar tooling requires all three
calendar fields before launching Codex or contacting the connector. The calendar
owner is distinct from the calendar ID; only its verified self attendee is
excluded from mutation/fingerprint checks. The standard policy remains SOL/high
analysis and Luna/medium calendar execution, with configured model names recorded
in provenance.

`create_app(..., config=None, demo_mode=False, now=None)` validates trusted web
identity and hosts before opening state. Demo mode requires an explicitly
injected store, configuration, synchronization callback and loopback hosts.
The optional clock callable supplies an aware datetime for deterministic
rendering. Demo entrypoints use synthetic data and isolated temporary stores;
normal environment/credential/state discovery is never needed.
