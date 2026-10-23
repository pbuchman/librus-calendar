# Shared application contract

`from app.core import AppStore`; default `AppStore()` uses `~/.local/share/librus-calendar/state.sqlite3`. All public return values are JSON serializable dictionaries/lists. Technical health timestamps use ISO 8601 UTC; school dates use Europe/Warsaw. The sole allowed calendar and verified owner are explicit `AppConfig` values. No calendar or account is selected by default.

## Store API for web/runtime controller

- `status()` -> {messages, proposals_pending, proposals_review, events, operations_unknown, writes_paused, last_sync, last_error, sync_requested_at, ...health fields below}. `last_sync` is the start of the last attempt, including failures, and is never migrated into a successful timestamp.
- `list_messages(limit=100)` -> list of {id, subject, sender, sent_at, text, status, last_error, analysis_metadata, analyzed_at, ...}. Expired `retained` source placeholders are hidden; `get_message` still resolves their IDs for ownership references. Legacy provenance is null until an explicit successful analysis; opening the store never invents provenance or reanalyzes historical messages.
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

The calendar probe must verify the exact calendar with owner/writer access before ordinary synchronization analysis or execution. `ok` requires successful fetch/analysis, an established historical discovery cursor, and no pending messages, automatic proposals/operations or unknown outcomes. Empty inboxes and proposals awaiting manual review can succeed. Initial discovery exceeding the per-run bound is partial until the history cursor reaches its end/window; later bounded audits of known history can succeed, while a bound exhausted with new/changed data remains partial. Analysis and every calendar tool call default to `gpt-6.1-sol` / `high`; explicit configuration overrides remain supported.

The initial read-only `list_calendars` probe permits at most two attempts, five seconds apart. Only a structured unexpected completed tool-call-count failure (zero or multiple) whose observed calls all match the exact expected read and arguments qualifies. Unfinished calls, unexpected calls/arguments, writes, malformed protocol/results, timeout, permission failure or missing/non-writable calendar never qualify. Per-call runtime state resets before each attempt. This policy never directly retries calendar mutations; uncertain write outcomes require read-only reconciliation. A recovered probe can yield a normal full sync success without incrementing consecutive failures.

The runtime accepts ordinary MCP or code-mode transport only with verified evidence of the exact authorized underlying MCP tool and arguments; a model's claimed result is never sufficient, regardless of transport.

The additive `calendar_probe_attempts` table records technical audit metadata only, starting an attempt durably before launching the process; audit persistence failure stops progress. Attempt history expires after 90 days and stores no calendar IDs, tool arguments/results, prompt/output text or message content. Standalone setup probes without an active sync run do not invent sync history.

`dry_run` and `paused` are explicit successful administrative outcomes that do not advance `last_successful_sync` or reset previous failure counts. `partial`, `failed` and `interrupted` increment the consecutive failure count and produce nonzero CLI exit status. `ok` resets the count and alone advances `last_successful_sync`. SIGTERM/SIGINT and a whole-run 45 minute deadline finish the attempt as interrupted; subprocess groups are stopped and inflight writes become unknown. The next locked worker recovers any abandoned running attempt. Unknown outcomes only undergo read-only reconciliation.

`from app.health_state import read_monitoring_state; read_monitoring_state(path, now=None)` uses SQLite `mode=ro`, `query_only` and a consistent transaction. It never calls AppStore, creates files, changes permissions or migrates schema. An unusable/missing database raises an exception. Legacy databases return available attempt/queue data and null new timestamps; they never invent a full success.

Schema version 1 fields: `schema_version`, `initialized_at`, `last_attempt_at`, `last_successful_sync`, `last_result`, `last_stage`, `last_error_code`, `last_duration_seconds`, `running_since`, `consecutive_failures`, `writes_paused`, `writes_paused_since`, `messages_pending`, `operations_pending`, `operations_unknown`, `oldest_pending_at`, `review_count`, `last_backup_success_at`, `last_backup_attempt_at`, `last_backup_result`.

Optional additive fields are `calendar_probe_result` (null or `running`/`ok`/`failed`/`interrupted`), `calendar_probe_attempts` (null or integer 1–2), `calendar_probe_recovered` (null with no evidence/running, otherwise boolean), `calendar_probe_retries_24h` and `calendar_probe_recoveries_24h` (null or nonnegative integers). Latest probe fields describe the selected latest sync run; no recorded attempt means null, including a run that failed before its probe. Recovery is true only for a successful second attempt. Rolling counters count second-attempt starts and successful second-attempt completions in the last 24 hours respectively. Missing table/no attempt evidence means null, never invented success or zero counts. Existing consumers may ignore new fields; new exporters/collectors accept legacy missing/null values and reject malformed supplied values. Netdata emits no value for missing/null counters and adds only an informational absolute-count chart, with no new alarms.

