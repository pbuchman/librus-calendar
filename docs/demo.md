# Offline demo and screenshots

[Polski](demo.pl.md) · [README](../README.md)

Run `scripts/ui_fixture.py` with the project's virtual-environment Python and open `http://127.0.0.1:8795`. Use `--port 8796` if needed. The server binds only to loopback, builds fresh temporary SQLite state and deletes it on exit. It supplies literal configuration and an explicit fixed clock, ignoring private/home configuration and account environment overrides. No Librus client, Codex model, Google Calendar connection, systemd sync or external write is initialized.

All teacher names, messages and analysis results are synthetic. The clock is **6 March 2030, 10:15 Europe/Warsaw**. The displayed success, event snapshots, source quotes and provenance are fixtures, not live service evidence. Every screenshot visibly says **“Demo — dane fikcyjne / fictional data”**.

| Screenshot | Source |
| --- | --- |
| `assets/inbox.png` | Actual app inbox, five newly authored fictional messages |
| `assets/message-detail.png` | Actual app detail and synthetic recognized planetarium event |
| `assets/extracurricular-review.png` | Actual app participation-review form for optional robotics |
| `assets/calendar-week.png` | Separate local HTML illustration from the same fixture; **not an app calendar feature or a Google Calendar screenshot** |

The calendar illustration contains the school trip, parent meeting and all-day consent deadline. Robotics remains unapproved and is absent. Approving its proposal only changes temporary SQLite state; the demo's sync callback is a no-op.

## Reproduce the four images

Install Node.js and Playwright in a separate development dependency directory, or use an existing installation. The renderer accepts an explicit module path; it adds no browser dependency to production Python requirements.

```sh
mkdir -p /tmp/librus-demo-browser
npm install --prefix /tmp/librus-demo-browser playwright
/tmp/librus-demo-browser/node_modules/.bin/playwright install chromium
.venv/bin/python scripts/render_demo.py --playwright-module /tmp/librus-demo-browser/node_modules/playwright
```

If using an existing Chrome/Chromium installation, pass `--browser-executable /absolute/path/to/browser` instead of downloading a browser. `--node /absolute/path/to/node` selects a Node runtime. `--output DIRECTORY` overrides the four-image destination, whose default is `docs/assets`. `--qa-output DIRECTORY` selects a separate directory for the mobile screenshot and JSON check summary; by default these remain outside the repository in the temporary directory.

The renderer starts the isolated demo, checks the actual page title, content and banner, captures desktop screens at 1440×1080, approves the extracurricular proposal and verifies its stored state, pauses writes, requests a no-op sync, and checks message navigation at 390×844. It blocks non-loopback browser requests and fails if any are attempted or browser errors occur. It then renders the standalone calendar illustration. Image pixels may vary slightly across browser/font versions; fixture values and the clock are fixed.

Isolation tests:

```sh
.venv/bin/python -m unittest discover -s tests -p test_demo.py -v
```

They verify no home/config discovery or real client initialization, repeatable message/proposal/event state, fixed synchronization timestamps, and exclusion of the unapproved extracurricular event from the illustration. This validates the offline fixture and UI flow; live account/model/calendar compatibility requires separate checks.
