# Keep Librus Calendar simple

Follow [the implementation plan](docs/simple-agenda-plan.pl.md). Implementation is authorized;
first run the read-only planning POC, then replace the old engine after its acceptance checks.

## One planner, one writer

- Read only message, announcement and school-calendar text; no attachment, image, PDF, OCR or external-link content.
- Keep one agenda in the existing SQLite database, runtime, Calendar, app timer and Netdata integration.
- Use one tool-free structured task per changed coherent snapshot: relevant texts, current agenda and saved decisions.
- Measure the complete bounded input before calling; overflow fails visibly, without silent truncation or a bespoke paging engine.
- Replace both old message inference and multi-source matching; retire the rollout switch, retain useful read-only preview.
- Application code assigns stable IDs, validates and persists the plan, then uses the existing durable operation executor.

## Preserve intent and state

- Preserve IDs, ownership, history, approved hours, manual edits, rejections and one-choice offers; never reset the database.
- Check current Calendar state before applying; material changes to user-controlled entries and cancellations require approval.
- Review blocks affected actions only; keep dependent offer choices atomic. Missing sources or plan items never authorize deletion.
- Reconcile uncertain writes read-only before retrying; preview cannot mutate active decisions, queue or Calendar.
- External edits, decisions and policy/planner changes invalidate reuse; own successful writes advance the baseline. Unchanged runs still reconcile and check health.

## Complexity, models and privacy

- Do not add blind model verification, certificates, lexical semantic rules, graph/vector databases, per-candidate calls or dynamic provider retry matrices.
- Delegate implementation with explicit scope and validation to the newest available SOL/high; xhigh needs bounded demonstrated difficulty. Keep runtime explicitly configured as gpt-6.1-sol/high; no ASTRA, max or ultra without a later explicit request.
- Show a failed POC's concrete bad input/output and narrow cause before changing it; use a few meaningful acceptance scenarios.
- Keep real school data, credentials and deployment identifiers private; public examples are fictional. Read the installed Home Dev secrets policy for configuration/deployment. Do not create hourly Codex acceptance automations; keep the app hourly sync.