Timestamps are UTC ISO strings or null, counters are integers, pause is boolean. Results are `idle`, `running`, `ok`, `partial`, `failed`, `interrupted`, `paused`, `dry_run`. Backup result is null, `running`, `ok` or `failed`. Pending messages include fetch and analysis work; pending operations include automatic proposals still lacking a queued operation and metadata_refresh rows in pending/inflight. Only metadata_refresh status unknown contributes to operations_unknown: a metadata CLI runs outside the systemd sync run, so its live inflight write is pending rather than an immediate unknown alarm. The next ordinary worker holding sync.lock converts abandoned metadata inflight to unknown; it can never reclassify a live locked CLI. Normal calendar operation inflight remains counted as unknown. `oldest_pending_at` includes pending messages and automatic operations/proposals plus pending/inflight metadata preparation times and excludes manual review. Message `pending_since` is set when work enters its queue, preserved through retries, and cleared after analysis; editing a previously analyzed message starts a new queue age. Legacy messages without an enqueue timestamp use the monitoring initialization time.

Proposal `pending_since` starts when a new automatic proposal is pending or a nonpending proposal becomes pending after approval. Reapproving already-pending work, preparing its operation, failed retries and reopening state preserve that eligibility time; leaving pending clears it. A linked pending operation uses the proposal's eligibility time, so preparation does not restart the backlog clock. Original `created_at` and mutable `updated_at` have separate meanings and are not substituted for a known eligibility time. The read-only health reader supports old schemas without migration; missing proposal eligibility falls back conservatively to proposal creation (or operation creation when no pending proposal exists). AppStore backfills only missing eligibility for existing pending proposals from creation and never resets known times. Decision transfer preserves this time from new sources and creation from old sources; a decision-only source without a proposal uses its known initialization time, or its recorded decision timestamp if initialization is absent, as a conservative baseline rather than claiming an exact eligibility transition. The reader returns metadata only, never credentials, message IDs, bodies or runtime evidence.

The additive proposal column requires forward recovery after migration: earlier releases with positional seven-column proposal inserts are not write-compatible with this schema. Do not select such a packaged release after migration without explicit compatibility verification; preserve the current state and journal when deploying a forward fix.

## Whole-agenda planning

`CodexRuntime.plan_agenda(snapshot)` receives one coherent bounded snapshot of relevant message, announcement and school-calendar text, existing agenda, current owned Calendar state, verified learner context and saved decisions. It runs one tool-free configured `gpt-6.1-sol/high` task and returns `{snapshot_hash,decision_reason,items}`. Items use `operation=create|link|update|review|ignore`, nullable existing `action_id`/`proposal_id`, exact `sources:[{source_id,source_hash}]`, reason, nullable normalized action/review change and offer references. Application validation checks existing references, learner scope, conflicts, ownership, saved decisions and dates. Application code assigns stable IDs and persists the plan and active materialization transactionally before queueing.

The actual incremental request is measured before invocation (160000 UTF-8 bytes); overflow fails visibly, without truncation or per-candidate calls. Hash reuse includes relevant source versions, agenda/Calendar, decisions, learner, policy/planner and model settings. Collection timestamps and a new day alone do not invalidate it. Full local state still participates in CAS without a prompt byte budget. The exact serialized request has a separate SHA-256 hash. Successful own writes advance content and authority baselines; external changes invalidate it. Unchanged runs still reconcile and check health. Review blocks affected work, independent clear school obligations may proceed, dependent offer choices remain atomic. Missing sources/items do not authorize deletion. Human-approved hours/edits/rejections and one-choice offers remain authoritative; material changes and cancellations require explicit review.

`latest_agenda_plan(preview=False)` separates active statuses from `preview`/`preview_failed` and returns `{id,input_hash,status,created_at,error_code,metadata,payload}`. `agenda_status()` returns `{engine:'simple-agenda',latest_plan,latest_preview}` summaries without payload. Metadata is a safe whitelist including selected `source_count`, whole `catalog_count`, actual `input_bytes` and `request_hash`; narrative reasons stay private payloads. `agenda-preview` and `sync --dry-run` read the last complete stored collection plus fresh Calendar, and save only isolated preview rows; no Librus refresh, active proposals, decisions, groups, operations or Calendar writes. Preview records `source_collection_at` and `sources_refreshed_by_preview=false`. Exact previews may be reused only after a fresh unchanged-state check. Normal CLI output is aggregate-only.

