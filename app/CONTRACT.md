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

## Runtime analysis

`CodexRuntime(config: AppConfig | None = None).analyze(message:dict, related_events:list[dict]) -> list[dict]`.
`last_analysis_metadata` is reset before each call and set only after that call succeeds: {model, effort, prompt_version: `school-actions-v4-alternatives`, decision_reason}. The model supplies a nonblank decision reason even when proposals is empty. The store persists only these documented fields, not arbitrary runtime metadata. A compatible legacy runtime with missing metadata is recorded with unknown model/effort/prompt_version and an explicit missing-reason explanation; it never inherits provenance from a previous call.
Message shape is Store message row (id,subject,sender,sent_at,text). Related events are Store list_events dictionaries; user modifications must be checked again by execute, never inferred safe from cached fingerprints.

Analysis proposal schema: {kind: "create"|"update"|"cancel"|"ignore", temporal_kind: "event"|"deadline"|"reminder", activity_scope: "school"|"extracurricular"|"unknown", due_at: ISO timestamp|null, title: str, description: str, start: ISO date or datetime, end: ISO date or datetime|null, all_day: bool, confidence:"high"|"medium"|"low", needs_review:bool, review_reason:str, source_quote:str, event_id:str|null}. Core fills message_id/id and validates. Existing proposals lacking temporal_kind default to `event`; missing activity_scope defaults to `unknown`. `ignore` may have empty date fields. Update requires event_id of an app-owned event. Cancellation is always initial manual review; only explicit confirm_cancel from the panel grants trusted deletion_approved=true. Analysis cannot grant this. Uncertain proposals -> review, past -> ignored. App titles start `[Librus]`; normalization strips repeated legacy `[Szkoła]` and current prefixes before adding one `[Librus]`.

Ordinary school actions with high or medium confidence and needs_review=false may be automatic, including optional classroom crafts and uniquely established preparation dates. Genuine ambiguity, missing source evidence, duplicate/conflicting dates and cancellation remain review. Extracurricular creates require participation approval; any nonignored unknown-scope action requires review unless already human-approved. Scope is per action, so an extra-activity offer in the same message cannot block a separately classified school action. A narrow enrollment-expression guard applies only to unknown legacy proposals; `chętni` alone is not a registration requirement. Existing human-approved proposals are not reanalyzed or reclassified by migration.

A deadline is a task due by a date/time, persisted as all_day=true with date-only start and exclusive next-day end. The prompt emits end=null; core normalizes a stale end to exactly one calendar day. due_at is nullable, but if present must include an explicit Europe/Warsaw-valid numeric offset and fall on the start day. The exact timestamp is retained; title and description show `do HH:MM`, and the description includes its date. No one-hour duration is inferred for deadlines. A timed deadline or invalid/mismatched due_at becomes review and cannot reach Calendar. Date-only reminders likewise require all_day=true. A uniquely inferred preparation date connected to an ordinary school activity may be automatic at medium confidence. Ambiguous inferred dates require review. Ordinary timed events retain the explicitly noted one-hour fallback. Source-quote, optional-participation, duplicate and unknown-write safeguards apply independently of temporal classification.

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

## Additive source UI and monitoring

`metadata.served_class_calendar` version `served-class-calendar-v1` is trusted
collector observation for `school_event` only. Every collection verifies HTTPS
host/path and HTTP 200 for the authenticated `/rodzic/index`, `/informacja` and
`/terminarz` views, including month POST results and form targets. Userinfo,
unexpected ports/routes/selectors/controls fail closed. The complete calendar
control contract permits only its month/year form, hidden request key, and the
recognized external language selector. The selected learner/class must remain
identical through all month reads and original-month restoration. This proves
served class-calendar audience, without asserting an exclusive account-role
label or participation. No account or child switch is performed.

The stable envelope binds its version, view contract, learner/class and observed
source fingerprint; it excludes its own envelope, fetch/window timestamps and
nonces. The persisted source content hash includes the envelope. Ingestion rejects
malformed/wrong-source/context proof. Only an ordinary school action with exact
verified CLASS scope, `condition=none` and no conflicting IDs may use it in place
of a literal recipient phrase. Literal body action/identity and grounded dates
remain mandatory. Other scopes, role/unknown/participation conditions and
extracurricular activity gain no authority. Model-supplied `recipient_basis` is
discarded; the application computes that field from the persisted observation,
separately from `source_quote`. Retention removes the source proof while keeping
the minimal basis guard, so expired content cannot silently upgrade authority.

Source/decision CAS includes minimal verified learner/class context and is read
before analysis. Activation pins that context for every analysis and compares it
again in the final receipt transaction. The receipt records the captured class,
never a later observed class, and clears its durable incomplete-attempt guard
only with all receipt fields after full success. Audience-based queue/claim
revalidate the current source proof/context and class-bound activation receipt;
missing/old-class receipts or changed proof/context defer without rewriting
pending intent or a frozen operation. Literal and legacy paths retain their
existing receipt behavior; unknown journals remain reconciliation-only.

`GET /sources`, `/sources/{source_id}` and `/actions/{action_id}` are owner-authenticated, read-only views. Sources show kind, outcome/reason and text; canonical action details show certainty, optional-reminder labeling, retained decision metadata and every supporting source/quote/relation. Source kinds are `message`, `announcement`, `school_event`, `substitution`; `school_calendar` is accepted as a filter alias. Attachments remain visibly `załącznik nieanalizowany`, never downloaded or analyzed. Existing message/proposal/offer review paths keep their current auth, CSRF, revisions and decision guards.

