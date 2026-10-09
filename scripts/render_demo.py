#!/usr/bin/env python3
"""Reproduce the four fictional public screenshots using loopback-only demo UI."""
import argparse
from datetime import date, datetime, timedelta
import html
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ui_fixture import CALENDAR_EVENTS, DEMO_LABEL


def calendar_illustration():
    """Illustration only: this is neither an app calendar route nor Google UI."""
    monday = date(2030, 3, 4)
    days = ['Pon / Mon', 'Wt / Tue', 'Śr / Wed', 'Czw / Thu', 'Pt / Fri', 'Sob / Sat', 'Ndz / Sun']
    headings = ''.join(f'<div>{name}<strong>{(monday + timedelta(days=i)).day}</strong></div>' for i, name in enumerate(days))
    events = ''
    all_day = ''
    for event in CALENDAR_EVENTS:
        day_index = (date.fromisoformat(event['start'][:10]) - monday).days
        title = html.escape('[Librus] ' + event['title'])
        if event['all_day']:
            due = datetime.fromisoformat(event['due_at']).strftime('%H:%M')
            all_day += f'<div class="deadline" style="grid-column:{day_index+2}">{title} — do {due}</div>'
        else:
            start, end = (datetime.fromisoformat(event[key]) for key in ('start', 'end'))
            top = (start.hour + start.minute/60 - 8)*42
            height = (end-start).total_seconds()/3600*42
            events += f'<article style="left:calc(64px + (100% - 64px)*{day_index}/7 + 5px);width:calc((100% - 64px)/7 - 10px);top:{top}px;height:{max(height,55)}px"><b>{title}</b><small>{start:%H:%M}–{end:%H:%M}</small></article>'
    hours = ''.join(f'<div class="hour" style="top:{(hour-8)*42}px"><span>{hour:02}:00</span></div>' for hour in range(8,20))
    return f'''<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Demo calendar illustration</title><style>
+*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f5;color:#243831;font:15px system-ui,sans-serif}}main{{max-width:1320px;margin:auto;padding:26px 36px}}.banner{{background:#e7eee8;border:1px solid #bdcec0;border-radius:8px;padding:12px 18px;font-weight:700}}header{{display:flex;justify-content:space-between;align-items:center;padding:22px 0 18px}}h1{{margin:0;font-size:30px;letter-spacing:-1px}}header p{{margin:8px 0;color:#61716a}}.pill{{background:white;padding:10px 16px;border-radius:9px;border:1px solid #d8e0da}}.calendar{{background:white;border:1px solid #d8e0da;border-radius:12px;overflow:hidden}}.headings{{display:grid;grid-template-columns:64px repeat(7,1fr);text-align:center;padding:14px 0;color:#61716a;border-bottom:1px solid #dce4df}}.headings strong{{display:block;color:#243831;font-size:24px;margin-top:5px}}.all-day{{display:grid;grid-template-columns:64px repeat(7,1fr);min-height:55px;align-items:center;border-bottom:1px solid #dce4df}}.all-day>span{{font-size:10px;text-align:center;color:#65786e}}.deadline{{font-size:11px;background:#f3e9bd;border-radius:5px;margin:5px;padding:7px;color:#604b17}}.grid{{height:504px;position:relative;background:repeating-linear-gradient(to right,transparent 0,transparent calc((100% - 64px)/7 - 1px),#edf0ee calc((100% - 64px)/7 - 1px),#edf0ee calc((100% - 64px)/7));background-position:64px 0}}.hour{{position:absolute;width:100%;border-top:1px solid #edf0ee}}.hour span{{position:absolute;left:12px;top:7px;font-size:11px;color:#6d7b74}}article{{position:absolute;background:#dae9e0;border-left:3px solid #4d7660;border-radius:5px;padding:8px;font-size:12px;overflow:hidden}}article small{{display:block;margin-top:7px;color:#4e6b59}}footer{{padding:18px 0;color:#607269;font-size:13px}}footer strong{{color:#3d5547}}
+</style><main><div class="banner">{DEMO_LABEL}</div><header><div><h1>Kalendarz tygodniowy / Week view</h1><p>4–10 marca / March 2030 · Europe/Warsaw</p></div><span class="pill">calendar@example.test</span></header><section class="calendar"><div class="headings"><div></div>{headings}</div><div class="all-day"><span>cały dzień<br>all-day</span>{all_day}</div><div class="grid">{hours}{events}</div></section><footer><strong>Lokalna ilustracja / Local illustration.</strong> Nie jest funkcją kalendarza aplikacji ani zrzutem Google Calendar.<br>Not an app calendar feature or a Google Calendar screenshot. Optional robotics is awaiting approval and is absent here.</footer></main></html>'''.replace('\n+', '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'docs/assets')
    parser.add_argument('--qa-output', type=Path, default=Path(tempfile.gettempdir())/'librus-demo-qa')
    parser.add_argument('--node', default='node')
    parser.add_argument('--playwright-module', default='playwright')
    parser.add_argument('--browser-executable', default='', help='Optional existing Chromium/Chrome executable')
    parser.add_argument('--port', type=int, default=8796)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.qa_output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='librus-demo-render-') as scratch:
        calendar = Path(scratch)/'calendar.html'
        calendar.write_text(calendar_illustration(), encoding='utf-8')
        with subprocess.Popen([sys.executable, str(Path(__file__).with_name('ui_fixture.py')), '--port', str(args.port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as server:
            try:
                url = f'http://127.0.0.1:{args.port}'
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError('Demo server stopped: ' + server.stderr.read().decode())
                    try:
                        with urlopen(url, timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError('Demo server did not become ready.')
                subprocess.run([args.node, str(Path(__file__).with_name('render_demo.cjs')), url,
                                str(args.output.resolve()), str(calendar), str(args.qa_output.resolve()),
                                args.playwright_module, args.browser_executable], check=True)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
    print('Four synthetic screenshots generated; QA evidence: ' + str(args.qa_output))


if __name__ == '__main__':
    main()