Existing normalized proposal fields and durable execution policy remain: timed/all-day events, deadlines/reminders, Europe/Warsaw validation, source evidence, confidence, review, activity scope, ownership and explicit cancellation approval. A deadline uses one all-day entry with exact `due_at` retained. Optional extracurricular participation and unknown scope require review. Ordinary clear school obligations may be automatic. Legacy proposal/analysis audit APIs remain compatibility/history surfaces, not independent active message inference.

## Explicit alternative offers

Analysis adds nullable `alternative_group` and `alternative_group_evidence` fields.
New strict output includes both; compatible older output defaults them to null.
Only explicitly related create/event/extracurricular alternatives with two to
thirty distinct dates and the same matching source excerpt form a group. No title
similarity or language keyword list establishes membership. Independent obligations
and recurring course lessons remain separate proposals. Unsupported grouping claims
lose their group fields and remain manual review; they never gain automatic writes.

Additive `proposal_groups` and `proposal_group_members` tables bind stable existing
proposal IDs to one message/source hash. States are `open`, `selected`, `rejected`.
`AppStore.list_proposal_groups(message_id=None,status=None,current=None)`,
`get_proposal_group(id,current=None)` and
`get_proposal_group_for_proposal(id,current=None)` return consistent complete groups,
independent of the ordinary 100-proposal/message page limits. Shape is
`{id,message_id,source_hash,group_key,source_evidence,status,selected_proposal_id,
revision,stale,members:[proposal+eligible+ineligible_reason]}` with members sorted
chronologically. Ineligibility codes are `past`, `stale`, `closed`, `unavailable`,
`invalid`. The optional aware clock supports deterministic demo/testing.

`resolve_proposal_group(group_id,decision,proposal_id=None,expected_revision=None,
current=None)` accepts `select` for exactly one future member or `reject` for all.
It reserves `BEGIN IMMEDIATE` before its first read, verifies source/revision and
complete membership, and atomically approves the selected proposal, rejects every
sibling, and records all member `decision_intents`. It never contacts Calendar.
Identical repeated decisions return the closed state without another mutation;
conflicting decisions fail. Past selection, stale source, external write evidence,
invalid revision and persistence failure leave the whole group unchanged.

Open and unselected group members cannot use individual proposal decisions. An
already selected member may use the established individual review/rebase path,
with unchanged source, operation and owned-event guards; its siblings remain closed.
Queue preparation and claim independently verify the selected membership. Known
independent offers remain separate even when they share a date/source quote.
Reanalysis cannot supersede a closed group. Ordinary reconstruction retains exact
human intent; unknown/renamed grouping requires matching literal offer evidence.
Ambiguous replacements remain review with `blocked_by_group_decision`; approval
is refused, while explicit ignore can safely dismiss them. Dates alone never
transfer approval between offers. No new write retry behavior is introduced.

Successful analysis of a changed source atomically supersedes untouched members
and removes its unsupported old open group, without human-rejection flags. Retention
uses the same reservation before reading eligibility or flags and retires open
groups whose source expires. Closed groups retain minimal decision state/membership
through pruning and direct decision transfer; source excerpts are removed.

`group-existing --request-file PRIVATE_REQUEST_JSON --plan-file NEW_PRIVATE_PLAN_JSON`
previews exactly one explicitly confirmed legacy group. Request keys are
`{message_id,proposal_ids,source_evidence}` with 2–30 unique explicit current
create/event/extracurricular review proposals from one source. Request files must
be owned regular files mode 0600. The non-overwriting plan is 0600 in an owned 0700
directory and binds the absolute database path, source hash and exact proposal row
fingerprints. The explicit request may confirm alternatives whose source lists
sessions without specific choice keywords; evidence still must match the source.

`group-existing --plan-file PRIVATE_PLAN_JSON --apply` rechecks that plan under
sync.lock and an immediate SQLite transaction, then adds only group/membership
rows. Existing proposal IDs, payload bytes and operation journals are unchanged.
Any event history, human intent or operation journal in the selected scope refuses
grouping. Repeating an unchanged applied plan returns `already_applied`; changed
source, rows or human group state refuses. Outputs contain only counts, status,
`calendar_writes:0`, and the preview plan path. Neither command initializes Librus,
analysis or Calendar clients. A grouping request or plan is private operational data
and must never be committed.

