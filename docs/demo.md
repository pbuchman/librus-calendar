# Offline demo and screenshots

[Polski](demo.pl.md) · [README](../README.md)

Run `scripts/ui_fixture.py` with the project's virtual-environment Python and open `http://127.0.0.1:8795`. Use `--port 8796` if needed. The server binds only to loopback, builds fresh temporary SQLite state and deletes it on exit. It supplies literal configuration and an explicit fixed clock, ignoring private/home configuration and account environment overrides. No Librus client, Codex model, Google Calendar connection, systemd sync or external write is initialized.

All teacher names, messages and analysis results are synthetic. The clock is **6 March 2030, 10:15 Europe/Warsaw**. The displayed success, event snapshots, source quotes and provenance are fixtures, not live service evidence. Every screenshot visibly says **“Demo — dane fikcyjne / fictional data”**.

| Screenshot | Source |
| --- | --- |
| `assets/inbox.png` | Actual app inbox, seven newly authored fictional messages |
| `assets/message-detail.png` | Actual app detail and synthetic recognized planetarium event |
| `assets/extracurricular-review.png` | Actual app choice card for one optional first-aid offer, including a past disabled date |
| `assets/calendar-week.png` | Separate local HTML illustration from the same fixture; **not an app calendar feature or a Google Calendar screenshot** |

The calendar illustration contains the school trip, parent meeting and all-day consent deadline. Robotics remains unapproved and is absent. Approving its proposal only changes temporary SQLite state; the demo's sync callback is a no-op.

## Try one offer with alternative dates

Open `/review` or `/messages/demo-training-choice`. The fictional first-aid offer has three dates, including one past date that remains visible and disabled. None is selected by default. Select one future date and use **Dodaj wybrany termin** to queue only that choice; the card then shows the chosen date and closes all other options. **Odrzuć całą ofertę** rejects every option without needing a date selection. Completed offers leave the review queue; the selected or rejected state remains visible in source-message detail. The navigation badge counts one open offer rather than each alternative. If the chosen date later needs review after a write conflict, only that chosen date returns to the queue with guarded approval/rejection controls; the other dates stay closed. Ambiguous replacement proposals after an earlier offer decision show source context and allow rejection, with approval blocked. Refreshing preserves the decision for that demo process; restarting resets all fictional state.

The separate message `/messages/demo-training-series` contains two meetings of a recurring theatre course. Both remain ordinary independent proposals; a series is not a choice of alternative dates. Existing optional robotics remains an ordinary participation approval. The expanded fixture has eight newly authored fictional messages, including a retained rejected club invitation. The existing screenshots were captured with the earlier seven-message fixture.

The read-only `/api/proposals` remains available with its existing limit. `/api/proposal-groups` supplies complete offer membership independently of that limit. Group decisions use `POST /api/proposal-groups/{id}/decision` with `decision=select|reject`, `proposal_id` for selection, `expected_revision`, the same-origin header and the CSRF cookie/form token. Individual proposal decisions cannot bypass the one-choice rule. Source changes, a stale revision, past dates or a conflicting closed decision are rejected by the store.

## Reproduce the four images

Install Node.js and Playwright in a separate development dependency directory, or use an existing installation. The renderer accepts an explicit module path; it adds no browser dependency to production Python requirements.

```sh
mkdir -p /tmp/librus-demo-browser
npm install --prefix /tmp/librus-demo-browser playwright
/tmp/librus-demo-browser/node_modules/.bin/playwright install chromium
.venv/bin/python scripts/render_demo.py --playwright-module /tmp/librus-demo-browser/node_modules/playwright
```

If using an existing Chrome/Chromium installation, pass `--browser-executable /absolute/path/to/browser` instead of downloading a browser. `--node /absolute/path/to/node` selects a Node runtime. `--output DIRECTORY` overrides the four-image destination, whose default is `docs/assets`. `--qa-output DIRECTORY` selects a separate directory for the mobile screenshot and JSON check summary; by default these remain outside the repository in the temporary directory.

The renderer starts the isolated demo, checks the actual page title, content and banner, captures desktop screens at 1440×1080, approves the standalone robotics proposal, checks the complete alternative offer with no default and a past disabled date, selects one future option and verifies the closed state in message detail, pauses writes, requests a no-op sync, and checks message navigation at 390×844. It blocks non-loopback browser requests and fails if any are attempted or browser errors occur. It then renders the standalone calendar illustration. Image pixels may vary slightly across browser/font versions; fixture values and the clock are fixed.

Isolation tests:

```sh
.venv/bin/python -m unittest discover -s tests -p test_demo.py -v
```

They verify no home/config discovery or real client initialization, repeatable message/proposal/event state, fixed synchronization timestamps, and exclusion of the unapproved extracurricular event from the illustration. This validates the offline fixture and UI flow; live account/model/calendar compatibility requires separate checks.

## Source and preview views

Open `/sources` to filter fictional messages, announcements and school-calendar entries and follow a source to its canonical action. The action detail lists supporting sources, evidence and certainty. `/preview` presents separate synthetic analyses with no approval or activation control. Attachments are labeled **załącznik nieanalizowany** and never fetched. These seeded source results are demonstrations only; existing four screenshots predate the source views and do not establish their deployed state.

Preview shows planned matching outcomes, retained human decisions and differences from existing actions. A fictional repeated trip links to its existing action; a repeated club invitation retains an actual seeded rejection. Virtual preview actions have no active-action link. A ready preview of the current source version is labeled separately from active analysis; stale versions do not override current source state.

These fictional views do not test legacy-evidence recovery, independent unique matching or the interrupted-activation guard. Those mechanisms need separate checks; a saved preview and corroborating source without verified recipient applicability grant no Calendar write authority.
