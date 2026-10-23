# Incremental agenda input and checkpoint

The planner keeps the complete agenda, Calendar observations, saved decisions and
one-choice offers in every request. The source catalog contains every known source
ID, current content hash, deletion, retention and body-availability state. Full
source bodies are supplied for new/changed versions and for affected human or
Calendar changes. Historical decisions alone do not resend bodies. Context-only
bodies and catalog-only rows cannot authorize a new automatic create. Previously
processed ignored history is not retrospectively analyzed. Every selected source
ID must be referenced by at least one plan item before any checkpoint can advance.
A grouped ignore can acknowledge multiple irrelevant/unavailable sources without
an existing action. Catalog-only IDs need no acknowledgment.

The planner first establishes an actual pupil activity, task or duty, then checks
its applicability. Operational availability alone is ignored even when recipients
or participation are unknown. For example, access hours for viewing already-written
contest papers do not announce contest participation, signup or a pupil duty.
An actual activity with unclear applicability still requires review, while a clear
optional classroom task remains eligible for automatic creation. This is a planner
instruction, not a lexical rule in application code; offline prompt/contract tests
do not prove model interpretation. A bounded read-only live planning check must
verify the distinction before rollout acceptance.

`app.agenda_incremental.assemble_incremental_snapshot(database, calendar_events,
current=..., owner_email=...)` is a pure read helper. It accepts an existing SQLite
read-only connection; never instantiate AppStore for a production planning POC.
Supply a fresh managed Calendar map; failed reads must abort instead of becoming
`None`. It returns `(snapshot, contract)`. The snapshot is the model input; the
contract stays local. Use `agenda_prompt(snapshot)` and the unchanged 160000-byte
cap. Nothing truncates or pages whole agenda state. `enforce_budget=False` is only
for inspection, never for invoking a provider.

`contract.local_state_hash` is a CAS digest of complete local source content,
agenda/authority, unsettled operations and checkpoint state. It is not constrained
by the prompt byte budget. `snapshot.snapshot_hash` echoes that CAS token for the
existing plan schema. `contract.request_hash` is SHA-256 of the exact UTF-8 prompt;
`input_bytes` measures those same bytes. Source count denotes supplied changes or
context; catalog count denotes all known sources.

The additive `agenda_processed_sources` ledger stores each source's exact content
hash plus deleted, retained and body-availability state, linked to the applied plan.
Retention/deletion are changes even when content_hash stays identical. The ledger
contains no private body. Missing ledger entries can bootstrap only analyzed, non-retained,
non-deleted rows whose exact analyzed_at matches a compatible applied
whole-snapshot plan (`simple-agenda-v4-shared-learner`, SOL/high metadata). Pending,
failed, retained, deleted, preview and failed-plan state do not prove processing.
Pruning changes retention/body availability without updating analyzed_at or
content_hash, so even a retained row with a compatible old timestamp is supplied
once as an unavailable change and then checkpointed by its exact current version. In
particular, an analyzed deletion is presented once, then checkpointed explicitly.

Integration must compare complete local CAS under BEGIN IMMEDIATE and commit
local plan materialization, only explicitly matching processed versions, bootstrap
versions and `agenda_authority_baseline` together. It must update only corresponding
messages whose current mirrored payload/version still matches. Preview, failure,
stale plan, or model omission must never blanket-mark messages or advance a
checkpoint. Source absence never authorizes cancellation. Unsettled writes remain
with the existing durable executor and read-only reconciliation.

A successful own write advances its existing baseline and the matching authority
baseline so the next unchanged run performs zero inference. A new source version,
human decision, external Calendar edit, policy/planner or model change invalidates
reuse. Historical source payloads omitted from the request still participate in
complete local CAS. The active planner uses this contract and preserves the existing writer.
`full_snapshot_hash` is the content-reuse baseline: it excludes day/observation
clocks while full CAS and actual requests retain the current as_of_date. Reuse
also requires an empty selected-source set, so missing ledger rows cannot hide
unprocessed versions. The collector must change content_hash when semantic
source content changes; all stored payloads still participate in complete CAS.