## Runtime execution

`CodexRuntime.execute(operation:dict) -> dict`.
Operation: {operation_id, action:"create"|"update"|"cancel", deletion_approved:bool, calendar_id:"calendar@example.test", marker:"librus-calendar:<stable-operation-id>", event_id:null|string, expected_fingerprint:null|string, event:Google-event-body, source_message_ids:[id], proposal_id, message_id, synchronization_started_at: UTC ISO timestamp}.
Event body: summary,description,start/end date or dateTime+timeZone, reminders.useDefault=false with popup minutes timed1440/60 or all-day360, no attendees. Description includes source ID, quote, operation marker and explicitly noted default one-hour duration when inferred. The label `Synchronizacja rozpoczęta: DD.MM.YYYY HH:MM:SS (Europe/Warsaw)` means the durable operation's preparation/start time, never a completed write. It is frozen once in synchronization_started_at before external execution. Queue reruns, definitely-unwritten retries and unknown reconciliation retain the exact body/timestamp/fingerprint; the label is not appended repeatedly. Legacy already-attempted intents remain immutable.

Result: {status:"applied"|"review"|"unknown"|"failed", event_id:string|null, event_url:string|null, snapshot:Google-event|null, fingerprint:string|null, error:string|null}. `failed` means definitely no write happened; only that state is retryable. `unknown` means possible write, do not blindly repeat. Runtime should reconcile by operation marker before any create and compare expected fingerprint before update/cancel. Never touch events lacking app marker; never send invitations. A review result records conflict and leaves manual decision.

Core operation lifecycle pending -> inflight -> applied/review/unknown; safe failed -> pending. Interrupted inflight is reconciled as unknown by default. Event writes skip completely with --dry-run or writes_paused.

A human approval, edit, rejection or confirmed cancellation protects the exact owned event across later AI proposal kinds and source messages. Automatic update/cancel proposals for that event enter review during analysis, queue preparation and atomic claim. A new explicit approval authorizes its current update; cancellation additionally requires `confirm_cancel`. Persisted decision intents retain minimal exact `event_ids`, including settled create-result identities, so later association/date changes and same-source siblings do not lose that protection. Repeating decision transfer may augment these IDs when the existing source hash, chosen proposal and signature match exactly, preserving cached analysis and journals.

Queue preparation reserves a SQLite write transaction through proposal normalization and operation persistence. `claim_operation(operation_id, expected_operation=None)` rechecks the trusted stored action/target and human-intent guard under the same reservation as its pending-to-inflight transition. Ordinary sync supplies the complete payload it will execute as `expected_operation`; a newer UI edit causes the stale cached payload to be refused while the latest approved intent remains pending. The runtime still compares the original Calendar fingerprint before any update or cancellation.

Unknown/inflight recovery uses operation.reconcile_only=true: runtime may only read/search; never create/update/delete. Description marker is the authoritative ownership marker because connector create does not support extendedProperties.

Cancelled events remain private tombstones (`state=cancelled`) and are excluded from default list_events/status active counts. The optional manual `backup` command retains the newest seven files when using its default destination; automatic backups are disabled by default, and deployment and migration create no database copies. Any identical start instant and all-day mode across messages or tombstones requires review, even when titles differ. Source quotes for automatic create/update must appear literally after whitespace normalization in the cached message body.

Cancellation operations include trusted prior_snapshot, prior_fingerprint and prior_marker from the persisted owned event. Read-only recovery may restore these fields for a legacy unknown operation only if its expected fingerprint matches that stored event. Runtime validates the snapshot ID, canonical fingerprint and marker before using authoritative 404/cancelled evidence to synthesize a tombstone preserving the old ownership marker. Core accepts cancellation as applied only for the exact target ID with status cancelled and the prior marker.

## Existing event metadata refresh

`python -m app.cli [--state PATH] metadata-refresh --event-id ID [--event-id ID ...] [--apply]` selects 1–30 unique explicit existing event IDs. Default preview shows before/after titles, frozen preparation time and fingerprints without writing journal intent. A compact event snapshot requires a read-only Calendar fetch whose fingerprint matches the retained original baseline; complete cached snapshots need no fetch. Apply uses the same sync.lock, validates every target, verifies Calendar access, then updates each exact existing ID through CodexRuntime.execute(action=update). No Librus login, analysis, creation, deletion or invitation is involved.

