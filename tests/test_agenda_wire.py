"""Fictional, lossless wire representation for one bounded planner request."""
import copy
import json
import unittest

from app.agenda import agenda_prompt, MAX_INPUT_BYTES
from app.codex_runtime import RuntimeFailure


CATALOG_COLUMNS = ['id', 'kind', 'content_hash', 'deleted', 'retained', 'body_available']
LINK_COLUMNS = ['source_id', 'source_hash', 'source_quote', 'relation']


def fictional_snapshot():
    return {
        'snapshot_hash': 'fictional-snapshot', 'sources': [{'id': 'fictional-source', 'text': 'Full fictional body.'}],
        'source_catalog': [dict(zip(CATALOG_COLUMNS, ['source-%d' % i, 'announcement', 'hash-%d' % i, False, False, True]))
                           for i in range(133)],
        'agenda': [{'id': 'action-%d' % i, 'action': {'title': 'Fictional duty', 'all_day': False},
                    'links': [dict(zip(LINK_COLUMNS, ['source-%d' % i, 'hash-%d' % i, 'Full fictional quote.', 'supports']))]
                             + ([dict(zip(LINK_COLUMNS, ['other-%d' % i, '', None, 'context']))] if i < 18 else []),
                    'decisions': [{'user_rejected': True}], 'current_decision': {'user_approved': True},
                    'calendar': [{'current': None}]} for i in range(70)],
        'offer_groups': [{'status': 'selected', 'members': [], 'selected_proposal_id': 'fictional-choice'}],
        'preferences': {'enabled': False, 'examples': []}, 'unsettled_operations': []}


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def wire_data(prompt):
    return json.loads(prompt.rsplit('\n', 1)[1])


def decode_wire(wire):
    decoded = copy.deepcopy(wire)
    if 'source_catalog' in decoded:
        table = decoded['source_catalog']
        decoded['source_catalog'] = [dict(zip(table['columns'], row, strict=True)) for row in table['rows']]
    columns = decoded.pop('agenda_link_columns')
    for entry in decoded['agenda']:
        if 'links' in entry:
            entry['links'] = [dict(zip(columns, row, strict=True)) for row in entry['links']]
    return decoded


class AgendaWireTests(unittest.TestCase):
    def test_complete_utf8_request_accepts_previous_failure_and_exact_new_limit(self):
        from unittest.mock import patch
        for target_bytes in (161040, 320000):
            with self.subTest(input_bytes=target_bytes):
                snapshot = fictional_snapshot()
                snapshot['sources'][0]['text'] = 'Pełny tekst żółwia. '
                with patch('app.agenda.MAX_INPUT_BYTES', 1000000):
                    initial_bytes = len(agenda_prompt(snapshot).encode('utf-8'))
                snapshot['sources'][0]['text'] += 'x' * (target_bytes - initial_bytes)
                original = copy.deepcopy(snapshot)
                prompt = agenda_prompt(snapshot)
                self.assertEqual(len(prompt.encode('utf-8')), target_bytes)
                self.assertEqual(decode_wire(wire_data(prompt)), original)
                self.assertEqual(snapshot, original)

    def test_near_cap_full_dictionary_input_fits_losslessly_on_wire(self):
        snapshot = fictional_snapshot()
        snapshot['sources'][0]['text'] += 'x' * (MAX_INPUT_BYTES + 2000 - json_bytes(snapshot))
        self.assertEqual(json_bytes(snapshot), MAX_INPUT_BYTES + 2000)
        original = copy.deepcopy(snapshot)
        prompt = agenda_prompt(snapshot)
        self.assertLessEqual(len(prompt.encode()), MAX_INPUT_BYTES)
        self.assertEqual(decode_wire(wire_data(prompt)), original)
        self.assertEqual(snapshot, original)

    def test_tables_roundtrip_false_null_empty_and_absence_without_mutation(self):
        snapshot = fictional_snapshot()
        snapshot['source_catalog'][0].update(deleted=None, retained=False, body_available=False, content_hash='')
        snapshot['agenda'][0]['links'][0].update(source_quote='Full quoted\nfictional text: żółty', source_hash=None, relation='')
        snapshot['agenda'][1]['links'] = []
        del snapshot['agenda'][2]['links']
        original = copy.deepcopy(snapshot)
        wire = wire_data(agenda_prompt(snapshot))
        self.assertEqual(wire['source_catalog']['columns'], CATALOG_COLUMNS)
        self.assertEqual(wire['agenda_link_columns'], LINK_COLUMNS)
        self.assertEqual(len(wire['source_catalog']['rows']), 133)
        self.assertEqual(decode_wire(wire), original)
        self.assertEqual(snapshot, original)
        del snapshot['source_catalog']
        self.assertNotIn('source_catalog', wire_data(agenda_prompt(snapshot)))
        self.assertEqual(decode_wire(wire_data(agenda_prompt(snapshot))), snapshot)
        empty = {'sources': [], 'source_catalog': [], 'agenda': []}
        self.assertEqual(decode_wire(wire_data(agenda_prompt(empty))), empty)

    def test_unrepresentable_row_fields_and_reserved_header_fail_explicitly(self):
        for target, key in (('source_catalog', 'kind'), ('links', 'source_quote')):
            for change in ('missing', 'extra', 'null_row'):
                snapshot = fictional_snapshot()
                rows = snapshot['source_catalog'] if target == 'source_catalog' else snapshot['agenda'][0]['links']
                if change == 'missing':
                    del rows[0][key]
                elif change == 'extra':
                    rows[0]['fictional_extra'] = None
                else:
                    rows[0] = None
                with self.subTest(target=target, change=change), self.assertRaisesRegex(RuntimeFailure, 'agenda_snapshot_invalid'):
                    agenda_prompt(snapshot)
        snapshot = fictional_snapshot()
        snapshot['agenda_link_columns'] = ['private-field']
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_snapshot_invalid'):
            agenda_prompt(snapshot)
        for field in ('source_catalog', 'agenda', 'links'):
            snapshot = fictional_snapshot()
            if field == 'links':
                snapshot['agenda'][0]['links'] = None
            else:
                snapshot[field] = None
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeFailure, 'agenda_snapshot_invalid'):
                agenda_prompt(snapshot)

    def test_true_overflow_measures_final_request_and_fixed_numeric_sections(self):
        snapshot = fictional_snapshot()
        snapshot['sources'][0]['text'] = 'x' * MAX_INPUT_BYTES
        with self.assertRaisesRegex(RuntimeFailure, 'agenda_context_overflow') as caught:
            agenda_prompt(snapshot)
        error = caught.exception
        from unittest.mock import patch
        with patch('app.agenda.MAX_INPUT_BYTES', 1000000):
            prompt = agenda_prompt(snapshot)
        self.assertEqual(error.input_bytes, len(prompt.encode()))
        self.assertEqual(error.input_limit_bytes, 320000)
        self.assertEqual(set(error.section_bytes), {'sources', 'source_catalog', 'agenda', 'offer_groups',
                                                  'preferences', 'unsettled_operations', 'envelope'})
        self.assertEqual(sum(error.section_bytes.values()), error.input_bytes)
        wire = wire_data(prompt)
        for key in set(error.section_bytes) - {'envelope'}:
            self.assertEqual(error.section_bytes[key], json_bytes(wire[key]))
        self.assertTrue(all(type(value) is int and 0 <= value < 2**63 for value in error.section_bytes.values()))
        self.assertEqual((error.source_count, error.catalog_count, error.agenda_count), (1, 133, 70))
