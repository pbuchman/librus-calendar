# Architecture, safeguards and privacy

[Polski](architecture.pl.md) · [README](../README.md) · [Setup](setup.md)

The system reads message lists and bodies directly from Librus Synergia over HTTPS. It does not consume Gmail notifications. Local SQLite state separates discovery, full-message fetching, AI analysis, proposed actions, durable operations, verified calendar snapshots and technical health history.

`app/librus_client.py` handles the narrow message/authentication flow; `app/sync.py` coordinates the locked worker; `app/codex_runtime.py` invokes the authenticated Codex CLI for structured analysis and selected calendar tools. `app/core.py` enforces write policy outside the model. The FastAPI/Jinja inbox in `app/web.py` shows cached messages, recognized actions, saved events and the review queue. The app does not provide a calendar week grid; the documentation's grid is explicitly a separate synthetic illustration.

## Decisions and write safety

Clear future school actions with source evidence may be automatic when writes are enabled. School preparation tasks, classroom activities and deadlines are assessed per action. Optional extracurricular creates require participation approval even when their date is clear; unknown scope, ambiguous dates, overlapping/duplicate starts, unsafe event state and cancellations need review. Ordinary analysis cannot approve a cancellation or infer that a child registered for an optional activity.

Explicitly alternative dates for one optional offer are stored as one durable group tied to the source message. The review queue and message detail show the full group independently of the legacy proposal-list limit, with no date selected by default. Past dates remain visible but cannot be chosen. Selecting a future option atomically queues that option and closes its siblings; rejecting closes the entire offer. A revision and source check prevent stale decisions; individual proposal actions cannot bypass the group. A closed group retains its selected or rejected state. Recurring series and separate obligations are not grouped merely because they share a message or title.

Dates use Europe/Warsaw and distinguish timed events from all-day reminders/deadlines. Exact deadline times are retained in the title/description while the calendar representation remains one all-day entry. The app rejects invalid or ambiguous DST times. A timed event lacking an end may use a clearly disclosed one-hour default.

Each new title receives one `[Librus]` prefix. The description stores a source quote, message reference, ownership marker and the operation's frozen synchronization-start timestamp. The timestamp is preparation time, not write completion. These source references also become private data in the destination calendar.

The worker verifies the exact configured calendar's write access, restricts changes to app-owned entries, excludes external attendees and sends no invitations. Existing entry fingerprints detect manual calendar changes. A stable operation marker supports idempotency; unknown external outcomes undergo read-only reconciliation instead of blind retries. Cancellations require explicit confirmation and preserve tombstones. Pausing writes or dry-run prevents calendar mutations; it does not make fetching/analysis offline.

## Provenance and health

Message analysis stores the actual runtime-reported model, reasoning effort, prompt version and decision reason. Missing legacy provenance stays unknown; merely opening the database does not invent it or backfill history. A synthetic demo uses visibly labeled synthetic provenance and invokes no model.

Full sync success is distinct from last attempt, dry-run, paused state and partial progress. The hourly worker persists sanitized technical stages, durations and error codes; a full success requires no automatic pending work or unknown outcomes. Proposals awaiting parent review do not necessarily block technical sync success. The read-only Netdata export contains aggregate health metadata, not message contents; a green health indicator is not proof that the model understood a message correctly.

Analysis and every calendar tool use the configured model/effort, defaulting to `gpt-6.1-sol`/`high`. The initial calendar-list access probe has at most one retry after five seconds, only for a structured unexpected completed tool-call count (zero or multiple) with the exact expected read call and arguments. Unfinished calls and other failures do not retry; create/update/delete calls are outside this retry policy. Uncertain writes continue to require read-only reconciliation.