Only active, owned events on the allowed calendar with valid canonical cached fingerprints, original ownership markers and no external attendees/recurrence/outstanding event operation are eligible. The migration changes the title prefix and adds one preparation-time label to the existing description. Dates, duration, reminders, location, original marker, event ID, privacy and attendees are preserved. A live manual edit causes fingerprint-conflict review; migration never restores a stale snapshot. It never modifies original proposals or their human approvals. Verified persistence updates only the existing events snapshot/fingerprint/updated_at and preserves message/proposal associations.

Separate metadata_refresh journal identity is digest(version,event_id), version `librus-branding-v1`; runtime operation_id remains the original marker suffix. Intent plus timestamp is durable before claim/write. Lifecycle pending -> inflight -> applied/review/unknown; only a definitely-unwritten failed result returns to pending. Inflight left by interruption is unknown, and any unknown/inflight resume performs read-only reconciliation through the validated executor. An ambiguous absent write never authorizes retry. Every safe failed retry performs a fresh live fingerprint read. Completed reruns retain the same timestamp and never reissue a write; complete cached snapshots need no Calendar call, while compact snapshots require read-only verification.

Metadata pending/inflight/unknown rows contribute to health queues as defined above and prevent ordinary sync from claiming full success. A process crash after claim is detected by the next worker holding sync.lock, which marks the abandoned metadata row unknown. Recovery requires the explicit metadata-refresh --apply command for the same IDs; ordinary sync does not execute/reconcile migration journal rows. Metadata refresh does not start/finish a sync run or advance last_sync/last_successful_sync. There is no database rollback and no event recreation after partial completion; the durable journal is resumed.

## Decision transfer, reconstruction and retention

All administrative commands accept global `--config PRIVATE_JSON` and `--state TARGET_DB` before the command. They acquire the normal process lock and emit aggregate counts only. They do not copy or export a database or write Calendar events.

- `migrate-decisions --from OLD_DB` calls `migrate_decisions(store, source_path)`. It opens the distinct, owned source directly with SQLite `mode=ro`, `query_only`, an integrity check and a consistent read transaction. It validates Calendar ID/owner bindings and the hashed Librus account before an atomic target transaction. Only explicit human approval/edit/rejection/cancellation intent, necessary event IDs/markers/conflict fingerprints/tombstones and unresolved operation/metadata journals transfer. Automatic ignored analyses are never classified as human rejection. No messages or analysis history transfer. Uncertain inflight journals become unknown. The target stays paused by default and requires reconstruction before new operations may queue.
- `migrate-decisions --from OLD_DB --allow-legacy-binding` explicitly authorizes an old schema lacking both Calendar bindings. It additionally requires a read-only runtime owner probe for the exact configured Calendar, a nonempty ownership table whose every Calendar ID matches, and a source Librus account hash. Partially supplied or conflicting bindings remain refused. The function API takes `allow_legacy_binding=True, verified_calendar=probe` for this deliberate exception.
- `rebuild --days 30 [--credentials PRIVATE_JSON]` uses `RebuildController`. Its runtime must be `CodexRuntime(config, read_only=True)`. It searches all Calendar pages by application marker/source description, verifies exact event IDs through reads, and keeps every distinct owned event for each source. Titles never establish ownership. It consumes all Librus metadata pages, including histories exceeding 100 messages, fetches only current-window sources and resumes durable per-message retries. Existing analyzed hashes avoid repeated AI calls. AI wording does not override durable human choices; exact source/action matches retain choices, uncertain matches enter review. It never queues or executes Calendar operations, even if the target's write pause is disabled.
- `prune --days 30` previews `prune_state`; `--apply` removes expired cached bodies, source quotes and obsolete analysis/audit content. It preserves source identity/hash placeholders, human choices, future action fields, all event ownership/tombstones and idempotency records. Complete unresolved write journals retain their immutable evidence. Settled operation/metadata journals retain only identifiers, markers, fingerprints and preparation timestamps. Unknown source dates are not guessed old. Ordinary synchronization applies the same retention. SQLite secure deletion and an in-place vacuum erase obsolete freed content when a body or audit was removed; no backup is created.

Rebuild records `rebuild_last_attempt_at` and `rebuild_status`; only a fully successful `rebuilt` outcome advances `rebuild_completed_at`. Incomplete/ambiguous ownership discovery blocks new ordinary operations until reconstruction succeeds. The gate is checked again atomically when claiming any existing operation, independently of queue limits. An identified manual Calendar edit retains the original conflict fingerprint and is a targeted review, allowing unrelated operations to continue. Normal runtime conflict evidence and explicit UI approval/rebase remain the way to review later updates; rebuild never accepts or overwrites a manual change automatically.

