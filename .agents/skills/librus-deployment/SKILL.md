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

The offline UI fixture uses temporary fictional state and no external clients. Use a separate development port. Keep real school data and plan output outside Git.

## Existing state upgrade

Keep the existing private database path and configured learner/calendar binding. Stop new old-worker runs, allow any active writer to finish and reconcile unknown outcomes. The new initialization idempotently links existing messages, proposals and owned events into the shared agenda; it preserves IDs, decisions, approved hours, groups and journals. Never reset, copy or restore SQLite, create a fresh replacement target, or run the retired source activation/message reanalysis workflow.

Use the prepared release interpreter with `-I` for status and `agenda-preview` (equivalently `sync --dry-run`). Preview uses the last complete stored collection plus fresh read-only Calendar state; it does not refresh Librus. It records source collection time and mutates only isolated preview rows. A failed or stale preview grants no write authority. Normal sync freshly collects and rechecks sources, decisions and Calendar before materializing a plan. Inspect preservation, review and unknown states before resuming writes. `prune` remains preview-only unless `--apply` is authorized by the retention policy; keep all journal and ownership evidence.

## Activate

For normal releases:

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA
```

For the explicitly authorized initial migration, preserve and validate the existing private state/configuration first, then use:

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