Each persisted sync probe attempt contains only run/attempt identity, timestamps, model/effort, validated CLI version, result, process/error codes, tool-call/protocol counts and expected-call validation flags; technical history expires after 90 days. It excludes calendar identifiers, tool arguments/results, prompts, message data and raw errors. The panel shows last-run probe result, attempts and recovery. The additive monitoring fields are optional: absent/null evidence means no data. Netdata's absolute rolling 24-hour retry/recovery chart is informational; successful recovery does not create a sync warning, and existing alarms remain unchanged.

Automatic database backups are intentionally disabled. Deployment does not create a database copy; forward recovery preserves the current operation journal. Both primary state and backups contain private school data. Audit evidence and tombstones remain local; there is no automatic history-wide reanalysis or irreversible blanket calendar repair.

## Private data boundaries

| Data | Where it goes in a real instance |
| --- | --- |
| Librus login/password | Protected local credentials file and Librus authentication endpoints |
| Message subject/body/sender | Private SQLite; relevant supplied fields go to the configured AI service for analysis |
| Relevant cached events | Private SQLite and selected AI/calendar verification context |
| Selected event title/details/source quote | Configured Google Calendar through the connected Codex app |
| Technical aggregate health | Local monitoring JSON and optional Netdata collector |
| SQLite backups | Private host backup files; protect storage and any offsite copies |

Production credentials, tokens, SQLite state, backups, logs, runtime evidence, real messages and private screenshots do not belong in this repository. Examples use reserved synthetic domains. The public screenshots contain only newly authored fictional data. The demo reads no existing home configuration, binds to loopback and uses temporary state.

The UI trusts a local Tailscale Serve proxy, an exact hostname and one configured login identity. It is not a general public multi-user service. A public reverse proxy that supplies forged identity headers invalidates that trust boundary. Keep access private, configure ACLs and protect the host.

## Project status

This is an unofficial personal proof of concept, independently maintained and unaffiliated with Librus, Google or OpenAI. Librus endpoints are undocumented and may change; the runtime requires account-specific Codex models and calendar tools. The public source is a configurable edition, not a backup or exact copy of private production deployment state.

Attachments are flagged for inspection in Librus; their contents are not analyzed. The full timetable, grades and school workflows beyond supported source types remain outside this integration. Automated tests and synthetic screenshots cover defined behavior; they do not establish live service compatibility, deployment success, perfect analysis or calendar accuracy. The detailed [application contract](../app/CONTRACT.md) documents state and maintenance rules.

## Review before public sharing

Run `python scripts/check_public_tree.py --working-tree --all-history` before publication. It checks the working/staged tree and all locally reachable Git history and metadata; ignored untracked runtime files are excluded, while tracked/history content is still checked. An optional external JSON file `{"patterns": ["literal private pattern"]}` can be passed with `--denylist` or `PUBLIC_TREE_DENYLIST`. Keep that private denylist outside the repository. The scanner is not exhaustive proof of secret or personal-data absence: manually review source, history, image pixels and metadata as well.

## Sources and canonical actions

Messages, announcements and school-calendar entries retain separate source identities, content hashes, learner scope and collection health. Substitutions are informational. Every action can retain links to multiple supporting sources, including the literal quote, source version and relation. The panel displays result/reason and certainty without inferring certainty from a successful fetch. A verified match links or enriches one action; incomplete evidence and ambiguous matches remain explicit review/incomplete states. Human approvals, edits, rejections, alternative-offer choices and owned-event identities remain authoritative across source changes and full replay.

A `school_event` may carry a separate `served_class_calendar` audience envelope minted by the collector. It binds the served-view contract, the verified selected learner and class, and the observed source content. The collector checks exact successful authenticated routes, complete bounded month views, supported controls, restoration of the temporary month selection and unchanged learner context. Missing or conflicting checks fail closed. A route name, visibility alone or model output cannot mint this proof; ingestion validates it and the source hash includes it.