Compact snapshots contain `_content_pruned=true` and operational event fields, while their saved fingerprint continues to describe the original full remote state. Metadata refresh reads and verifies that full baseline before planning, or uses the exact immutable preimage already in an unresolved journal for reconciliation. Before claiming a cancellation from compact state, ordinary sync reads and durably freezes the unchanged full prior snapshot. An interrupted delete therefore remains reconcilable from authoritative absence without recreating the event. Settled compact metadata reruns remain idempotent and cannot dispatch a fresh write.

`CodexRuntime(..., read_only=True)` rejects every tool outside the fixed read allowlist (`list_calendars`, `search_events`, `list_events`, `read_event`, `fetch`) before launching a subprocess. Tool sessions enable destructive/open-world connector permission only for an actual allowed write method; read sessions disable both. Read-only `execute` always refuses. `discover_events()` rejects repeated pagination tokens, malformed responses and full pages whose continuation contract is unavailable; `read_event(id, expected_event)` preserves all-day/DST representations without inventing dates.

Manual `backup` remains a separate compatibility feature. `AppConfig.backup_enabled=False` is the default deployment/monitoring policy; installation and migration do not schedule or create database backups.

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
excluded from mutation/fingerprint checks. The standard policy uses SOL/high
analysis and calendar execution, with configured model names recorded
in provenance.

`create_app(..., config=None, demo_mode=False, now=None)` validates trusted web
identity and hosts before opening state. Demo mode requires an explicitly
injected store, configuration, synchronization callback and loopback hosts.
The optional clock callable supplies an aware datetime for deterministic
rendering. Demo entrypoints use synthetic data and isolated temporary stores;
normal environment/credential/state discovery is never needed.

## Source UI and monitoring

`source_status()` reports active `simple-agenda` mode, required-source collection health and counts; `active_pending` includes pending/failed sources. Legacy per-source preview counts are zero. `multi_source_mode` is inert compatibility configuration. The UI preserves existing message/proposal/group/approval endpoints and owner/CSRF enforcement. `/api/sources` is bounded and paginated. `/api/actions/{id}` includes all supporting source links and preserved decisions. `/preview` and `/api/agenda` expose the latest whole preview separately from the latest active plan, with collection time and no claim that stored state is current. A failed latest preview hides the older success. Model/source text is escaped. `/api/source-previews` labels old records as historical `legacy-source-planner`; these records grant no current readiness or write authority.

The read-only health reader uses persisted `agenda_engine='simple-agenda'` to ignore legacy preview mode and read real source pending/failed counts. Old databases retain their historical interpretation and need no migration. Optional `agenda` monitoring metadata contains only `engine`, separate latest active/preview status, timestamp and finite error code. No payload, ID, hash, title, source text or model rationale leaves SQLite. Existing Netdata source charts work in active mode with unchanged alarm thresholds; sync failures continue to fail normal health.

Text-bearing source, agenda-plan, preview and audit content uses the 30-day retention policy. Minimal future facts, decisions, ownership and unknown/inflight operation evidence remain protected. The existing 90-day retention applies only to technical sync/probe history.


Incremental source selection and checkpoint: `assemble_incremental_snapshot` accepts
an existing store or read-only SQLite connection plus fresh Calendar observations.
The request includes every agenda entry, decision and offer, a whole source catalog,
and full new/changed or affected context bodies. Each selected source must appear
in at least one plan item; grouped ignore acknowledges irrelevant/unavailable text.
New automatic create needs a supplied available `source_role=changed` body. Catalog
references remain valid for links and existing authority; absence never cancels.
The additive `agenda_processed_sources` ledger tracks content hash, deletion,
retention and body availability without raw bodies. Only compatible applied full
plans with exact analyzed timestamps can bootstrap analyzed non-retained/non-deleted
rows. Retained/deleted rows are presented once. Pending/failed state never bootstraps.
Materialization compares the complete local CAS, then persists plan, source ledger,
authority baseline and only exact corresponding message ID/hash statuses in one
transaction. Preview, failure, stale input and omitted selected sources never
advance any active checkpoint. Source content_hash is the collector's semantic
content-version invariant; complete stored payloads still participate in CAS.
See [incremental contract](../docs/incremental-agenda-contract.md).
