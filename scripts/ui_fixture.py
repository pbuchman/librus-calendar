#!/usr/bin/env python3
"""Deterministic, synthetic UI demo. No Librus, Codex or Calendar clients."""
from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import AppConfig
from app.core import AppStore
from app.web import create_app

DEMO_NOW = datetime(2030, 3, 6, 10, 15, tzinfo=ZoneInfo('Europe/Warsaw'))
DEMO_STAMP = DEMO_NOW.isoformat()
DEMO_LABEL = 'Demo — dane fikcyjne / fictional data'
DEMO_CONFIG = AppConfig(calendar_id='calendar@example.test', calendar_owner='owner@example.test',
                        state_path=Path('demo-state-unused.sqlite3'), credentials_path=Path('demo-credentials-unused.json'))

# Newly authored synthetic messages; no private source messages were reused.
MESSAGES = [
    {'id': 'demo-trip', 'subject': 'Wyjście klasy do planetarium', 'sender': 'Marta Fikcyjna · nauczycielka demo',
     'sent_at': '2030-03-06T09:30:00+01:00',
     'text': 'Dzień dobry!\n\n8 marca 2030 klasa odwiedzi planetarium. Zbiórka przed szkołą o 08:30, powrót o 12:00. Proszę spakować wodę i małą przekąskę.\n\nMarta Fikcyjna\nWiadomość demonstracyjna — dane fikcyjne.', 'attachment_count': 0},
    {'id': 'demo-robotics', 'subject': 'Koło robotyki — zapisy na warsztaty', 'sender': 'Tomasz Przykładowy · nauczyciel demo',
     'sent_at': '2030-03-06T08:20:00+01:00',
     'text': 'Zapraszamy chętne osoby na dodatkowe warsztaty robotyki 9 marca 2030, od 15:00 do 16:30 w pracowni R. Udział jest dobrowolny i wymaga zapisu przez rodzica.\n\nTomasz Przykładowy\nWiadomość demonstracyjna — dane fikcyjne.', 'attachment_count': 0},
    {'id': 'demo-meeting', 'subject': 'Spotkanie rodziców w sali B', 'sender': 'Marta Fikcyjna · nauczycielka demo',
     'sent_at': '2030-03-05T16:40:00+01:00',
     'text': 'Spotkanie rodziców odbędzie się 7 marca 2030 od 17:30 do 18:30 w sali B. Omówimy plan pracy na kolejny miesiąc.\n\nMarta Fikcyjna\nWiadomość demonstracyjna — dane fikcyjne.', 'attachment_count': 0},
    {'id': 'demo-consent', 'subject': 'Zgoda na szkolne wyjście — termin', 'sender': 'Jan Testowy · sekretariat demo',
     'sent_at': '2030-03-05T12:00:00+01:00',
     'text': 'Prosimy dostarczyć podpisaną zgodę na szkolne wyjście do 7 marca 2030 do godziny 18:00. Formularz otrzymali Państwo podczas spotkania.\n\nJan Testowy\nWiadomość demonstracyjna — dane fikcyjne.', 'attachment_count': 0},
    {'id': 'demo-bulletin', 'subject': 'Biblioteka — nowa półka z książkami', 'sender': 'Ewa Demonstracyjna · biblioteka demo',
     'sent_at': '2030-03-04T11:00:00+01:00',
     'text': 'W bibliotece pojawiła się nowa półka z opowieściami o kosmosie. Zapraszamy do czytania w wolnej chwili. Ta informacja nie zawiera terminu wydarzenia.\n\nEwa Demonstracyjna\nWiadomość demonstracyjna — dane fikcyjne.', 'attachment_count': 0},
]
CALENDAR_EVENTS = [
    {'message_id': 'demo-trip', 'title': 'Wyjście do planetarium', 'start': '2030-03-08T08:30:00+01:00', 'end': '2030-03-08T12:00:00+01:00', 'all_day': False,
     'quote': '8 marca 2030 klasa odwiedzi planetarium. Zbiórka przed szkołą o 08:30, powrót o 12:00.'},
    {'message_id': 'demo-meeting', 'title': 'Spotkanie rodziców', 'start': '2030-03-07T17:30:00+01:00', 'end': '2030-03-07T18:30:00+01:00', 'all_day': False,
     'quote': 'Spotkanie rodziców odbędzie się 7 marca 2030 od 17:30 do 18:30 w sali B.'},
    {'message_id': 'demo-consent', 'title': 'Dostarczyć zgodę na wyjście', 'start': '2030-03-07', 'end': None, 'all_day': True, 'temporal_kind': 'deadline', 'due_at': '2030-03-07T18:00:00+01:00',
     'quote': 'Prosimy dostarczyć podpisaną zgodę na szkolne wyjście do 7 marca 2030 do godziny 18:00.'},
]


