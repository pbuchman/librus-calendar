# Architecture, safeguards and privacy

[Polski](architecture.pl.md) · [README](../README.md) · [Setup](setup.md)

The system reads message lists and bodies directly from Librus Synergia over HTTPS. It does not consume Gmail notifications. Local SQLite state separates discovery, full-message fetching, AI analysis, proposed actions, durable operations, verified calendar snapshots and technical health history.

`app/librus_client.py` handles the narrow message/authentication flow; `app/sync.py` coordinates the locked worker; `app/codex_runtime.py` invokes the authenticated Codex CLI for structured analysis and selected calendar tools. `app/core.py` enforces write policy outside the model. The FastAPI/Jinja inbox in `app/web.py` shows cached messages, recognized actions, saved events and the review queue. The app does not provide a calendar week grid; the documentation's grid is explicitly a separate synthetic illustration.

## Decisions and write safety

Clear future school actions with source evidence may be automatic when writes are enabled. School preparation tasks, classroom activities and deadlines are assessed per action. Optional extracurricular creates require participation approval even when their date is clear; unknown scope, ambiguous dates, overlapping/duplicate starts, unsafe event state and cancellations need review. Ordinary analysis cannot approve a cancellation or infer that a child registered for an optional activity.

Dates use Europe/Warsaw and distinguish timed events from all-day reminders/deadlines. Exact deadline times are retained in the title/description while the calendar representation remains one all-day entry. The app rejects invalid or ambiguous DST times. A timed event lacking an end may use a clearly disclosed one-hour default.

Each new title receives one `[Librus]` prefix. The description stores a source quote, message reference, ownership marker and the operation's frozen synchronization-start timestamp. The timestamp is preparation time, not write completion. These source references also become private data in the destination calendar.

The worker verifies the exact configured calendar's write access, restricts changes to app-owned entries, excludes external attendees and sends no invitations. Existing entry fingerprints detect manual calendar changes. A stable operation marker supports idempotency; unknown external outcomes undergo read-only reconciliation instead of blind retries. Cancellations require explicit confirmation and preserve tombstones. Pausing writes or dry-run prevents calendar mutations; it does not make fetching/analysis offline.

## Provenance and health

Message analysis stores the actual runtime-reported model, reasoning effort, prompt version and decision reason. Missing legacy provenance stays unknown; merely opening the database does not invent it or backfill history. A synthetic demo uses visibly labeled synthetic provenance and invokes no model.

Full sync success is distinct from last attempt, dry-run, paused state and partial progress. The hourly worker persists sanitized technical stages, durations and error codes; a full success requires no automatic pending work or unknown outcomes. Proposals awaiting parent review do not necessarily block technical sync success. The read-only Netdata export contains aggregate health metadata, not message contents; a green health indicator is not proof that the model understood a message correctly.

Backups use SQLite's backup API, integrity checks and restricted permissions, with the newest seven default copies retained. Both primary state and backups contain private school data. Audit evidence and tombstones remain local; there is no automatic history-wide reanalysis or irreversible blanket calendar repair.

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

Attachments are flagged for inspection in Librus; their contents are not analyzed. Timetables, grades and every school workflow are outside this narrow message integration. Automated tests and synthetic screenshots cover defined behavior; they do not establish live service compatibility, deployment success, perfect analysis or calendar accuracy. The detailed [application contract](../app/CONTRACT.md) documents state and maintenance rules.

## Review before public sharing

Run `python scripts/check_public_tree.py --working-tree --all-history` before publication. It checks the working/staged tree and all locally reachable Git history and metadata; ignored untracked runtime files are excluded, while tracked/history content is still checked. An optional external JSON file `{"patterns": ["literal private pattern"]}` can be passed with `--denylist` or `PUBLIC_TREE_DENYLIST`. Keep that private denylist outside the repository. The scanner is not exhaustive proof of secret or personal-data absence: manually review source, history, image pixels and metadata as well.