Only an ordinary school action with scope `class`, the exact verified class and `condition=none` can use this envelope to establish class applicability without a class phrase in the body. Scopes `all`, `learner` and `unknown` cannot derive applicability from it. Literal action identity, source quotation and grounded date evidence remain required; no audience sentence is invented. The application computes the recipient basis separately and revalidates it before writes, so a changed source or context requires fresh analysis. Dispatch also requires an activation receipt bound to the current class context: both cached work from an old class and fresh new-class proof under an old receipt stay deferred until fresh validated activation, without rewriting the saved operation intent. The envelope does not establish an exclusive account role or authorize extracurricular participation. Role restrictions remain informational, unknown or conditional participation requires review, conflicting recipient identifiers refuse authority, and human decisions remain authoritative. Messages, announcements and substitutions do not gain this class-calendar exception. Attachments remain metadata only.

When direct matching cannot establish a link, a separate tool-free verifier checks the complete bounded inventory of plausible actions, including candidates with missing evidence. An incomplete or overflowing inventory stays in review; removing an inconvenient candidate cannot establish uniqueness. The verifier receives the new literal quote, original saved action quotes and actual canonical fields, without the previous matcher's rationale or expanded neighboring text. For a structured `school_event` only, an independently observed title may establish purpose alongside literal body action/date evidence, verified recipient applicability and the observed calendar date. A generated title or date similarity alone is insufficient. The verifier must identify exactly one action with the same purpose, occasion and recipient.

Original saved quotations remain unchanged. If a legacy action lacks one, a separate recovery call may derive a bounded literal fact only from its unique, still-current own origin message, with exact matching source and proposal hashes. That fact is labeled reconstructed evidence and independently checked against the canonical action before semantic verification. The entire body is never substituted for an action quotation; no historical quotation, human decision or source version is fabricated or repaired. Missing, changed or ambiguous origin evidence keeps the action in review.

An incoming source with unverified recipient applicability may only corroborate an already human-confirmed occurrence with one verified app-owned event. It must be observably older than the exact current origin and independently identify that occurrence uniquely. Its link is labeled as having no recipient or write authority: it cannot create, update or cancel an action, change a decision or event, or supply proof for a future match. Otherwise it remains in review. An expired ordinary school create fact may be ignored without recipient authority only when its literal, dated fact is valid at the fixed evaluation clock; an unknown future recipient still requires review. Its diagnostic link grants no identity, recipient, chronology, retained-state fallback or operation authority. The same boundary applies to corroboration with an unverified recipient.

Extraction, matching, legacy-evidence recovery and verification share the preview phase's 31-call budget and deadline. Exact quotes, source hashes and the final unchanged-version check protect every accepted result.

New-source mode defaults to `off`. `preview` stores analyses in a separate queue, with no Calendar operation or active decision path. First activation and an explicit reactivation attempt require fresh collection, reanalysis and revalidation; stale preview results cannot authorize work. A configuration mode change alone does not invalidate a valid prior receipt. A full active synchronization requires completeness for every required source, while preview health is separate evidence. Collection failures cannot be disguised as empty successful source lists. Unknown legacy source-health fields stay null. Starting an activation apply durably records an incomplete-attempt marker that suspends an older receipt and blocks shared analysis, queue and claim after failure or interruption, including after restart and despite an older receipt. A fresh retry plan covers the failed attempt's exact source selection and fresh analysis. All receipt fields and removal of the marker commit atomically.

Source previews and source-analysis audits can contain narrative content and follow the 30-day content-retention policy. Only purely technical sync/probe history uses the separate 90-day window; source audit metadata has no 90-day exemption. Minimal retained decisions, ownership, future facts and unresolved write evidence follow the preservation rules.

Source details remain private UI data. The read-only Netdata path selects only fixed source kinds, mode, last successful fetch, numeric queue/error/link counts and finite technical result/error codes. It never exports learner/source/action identifiers, names, titles, text, quotes or raw errors. It adds informational source charts without changing existing alarms. Attachments remain metadata only, visibly labeled **załącznik nieanalizowany**; this release performs no image, OCR, PDF or attachment-content requests to external services.