`GET /api/sources` accepts `limit` (1–100, default 50), nonnegative `offset`, optional `kind` and `status`, returning `{items,limit,offset,next_offset}`. It fetches one sentinel row to determine the next offset. `GET /api/actions/{action_id}` includes canonical state, links and corresponding source records. `GET /api/source-previews` uses the same pagination envelope. `/preview` has no approval/activation path: source preview analyses cannot mutate proposals, operations, human decisions or Calendar. Existing `/api/messages`, `/api/proposals`, `/api/proposal-groups` response shapes remain compatible.

Health schema version 1 adds optional `sources: null|{mode,preview_count,kinds:{kind:{mode,last_successful_fetch,pending,failed,links,last_result,last_error}}}`. The read-only reader detects the additive tables without initialization or migration. It uses fixed aggregate queries plus private selected-context and incomplete-attempt identity metadata to determine retry eligibility; it selects no source/message bodies and emits aggregates only. Unknown source error strings map to `source_operation_failed`. Exporter and standalone collector validate fixed kinds/modes/counts/timestamps and an explicit finite error-code allowlist; extra private fields are not exported. Old databases/exports retain null or absent source evidence, yielding chart gaps rather than invented healthy values. Each source chart is informational; ready-preview counts are separate and do not advance full active-sync success. Existing alarms/thresholds/routes and backup-disabled behavior remain unchanged.

Candidate retrieval applies the existing deterministic distinct-action guard before relevance ranking and the 12-candidate cap, only when the distinguishing fields are explicitly known. Missing or unknown types provide no exclusion proof. Owned inventory-only entries stay visible regardless of legacy default types. More than 12 still-plausible candidates remains uncertain review; no matching, recovery or verification runs against a truncated set.

Planner v3 resolves virtual origins privately before consuming aliases, independent of preview timestamps. A virtual origin must be current-ready for the exact source/hash/planner and retain its deterministic source/hash/ordinal/action binding. Exactly one own-source literal quote must match its saved prepared action; context and original quote are rebuilt through the existing safe store projections. Serialized `evidence`, `original_evidence`, `reconstructed_evidence`, `recovery_origin` and virtual decision/event authority are untrusted. A real persisted `get_action` result takes precedence over virtual claims. Each prepared non-ignore action is conserved even when saved outcomes are missing, short or malformed; valid ignore/empty previews add no blocker. Invalid bindings become separate proofless inventory with untrusted exclusion/authority flags cleared, blocking all matching strategies, including closed-offer lookup, with fixed `unbound_preview_candidate` / `ineligible_unbound_preview` diagnostics. Alias collapse requires its own exact-current prepared literal quote/link and the exact validated target payload/link prefix; an ID or payload alone is insufficient. Genuine corroboration/closed aliases retain their existing guards only after current pure preparation rederives their policy and real persisted authority, while an own expired fact may be omitted only when current source/context/clock rederives `expired_without_authority` without real decision/event authority. Existing full-candidate-set eligibility checks for recovery and independent verification, and existing budget limits, remain unchanged. The v3 planner bump invalidates older previews and activation plans; reconstructed provenance alone is not semantic acceptance.

Matching emits `matching_diagnostics[]` with fixed primary-response, primary-validation, recovery and verification codes plus `candidate_count`, `candidate_set_complete`, `missing_original_count` and `unrecoverable_count`. Diagnostic fields contain only fixed codes, counts and completeness flags, never raw results, quotes or identifiers. They provide no matching, decision, event or write authority.

Preview HTML renders `outcomes[]` matching/routing snapshots, not the raw extracted `proposals[]`. It shows retained decisions and a private before/after comparison to an existing canonical action. `/actions/{id}` links require actual store membership; deterministic virtual preview IDs do not establish active actions. Every preview payload records `planner_version`, governed by the pure `source_preview` contract. Developers must bump `SOURCE_PREVIEW_PLANNER_VERSION` whenever source extraction, prompts, matching or applicability semantics change; release SHA changes alone do not invalidate this cache. Only an exact current planner and source hash establishes readiness, virtual matching context or activation eligibility. Missing/old markers and malformed payloads fail closed. API pagination filters the effective stale status before its limit; HTML labels stale results and hides their old outcomes/errors until fresh analysis completes. Failed refresh replaces the old payload with a current failed record and remains retryable. Preview adds no forms or write paths.

In preview mode, per-kind aggregate `pending` counts current pending/failed sources without a current ready or failed preview; `failed` counts current-version failed previews; optional `preview_ready` counts current-version ready previews. Stale planner versions remain queued within the normal preview budget. An analyzed source can refresh only when its exact current hash belongs to a validated incomplete activation selection, current learner context is verified, and its selection preview still exists. Ordinary analyzed history stays excluded. Planner upgrades never invalidate decisions, event ownership or journals. Activation plans and decision snapshots bind the current planner version; apply rejects an old plan before collection, and partial attempts require fresh previews before a new retry plan. The message channel and active-mode source statuses retain their normal queue semantics. Global ready count also joins current, nondeleted source versions. Old snapshots without `preview_ready` leave that informational dimension unknown. A changed offer member with `blocked_by_offer_group`, or an alternative marker without complete stored group membership, has no generic approval control; the UI links the original complete offer/message and leaves explicit rejection available.