def seed_store(path: Path) -> AppStore:
    """Explicit demo DB/config; no home configuration or external clients."""
    with patch('app.core.utcnow', return_value=DEMO_STAMP):
        return _seed_store(path)


def _seed_store(path: Path) -> AppStore:
    store = AppStore(path, config=DEMO_CONFIG)
    by_message = {item['message_id']: item for item in CALENDAR_EVENTS}
    for message in MESSAGES:
        store.save_message(message)
        event = by_message.get(message['id'])
        proposals = []
        if event:
            proposals = [{'kind': 'create', 'title': event['title'], 'description': DEMO_LABEL,
                          'start': event['start'], 'end': event['end'], 'all_day': event['all_day'],
                          'temporal_kind': event.get('temporal_kind', 'event'), 'due_at': event.get('due_at'),
                          'activity_scope': 'school', 'confidence': 'high', 'needs_review': False,
                          'source_quote': event['quote'], 'event_id': None}]
        elif message['id'] == 'demo-robotics':
            proposals = [{'kind': 'create', 'title': 'Dodatkowe warsztaty robotyki', 'description': DEMO_LABEL,
                          'start': '2030-03-09T15:00:00+01:00', 'end': '2030-03-09T16:30:00+01:00',
                          'all_day': False, 'activity_scope': 'extracurricular', 'confidence': 'high', 'needs_review': True,
                          'review_reason': 'Zajęcia dodatkowe wymagają potwierdzenia udziału. Zatwierdź dopiero po podjęciu decyzji o zapisaniu dziecka.',
                          'source_quote': 'Udział jest dobrowolny i wymaga zapisu przez rodzica.', 'event_id': None}]
        store.save_proposals(message['id'], proposals, current=DEMO_NOW, metadata={
            'model': 'synthetic-fixture (no model called)', 'effort': 'none',
            'prompt_version': 'demo-v1', 'decision_reason': 'Synthetic fixture; no AI analysis or calendar write occurred.'})
    # Fake locally verified snapshots; never contact an external calendar.
    for operation in store.queue_operations(current=DEMO_NOW):
        snapshot = {**operation['event'], 'id': 'demo-event-' + operation['message_id']}
        store.record_result(operation['operation_id'], {'status': 'applied', 'event_id': snapshot['id'], 'snapshot': snapshot})
    with store.db:
        for table, columns in {'messages': ['fetched_at', 'analyzed_at'], 'proposals': ['created_at', 'updated_at'],
                               'operations': ['created_at', 'updated_at'], 'events': ['updated_at'], 'analysis_audit': ['analyzed_at']}.items():
            for column in columns:
                store.db.execute(f'UPDATE {table} SET {column}=?', (DEMO_STAMP,))
        store.db.execute("INSERT INTO sync_runs VALUES ('demo-run',?,?,'complete','ok',NULL,12)", (DEMO_STAMP, DEMO_STAMP))
    for key, value in {'health_initialized_at': DEMO_STAMP, 'last_sync': DEMO_STAMP,
                       'last_successful_sync': DEMO_STAMP, 'writes_paused': 'false',
                       'writes_paused_since': '', 'consecutive_sync_failures': '0'}.items():
        store.set_setting(key, value)
    return store


def make_demo_app(store: AppStore, port=8795):
    return create_app(store, sync_request=lambda: None, config=DEMO_CONFIG, allow_local=True,
                      hosts={f'127.0.0.1:{port}', f'localhost:{port}', 'testserver'},
                      demo_mode=True, now=lambda: DEMO_NOW)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8795)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('Use an unprivileged local port between 1024 and 65535.')
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='librus-public-demo-') as directory:
        store = seed_store(Path(directory) / 'state.sqlite3')
        try:
            print(f'{DEMO_LABEL}\nOffline demo: http://127.0.0.1:{args.port}', flush=True)
            uvicorn.run(make_demo_app(store, args.port), host='127.0.0.1', port=args.port,
                        proxy_headers=False, access_log=False, log_level='warning')
        finally:
            store.close()


if __name__ == '__main__':
    main()
