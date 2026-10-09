from test_support import CALENDAR_ID, TEST_CONFIG, event_fingerprint
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.core import AppStore, WARSAW, normalize_proposal, digest

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=WARSAW)
QUOTE = 'Wycieczka odbędzie się 10 listopada o 09:00.'


def proposal(**patch):
    result = {'kind': 'create', 'title': 'Wycieczka', 'description': 'Zbiórka', 'start': '2030-11-10T09:00:00+01:00',
              'end': None, 'all_day': False, 'confidence': 'high', 'needs_review': False, 'source_quote': QUOTE, 'activity_scope': 'school'}
    result.update(patch)
    return result


def message(message_id='1', **patch):
    result = {'id': message_id, 'subject': 'Wycieczka', 'sender': 'Teacher', 'sent_at': '2026-10-08T10:00:00+02:00', 'text': QUOTE}
    result.update(patch)
    return result


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(Path(self.tmp.name)/'private/state.sqlite3', config=TEST_CONFIG)
        self.store.save_message(message())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def apply_create(self):
        item = self.store.save_proposals('1', [proposal()], NOW)[0]
        operation = self.store.queue_operations(NOW)[0]
        self.store.set_writes_paused(False)
        self.assertTrue(self.store.claim_operation(operation['operation_id']))
        snapshot = {**operation['event'], 'id': 'event-1'}
        self.store.record_result(operation['operation_id'], {'status': 'applied', 'event_id': 'event-1', 'snapshot': snapshot, 'fingerprint': digest(snapshot)})
        return operation, snapshot

    def test_dates_timezone_defaults_and_reminders(self):
        item = self.store.save_proposals('1', [proposal(start='2030-11-10T09:00:00')], NOW)[0]
        self.assertEqual(item['start'], '2030-11-10T09:00:00+01:00')
        operation = self.store.queue_operations(NOW)[0]
        self.assertEqual(operation['calendar_id'], CALENDAR_ID)
        self.assertEqual(operation['event']['end']['dateTime'], '2030-11-10T10:00:00+01:00')
        self.assertIn('60 minut', operation['event']['description'])
        self.assertEqual([r['minutes'] for r in operation['event']['reminders']['overrides']], [1440, 60])
        self.assertNotIn('attendees', operation['event'])
        self.assertNotIn('extendedProperties', operation['event'])
        self.assertTrue(operation['event']['summary'].startswith('[Librus]'))

    def test_branding_strips_old_and_duplicate_prefixes_and_legacy_scope_is_unknown(self):
        item = normalize_proposal(proposal(title='[Szkoła] [Librus] [Szkoła] Wycieczka'), NOW)
        self.assertEqual(item['title'], '[Librus] Wycieczka')
        raw = proposal()
        raw.pop('activity_scope')
        legacy = normalize_proposal(raw, NOW)
        self.assertEqual(legacy['activity_scope'], 'unknown')
        self.assertEqual(legacy['status'], 'review')

    def test_medium_school_action_auto_but_extra_unknown_and_ambiguous_require_review(self):
        self.assertEqual(normalize_proposal(proposal(confidence='medium', activity_scope='school'), NOW)['status'], 'pending')
        for item in (proposal(confidence='medium', needs_review=True), proposal(confidence='low'),
                     proposal(activity_scope='extracurricular'), proposal(activity_scope='unknown'),
                     proposal(kind='update', activity_scope='unknown', event_id='owned')):
            with self.subTest(item=item):
                self.assertEqual(normalize_proposal(item, NOW)['status'], 'review')
        approved = normalize_proposal(proposal(activity_scope='extracurricular', user_approved=True), NOW)
        self.assertEqual(approved['status'], 'pending')

    def test_mixed_message_optional_craft_does_not_inherit_extra_activity_review(self):
        text = QUOTE + ' Dla chętnych: wykonanie papierowego serca w klasie. Zapisy na dodatkowe zajęcia taneczne.'
        self.store.save_message(message(text=text))
        items = self.store.save_proposals('1', [proposal(confidence='medium', activity_scope='school'),
            proposal(title='Zajęcia taneczne', start='2030-11-11T09:00:00+01:00', activity_scope='extracurricular')], NOW)
        self.assertEqual([item['status'] for item in items], ['pending', 'review'])
        self.assertEqual(len(self.store.queue_operations(NOW)), 1)

    def test_operation_preparation_timestamp_is_frozen_through_queue_retry_and_reconciliation(self):
        self.store.save_proposals('1', [proposal()], NOW)
        with patch('app.core.utcnow', return_value='2026-10-08T10:00:01+00:00'):
            operation = self.store.queue_operations(NOW)[0]
        self.assertEqual(operation['synchronization_started_at'], '2026-10-08T10:00:01+00:00')
        self.assertIn('Synchronizacja rozpoczęta: 08.10.2026 12:00:01 (Europe/Warsaw)', operation['event']['description'])
        with patch('app.core.utcnow', return_value='2026-10-09T11:00:00+00:00'):
            self.store.queue_operations(NOW)
        self.assertEqual(self.store.pending_operations()[0], operation)
        self.store.set_writes_paused(False)
        self.store.claim_operation(operation['operation_id'])
        self.store.record_result(operation['operation_id'], {'status': 'failed', 'error': 'No external write.'})
        self.store.queue_operations(NOW)
        self.assertEqual(self.store.pending_operations()[0], operation)
        self.store.claim_operation(operation['operation_id'])
        self.store.recover_interrupted()
        self.store.queue_operations(NOW)
        reconciled = self.store.reconcile_operations()[0]
        self.assertTrue(reconciled.pop('reconcile_only'))
        self.assertEqual(reconciled, operation)
        self.assertEqual(operation['event']['description'].count('Synchronizacja rozpoczęta:'), 1)

    def test_all_day_end_and_previous_evening_reminder(self):
        self.store.save_proposals('1', [proposal(start='2030-11-10', all_day=True)], NOW)
        body = self.store.queue_operations(NOW)[0]['event']
        self.assertEqual(body['start'], {'date': '2030-11-10'})
        self.assertEqual(body['end'], {'date': '2030-11-11'})
        self.assertEqual(body['reminders']['overrides'], [{'method': 'popup', 'minutes': 360}])

    def test_deadline_is_one_all_day_date_with_exact_due_time(self):
        item = self.store.save_proposals('1', [proposal(temporal_kind='deadline', all_day=True,
            start='2030-11-10', end='2030-11-14', due_at='2030-11-10T16:30:15+01:00')], NOW)[0]
        self.assertEqual(item['temporal_kind'], 'deadline')
        self.assertEqual(item['due_at'], '2030-11-10T16:30:15+01:00')
        self.assertEqual(item['end'], '2030-11-11')
        self.assertFalse(item['default_duration'])
        body = self.store.queue_operations(NOW)[0]['event']
        self.assertEqual(body['start'], {'date': '2030-11-10'})
        self.assertEqual(body['end'], {'date': '2030-11-11'})
        self.assertIn('do 16:30', body['summary'])
        self.assertIn('2030-11-10 do 16:30', body['description'])
        self.assertNotIn('60 minut', body['description'])

    def test_deadline_rejects_mismatched_day_offset_and_missing_time_zone(self):
        for due_at in ('2030-11-11T16:00:00+01:00', '2030-11-10T16:00:00+02:00', '2030-11-10T16:00:00'):
            with self.subTest(due_at=due_at), self.assertRaises(ValueError):
                normalize_proposal(proposal(temporal_kind='deadline', start='2030-11-10', all_day=True, due_at=due_at), NOW)
        invalid = self.store.save_proposals('1', [proposal(temporal_kind='deadline', due_at='2030-11-10T09:00:00+01:00')], NOW)[0]
        self.assertEqual(invalid['status'], 'review')
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_legacy_event_and_date_only_reminder_preserve_semantics(self):
        legacy = normalize_proposal(proposal(), NOW)
        self.assertEqual(legacy['temporal_kind'], 'event')
        self.assertIsNone(legacy['due_at'])
        self.assertTrue(legacy['default_duration'])
        reminder = normalize_proposal(proposal(temporal_kind='reminder', start='2030-11-10', all_day=True), NOW)
        self.assertEqual(reminder['end'], '2030-11-11')
        self.assertFalse(reminder['default_duration'])
        with self.assertRaises(ValueError):
            normalize_proposal(proposal(temporal_kind='reminder', start='2030-11-10'), NOW)
        with self.assertRaises(ValueError):
            normalize_proposal(proposal(temporal_kind='reminder'), NOW)

    def test_expired_exact_deadline_today_is_not_queued(self):
        item = normalize_proposal(proposal(temporal_kind='deadline', start='2026-10-08', all_day=True,
            due_at='2026-10-08T11:59:00+02:00'), NOW)
        self.assertEqual(item['status'], 'ignored')

    def test_stale_unattempted_deadline_body_is_rebuilt_as_one_calendar_day(self):
        item = self.store.save_proposals('1', [proposal(temporal_kind='deadline', start='2030-11-10',
            end=None, all_day=True, due_at='2030-11-10T16:00:00+01:00')], NOW)[0]
        operation = self.store.queue_operations(NOW)[0]
        bad = dict(operation, event={**operation['event'], 'start': {'dateTime': '2030-11-10T16:00:00+01:00'},
                                    'end': {'dateTime': '2030-11-10T17:00:00+01:00'}})
        with self.store.db:
            self.store.db.execute('UPDATE operations SET payload=? WHERE id=?', (json.dumps(bad), operation['operation_id']))
        self.store.queue_operations(NOW)
        restored = self.store.pending_operations()[0]['event']
        self.assertEqual(restored['start'], {'date': '2030-11-10'})
        self.assertEqual(restored['end'], {'date': '2030-11-11'})

    def test_stale_hour_long_deadline_operation_cannot_be_executed(self):
        item = self.store.save_proposals('1', [proposal()], NOW)[0]
        operation = self.store.queue_operations(NOW)[0]
        # A payload produced before deadline semantics were enforced is quarantined.
        with self.store.db:
            raw = dict(item, temporal_kind='deadline', due_at=item['start'])
            self.store.db.execute('UPDATE proposals SET payload=? WHERE id=?', (json.dumps(raw), item['id']))
        self.store.queue_operations(NOW)
        self.store.set_writes_paused(False)
        self.assertEqual(self.store.pending_operations(), [])
        self.assertFalse(self.store.claim_operation(operation['operation_id']))
        self.assertEqual(self.store.list_proposals()[0]['status'], 'review')

    def test_metadata_and_empty_analysis_are_saved_together(self):
        metadata = {'model': 'gpt-6.1-sol', 'effort': 'high', 'prompt_version': 'school-actions-v2',
                    'decision_reason': 'Information only; no dated action.', 'private_extra': 'must not persist'}
        self.store.save_proposals('1', [], NOW, metadata)
        message = self.store.get_message('1')
        self.assertEqual(message['analysis_metadata']['decision_reason'], metadata['decision_reason'])
        self.assertNotIn('private_extra', message['analysis_metadata'])
        self.assertIsNotNone(message['analyzed_at'])
        audit = self.store.get_analysis_history('1')[0]
        self.assertEqual(audit['proposal_ids'], [])
        self.assertEqual(audit['source_hash'], message['payload_hash'])
        self.assertEqual(audit['metadata'], message['analysis_metadata'])

    def test_past_uncertain_and_missing_quote_never_auto_queue(self):
        cases = [proposal(start='2026-10-07T09:00:00+02:00'), proposal(confidence='low'), proposal(source_quote='invented source quote')]
        items = self.store.save_proposals('1', cases, NOW)
        self.assertEqual([i['status'] for i in items], ['ignored', 'review', 'review'])
        self.assertEqual(self.store.queue_operations(NOW), [])

    def test_dst_gap_and_ambiguous_wall_time_require_review(self):
        for value in ('2030-03-31T02:30:00', '2030-10-27T02:30:00'):
            with self.assertRaises(ValueError):
                normalize_proposal(proposal(start=value), NOW)

    def test_cross_message_duplicate_requires_review(self):
        first = self.store.save_proposals('1', [proposal()], NOW)[0]
        self.store.save_message(message('2'))
        second = self.store.save_proposals('2', [proposal(title='WYCIECZKA')], NOW)[0]
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(second['status'], 'review')
        self.assertIn('same time', second['review_reason'])

    def test_interrupted_operation_reconciles_only_and_cannot_be_reapproved(self):
        item = self.store.save_proposals('1', [proposal()], NOW)[0]
        op = self.store.queue_operations(NOW)[0]
        self.store.set_writes_paused(False)
        self.store.claim_operation(op['operation_id'])
        self.assertEqual(self.store.recover_interrupted(), 1)
        unknown = self.store.reconcile_operations()[0]
        self.assertTrue(unknown['reconcile_only'])
        self.assertEqual(self.store.pending_operations(), [])
        self.store.record_result(op['operation_id'], {'status': 'failed', 'error': 'Read-only reconciliation could not find the event.'})
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(len(self.store.reconcile_operations()), 1)
        with self.assertRaises(ValueError):
            self.store.resolve_proposal(item['id'], 'approve')

    def test_manual_cancel_confirmation_only_and_tombstone(self):
        initial, snapshot = self.apply_create()
        self.store.save_message(message('2', text=QUOTE + ' Wycieczka odwołana.'))
        item = self.store.save_proposals('2', [proposal(kind='cancel', event_id='event-1', deletion_approved=True)], NOW)[0]
        self.assertEqual(item['status'], 'review')
        self.assertFalse(item['deletion_approved'])
        with self.assertRaises(ValueError):
            self.store.resolve_proposal(item['id'], 'approve')
        approved = self.store.resolve_proposal(item['id'], 'confirm_cancel')
        self.assertTrue(approved['deletion_approved'])
        op = self.store.queue_operations(NOW)[0]
        self.assertTrue(op['deletion_approved'])
        self.assertEqual(op['action'], 'cancel')
        self.assertEqual(op['prior_snapshot'], snapshot)
        self.assertEqual(op['prior_marker'], initial['marker'])
        self.assertEqual(op['prior_fingerprint'], op['expected_fingerprint'])
        self.store.claim_operation(op['operation_id'])
        self.store.record_result(op['operation_id'], {'status': 'applied', 'event_id': 'event-1', 'snapshot': {**snapshot, 'status': 'cancelled'}, 'fingerprint': 'cancelled'})
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual(self.store.status()['events_cancelled'], 1)
        self.store.save_message(message('3'))
        recreated = self.store.save_proposals('3', [proposal()], NOW)[0]
        self.assertEqual(recreated['status'], 'review')

    def test_manual_target_and_attendee_response_rejected(self):
        item = self.store.save_proposals('1', [proposal(kind='update', event_id='not-app-owned')], NOW)[0]
        self.assertEqual(item['status'], 'review')
        with self.assertRaises(ValueError):
            self.store.resolve_proposal(item['id'], 'approve')
        self.store.resolve_proposal(item['id'], 'ignore')
        item = self.store.save_proposals('1', [proposal(title='Other')], NOW)[0]
        op = self.store.queue_operations(NOW)[0]
        self.store.record_result(op['operation_id'], {'status': 'applied', 'event_id': 'bad', 'snapshot': {**op['event'], 'attendees': [{'email': 'other@example.test'}]}})
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual(self.store.list_proposals('review')[0]['id'], item['id'])

    def test_connector_wrapped_marker_and_verified_self_attendee(self):
        self.store.save_proposals('1', [proposal()], NOW)
        operation = self.store.queue_operations(NOW)[0]
        wrapped = operation['marker'].replace('librus-calendar:', 'librus-\ncalendar:')
        snapshot = {**operation['event'], 'id': 'wrapped-event',
                    'description': 'Kontrolowane wydarzenie testowe. ' + wrapped + '\n\n',
                    'attendees': [{'email': TEST_CONFIG.calendar_owner, 'is_self': True, 'responseStatus': 'accepted'}],
                    'start': operation['event']['start']['dateTime'], 'end': operation['event']['end']['dateTime']}
        self.store.record_result(operation['operation_id'], {'status': 'applied', 'event_id': 'wrapped-event', 'snapshot': snapshot, 'fingerprint': 'wrapped'})
        self.assertEqual(self.store.status()['events'], 1)
        self.assertEqual(self.store.list_events()[0]['id'], 'wrapped-event')
        self.store.save_message(message('2'))
        same = self.store.save_proposals('2', [proposal(title='Different title, same ceremony')], NOW)[0]
        self.assertEqual(same['status'], 'review')
        self.store.save_message(message('3'))
        later = self.store.save_proposals('3', [proposal(start='2030-11-11T09:00:00+01:00')], NOW)[0]
        self.assertEqual(later['status'], 'pending')

    def test_upgrade_rechecks_existing_pending_same_instant_with_different_titles(self):
        first = self.store.save_proposals('1', [proposal(title='Pasowanie i akademia')], NOW)[0]
        self.store.queue_operations(NOW)
        self.store.save_message(message('2'))
        second = self.store.save_proposals('2', [proposal(title='Uroczyste pasowanie')], NOW)[0]
        # Simulate a proposal created by the older title-only duplicate rule.
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending' WHERE id=?", (second['id'],))
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(len(self.store.list_proposals('review')), 2)

    def test_optional_registration_offer_requires_explicit_participation_approval(self):
        self.store.save_message(message(text=QUOTE + ' Zapisy chętnych na dodatkowe zajęcia taneczne.'))
        item = self.store.save_proposals('1', [proposal(user_approved=True, activity_scope='extracurricular')], NOW)[0]
        self.assertEqual(item['status'], 'review')
        self.assertFalse(item['user_approved'])
        self.assertEqual(self.store.queue_operations(NOW), [])
        approved = self.store.resolve_proposal(item['id'], 'approve')
        self.assertTrue(approved['user_approved'])
        self.assertEqual(len(self.store.queue_operations(NOW)), 1)

    def test_actual_participation_confirmation_cues_and_legacy_pending(self):
        text = QUOTE + ' W celu potwierdzenia udziału dziecka w zajęciach prosimy o wysłanie wiadomości SMS oraz wydrukowanie i podpisanie ulotki. Po otrzymaniu zgłoszenia dziecko może uczestniczyć.'
        self.store.save_message(message(text=text))
        item = self.store.save_proposals('1', [proposal(activity_scope='unknown')], NOW)[0]
        self.assertEqual(item['status'], 'review')
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='pending' WHERE id=?", (item['id'],))
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(self.store.list_proposals()[0]['status'], 'review')
        self.assertIn('participation', self.store.list_proposals()[0]['review_reason'])

    def test_explicit_approval_rebases_only_verified_manual_edit_conflict(self):

        initial, snapshot = self.apply_create()
        item = self.store.save_proposals('1', [proposal(kind='update', event_id='event-1', title='Updated trip')], NOW)[0]
        operation = self.store.queue_operations(NOW)[0]
        edited = {**snapshot, 'summary': '[Szkoła] User edited title'}
        fingerprint = event_fingerprint(edited)
        self.store.record_result(operation['operation_id'], {'status': 'review', 'error': 'manual_edit_conflict', 'event_id': 'event-1', 'snapshot': edited, 'fingerprint': fingerprint})
        self.store.resolve_proposal(item['id'], 'approve')
        queued = self.store.queue_operations(NOW)[0]
        self.assertEqual(queued['expected_fingerprint'], fingerprint)
        self.assertEqual(self.store.list_events()[0]['snapshot']['summary'], '[Szkoła] User edited title')

    def test_manual_conflict_rebase_rejects_foreign_guests_and_wrong_marker(self):

        initial, snapshot = self.apply_create()
        item = self.store.save_proposals('1', [proposal(kind='update', event_id='event-1')], NOW)[0]
        operation = self.store.queue_operations(NOW)[0]
        for patch in ({'attendees': [{'email': 'foreign@example.test', 'is_self': True}]}, {'description': 'Manual event without app marker'}):
            edited = {**snapshot, **patch}
            self.store.record_result(operation['operation_id'], {'status': 'review', 'error': 'manual_edit_conflict', 'event_id': 'event-1', 'snapshot': edited, 'fingerprint': event_fingerprint(edited)})
            with self.assertRaises(ValueError):
                self.store.resolve_proposal(item['id'], 'approve')
            self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(self.store.list_events()[0]['snapshot'], snapshot)

    def test_unknown_cancel_recovers_trusted_prior_snapshot_for_legacy_operation(self):
        initial, snapshot = self.apply_create()
        self.store.save_message(message('2', text=QUOTE + ' Odwołane.'))
        item = self.store.save_proposals('2', [proposal(kind='cancel', event_id='event-1')], NOW)[0]
        self.store.resolve_proposal(item['id'], 'confirm_cancel')
        operation = self.store.queue_operations(NOW)[0]
        legacy = {key: value for key, value in operation.items() if not key.startswith('prior_')}
        with self.store.db:
            self.store.db.execute("UPDATE operations SET status='unknown',payload=? WHERE id=?", (json.dumps(legacy), operation['operation_id']))
        recovered = self.store.reconcile_operations()[0]
        self.assertTrue(recovered['reconcile_only'])
        self.assertEqual(recovered['prior_snapshot'], snapshot)
        self.assertEqual(recovered['prior_marker'], initial['marker'])
        self.assertEqual(recovered['prior_fingerprint'], operation['expected_fingerprint'])
        self.store.record_result(operation['operation_id'], {'status': 'applied', 'event_id': 'event-1', 'snapshot': {**snapshot, 'status': 'cancelled'}, 'fingerprint': 'tombstone'})
        self.assertEqual(self.store.status()['events'], 0)
        self.assertEqual(self.store.status()['events_cancelled'], 1)

    def test_cancel_result_requires_exact_target_and_cancelled_status(self):
        initial, snapshot = self.apply_create()
        self.store.save_message(message('2'))
        item = self.store.save_proposals('2', [proposal(kind='cancel', event_id='event-1')], NOW)[0]
        self.store.resolve_proposal(item['id'], 'confirm_cancel')
        operation = self.store.queue_operations(NOW)[0]
        for returned_id, returned_snapshot in [('different-event', {**snapshot, 'id': 'different-event', 'status': 'cancelled'}), ('event-1', snapshot)]:
            self.store.record_result(operation['operation_id'], {'status': 'applied', 'event_id': returned_id, 'snapshot': returned_snapshot, 'fingerprint': 'fp'})
            self.assertEqual(self.store.status()['events'], 1)
            self.assertEqual(self.store.status()['events_cancelled'], 0)

    def test_past_optional_offer_stays_ignored_before_all_review_guards(self):
        self.store.save_message(message(text='Zapisy chętnych na dodatkowe zajęcia. Po otrzymaniu zgłoszenia potwierdzamy udział.'))
        item = self.store.save_proposals('1', [proposal(start='2026-09-30T09:00:00+02:00', source_quote='mismatched optional quote')], NOW)[0]
        self.assertEqual(item['status'], 'ignored')
        self.assertIn('Past date', item['review_reason'])
        self.assertEqual(self.store.queue_operations(NOW), [])
        self.assertEqual(self.store.status()['proposals_review'], 0)

    def test_previously_queued_operation_expires_with_its_past_proposal(self):
        self.store.save_proposals('1', [proposal()], NOW)
        self.store.queue_operations(NOW)
        self.assertEqual(len(self.store.pending_operations()), 1)
        self.store.queue_operations(datetime(2031, 1, 1, tzinfo=WARSAW))
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(self.store.list_proposals()[0]['status'], 'ignored')

    def test_modified_message_invalidates_only_unwritten_work(self):
        self.store.save_proposals('1', [proposal()], NOW)
        self.store.queue_operations(NOW)
        self.assertTrue(self.store.save_message(message(text=QUOTE + ' Nowy termin.')))
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(self.store.analysis_messages()[0]['id'], '1')

    def test_backup_private_retention_and_restorable(self):
        paths = [self.store.backup() for _ in range(9)]
        kept = list(self.store.path.parent.glob('backup-*.sqlite3'))
        self.assertEqual(len(kept), 7)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in kept))
        restored = sqlite3.connect(paths[-1])
        self.assertEqual(restored.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        restored.close()
        state = self.store.status()
        self.assertEqual(state['last_backup_result'], 'ok')
        self.assertIsNotNone(state['last_backup_attempt_at'])
        self.assertIsNotNone(state['last_backup_success_at'])

    def test_failed_backup_integrity_never_advances_success_and_removes_bad_file(self):
        self.store.backup()
        success = self.store.status()['last_backup_success_at']
        original_connect = sqlite3.connect
        class BadBackup(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == 'PRAGMA integrity_check':
                    return [('corrupt database',)]
                return super().execute(sql, parameters)
        destination = self.store.path.parent / 'bad-backup.sqlite3'
        with patch('app.core.sqlite3.connect', side_effect=lambda *args, **kwargs: original_connect(*args, factory=BadBackup, **kwargs)):
            with self.assertRaises(ValueError):
                self.store.backup(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.store.status()['last_backup_success_at'], success)
        self.assertEqual(self.store.status()['last_backup_result'], 'failed')

    def test_backup_failure_preserves_existing_destination(self):
        destination = self.store.path.parent / 'existing.sqlite3'
        destination.write_bytes(b'preserve existing file')
        with self.assertRaises(FileExistsError):
            self.store.backup(destination)
        self.assertEqual(destination.read_bytes(), b'preserve existing file')
        self.assertEqual(self.store.status()['last_backup_result'], 'failed')


if __name__ == '__main__':
    unittest.main()
