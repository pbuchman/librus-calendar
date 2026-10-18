---
name: librus-deployment
description: Prepare, deploy, verify, and recover the Linux Librus Calendar application using its canonical SHA-addressed packaged release scripts, private external configuration, systemd user units, and optional Netdata monitoring.
---

# Librus Calendar deployment

Use this skill for Librus releases, initial migration from the known legacy user units, live release verification, and compatible code recovery. The canonical implementation is `scripts/deploy.py` in the Librus repository; invoke it instead of duplicating installer shell commands. `docs/setup.md` supplies account and service configuration details.

## Authority and privacy

Follow the user's explicit deployment authorization; do not invent an extra approval gate. Confirm the intended source SHA and migration policy within that authorization. Never publish credentials, account values, message bodies, database files, logs or private evidence. Public examples use `$HOME`, reserved domains and generic user names. Keep configuration under `$HOME/.config/librus-calendar`, state under `$HOME/.local/share/librus-calendar`, and private development evaluation outside Git.

Production runs an installed wheel in `$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv`; `current` points to the selected release. Always pass `-I` to installed release/current Python runtime commands, including manual status, sync and administrative operations, so inherited `PYTHONPATH`, the current directory and user site cannot select checkout code. Service units already enforce `-I`. Package resources include templates, CSS and runtime schemas. Do not modify a release in place.

Backups are disabled intentionally (`backup_enabled=false`, `LIBRUS_BACKUP_ENABLED=0`). Neither prepare, installer, activation nor recovery creates a database backup. Do not copy/restore SQLite during deployment or code rollback: that would discard external-write evidence. Retain or delete old state only according to the explicitly authorized migration policy.

## Prepare and test

Use a clean Git checkout at the reviewed exact 40-character SHA, reachable from locally fetched `origin/main`. Fetch in the normal authorized source workflow before preparation; the script does not fetch or publish. Use a Python 3.12+ development environment with `requirements.txt` and the pinned build tools from `pyproject.toml` installed.

Run from the Librus repository:

```sh
.venv/bin/python scripts/deploy.py prepare --sha FULL_SHA
```

Preparation validates the source, runs full offline tests in an isolated temporary home, scans source and complete reachable history, builds a wheel, installs dependencies into a separate release, verifies imports/resources outside the checkout, and records artifact/application hashes and the wheel's resource inventory. Each installed release is validated against its own hash-verified wheel and recorded application files; older manifests derive their resource inventory from their wheel. Preparation, including reuse, and target activation also require the invoking source's full resource list. All preparation completes before services stop. An existing verified release is reused; no service commands or production database access occur during preparation.

Never run real-message evaluation against production state or expose it on the production port. `scripts/evaluate_analysis.py --config PRIVATE_EVAL_CONFIG PRIVATE_CASES_JSON` uses an explicitly read-only runtime and does not discover the production configuration. The offline UI fixture uses temporary synthetic state; use a separate development port when production is running.

## Initial state migration

Follow the reviewed migration policy and stop new old-worker runs, allowing any active writer to finish. Use the prepared release Python before activation, explicitly selecting the new private state:

```sh
release_python="$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv/bin/python"
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" pause-writes
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" migrate-decisions --from "$HOME/.local/share/librus-calendar/state.sqlite3"
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" rebuild --days 30
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" prune --days 30
```

The migration source and target must differ. `migrate-decisions` directly reads the old DB; it copies no message history or database file. It keeps minimal human decisions, exact owned event IDs/markers, cancellation tombstones and unresolved journal evidence. Bound calendar/owner and account identities must match. For an explicitly authorized unbound legacy source only, append `--allow-legacy-binding`; this requires a read-only Calendar owner probe and every exact saved event calendar to match.

`rebuild` permits calendar reads only, scans all Librus pages in the 1–30 day window, retries failed cached work, and verifies discovered and transferred exact event IDs. It changes only target local state and invokes analysis, never queues/executes a calendar mutation. Investigate partial/review outcomes before acceptance. Keep writes paused, run `sync --dry-run` with the prepared release interpreter, and inspect pending proposals and review items before resuming writes. An automatic update or cancellation of an event governed by prior human approval, edit or rejection requires fresh approval of that change, including across source messages after reconstruction; cancellation also requires explicit confirmation.

`prune` previews by default; use `--apply` only within the approved retention policy. It clears old message/analysis content and unnecessary snapshot payload but retains minimal ownership, event IDs, decisions, conflict/tombstone and every durable operation journal record. Unknown/inflight intent is preserved in full. In-place secure deletion/VACUUM makes no backup and no calendar write. Retain/delete the old source according to the explicit acceptance policy, never through an automatic installer copy/delete. The detailed contract is in `docs/setup.md` and `app/CONTRACT.md`.

## Activate

For normal releases:

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA
```

For the explicitly authorized initial migration, prepare the private target state/configuration first, then use:

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA --migrate-legacy --no-sync
```

Only exact known legacy unit contents may be replaced. Arbitrary different units and unsafe symlinks are refused before writes. Activation stops the hourly timer and refuses to switch while a sync worker is running; it never kills a calendar writer. Retry after completion. Missing units on a fresh install are handled. `--no-sync` starts web and monitor only; enable the sync timer after the explicitly planned first-run acceptance. Existing secrets remain untouched. Legacy backup units are disabled and removed only by explicit migration activation.

`bash scripts/install_linux.sh --sha FULL_SHA` installs units only; it does not activate the release or start services. Production remains on loopback port 8795 and its existing private Tailscale Serve route on 8445. Preserve all other Serve routes and never enable Funnel.

The optional root Netdata installer is `deploy/install-librus-monitor.sh APP_USER`; append `--migrate-legacy` only for explicitly authorized replacement of known prior collector files. It does not restart Netdata. Collector updates may require a controlled Netdata/Python worker restart; health-rule reload alone does not reload Python code. If maintenance notifications must be muted, first save existing silence rules, apply a bounded Librus-only mute and guaranteed cleanup, then restore exactly those prior rules. Never blindly clear somebody else's silence configuration.

## Verify and recover

```sh
.venv/bin/python scripts/deploy.py verify --no-sync
systemctl --user enable --now librus-sync.timer
.venv/bin/python scripts/deploy.py verify
```

Use `--no-sync` only during the planned staged migration. Verify the selected release SHA and hashes, installed app/resources, web and monitor units, the configured read-only database aggregates, owner-restricted Tailscale access, hourly Europe/Warsaw timer, minute monitor timer, and Netdata collector freshness. The monitor's nonsensitive `monitor.env` must specify the same state path as production config because its sandbox cannot read private config/credentials. It must disable backup policy explicitly. Disabled backup alarms are intentional; sync, web, database and collector faults remain active.

Prefer forward recovery. Stop new scheduled runs, let an active writer finish, keep SQLite and its operation journal intact, repair code/configuration, prepare a reviewed new SHA, and activate/verify it. A prior packaged release can be activated only after confirming it supports the current database schema and journal. Check out that reviewed prior SHA cleanly, reachable from `origin/main`, and invoke the same `activate --sha PREVIOUS_FULL_SHA`; do not restore an older database. Keep writes paused until unknown outcomes are reconciled. No extra user approval is needed for steps already authorized.
