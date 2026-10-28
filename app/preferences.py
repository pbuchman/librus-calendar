"""Future human choices as bounded context for the existing agenda planner.

This is an explicit opt-in ledger, not model training. Historical exact-action
decisions keep their existing authority and are never imported as examples.
"""
from datetime import datetime
import json

from .codex_runtime import RuntimeFailure

SETTING = 'preference_learning'
VERSION = 1
CONTEXT_EXAMPLES = 50
DESCRIPTION_CHARS = 400
SCHEMA = '''CREATE TABLE IF NOT EXISTS preference_examples(
 id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL)'''
ACTION_FIELDS = ('title', 'description', 'start', 'end', 'all_day',
                 'temporal_kind', 'due_at', 'activity_scope', 'optional')


def _instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Preference boundary requires an explicit timezone.')
    return parsed


def enable_preferences(store, receipt_path):
    """Explicit, idempotent admin API; call under sync_lock, no Calendar access."""
    from .core import digest, utcnow
    from .source_admin import read_private_json
    receipt = read_private_json(receipt_path)
    if (receipt.get('kind') != 'administrative_bulk_cleanup'
            or receipt.get('excluded_from_preference_learning') is not True):
        raise ValueError('An administrative learning-boundary receipt is required.')
    cutoff = receipt.get('created_at')
    try:
        if _instant(cutoff) > _instant(utcnow()):
            raise ValueError('Future preference boundary.')
    except (TypeError, AttributeError):
        raise ValueError('Invalid preference boundary.') from None
    pids = receipt.get('preexisting_proposal_ids_excluded_from_future_examples')
    gids = receipt.get('preexisting_group_ids_excluded_from_future_examples')
    if any(not isinstance(ids, list)
           or any(not isinstance(value, str) or not value for value in ids)
           or len(ids) != len(set(ids))
           for ids in (pids, gids)):
        raise ValueError('Complete excluded identities are required.')
    boundary = {'version': VERSION, 'cutoff': cutoff,
                'excluded_proposal_ids': sorted(pids), 'excluded_group_ids': sorted(gids)}
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        old = store._setting(SETTING)
        if old:
            if json.loads(old).get('boundary_hash') != digest(boundary):
                raise ValueError('Preference learning already has a different boundary.')
            return preference_status(store.db)
        # The receipt must cover every row that already existed at its cutoff.
        # Later rows are eligible only when a human actually decides after enable.
        if any(row[0] not in pids and _instant(row[1]) <= _instant(cutoff)
               for row in store.db.execute('SELECT id,created_at FROM proposals')):
            raise ValueError('Receipt does not cover existing proposal history.')
        if any(row[0] not in gids and _instant(row[1]) <= _instant(cutoff)
               for row in store.db.execute('SELECT id,created_at FROM proposal_groups')):
            raise ValueError('Receipt does not cover existing offer history.')
        boundary.update(boundary_hash=digest(boundary), enabled_at=utcnow())
        store.db.execute('INSERT INTO settings VALUES (?,?)', (SETTING, json.dumps(boundary, sort_keys=True)))
    return preference_status(store.db)


def _settings(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (SETTING,)).fetchone()
    return json.loads(row[0]) if row else None


def preference_status(db):
    settings = _settings(db)
    total = db.execute('SELECT COUNT(*) FROM preference_examples').fetchone()[0]
    return {'enabled': bool(settings), 'examples': total, 'context_examples': min(total, CONTEXT_EXAMPLES),
            'context_window': CONTEXT_EXAMPLES, 'calendar_writes': 0}


def _eligible(settings, row, kind):
    return (settings and settings.get('version') == VERSION
            and row['id'] not in settings['excluded_' + kind + '_ids']
            and _instant(row['created_at']) > _instant(settings['cutoff']))


def _compact(action):
    return {key: action[key] for key in ACTION_FIELDS if key in action}


def action_digest(action):
    from .core import digest
    return digest({key: action[key] for key in (*ACTION_FIELDS, 'kind', 'default_duration', 'event_id') if key in action})


def _sources(item):
    return sorted({ref['source_id'] for ref in item.get('agenda_sources', [])}
                  or {item.get('source_id') or 'message:' + item['message_id']})


def _record(store, example):
    from .core import digest, utcnow
    # Repeated identical submissions do not multiply evidence. A changed human
    # choice creates another example so the planner sees the contradiction.
    eid = digest(['human-preference', example])[:32]
    store.db.execute('INSERT OR IGNORE INTO preference_examples VALUES (?,?,?)',
                     (eid, json.dumps(example, ensure_ascii=False, sort_keys=True), utcnow()))


def remember_proposal_choice(store, row, item, decision):
    settings = _settings(store.db)
    if (not _eligible(settings, row, 'proposal') or item.get('kind') != 'create'
            or decision not in ('approve', 'ignore')
            or store.get_proposal_group_for_proposal(row['id'])):
        return
    _record(store, {'kind': 'proposal', 'proposal_id': row['id'],
                   'outcome': 'accept' if decision == 'approve' else 'reject',
                   'action': _compact(item), 'source_ids': _sources(item)})


def remember_group_choice(store, row, members, decision, selected_id):
    settings = _settings(store.db)
    if (not _eligible(settings, row, 'group')
            or any(not _eligible(settings, member, 'proposal') for member in members)):
        return
    choices = [store._proposal(member) for member in members]
    selected = next((item for item in choices if item['id'] == selected_id), None)
    _record(store, {'kind': 'offer', 'group_id': row['id'],
                   'outcome': 'accept' if decision == 'select' else 'reject',
                   'action': _compact(selected or choices[0]),
                   'alternatives': [_compact(item) for item in choices],
                   'source_ids': sorted({sid for item in choices for sid in _sources(item)}),
                   'meaning': 'One participation choice; unselected alternative dates are not negative preferences.'})


def preference_snapshot(db):
    """Also works with old read-only databases; never silently omit examples."""
    settings = _settings(db)
    if not settings:
        return {'enabled': False, 'examples': []}
    total = db.execute('SELECT COUNT(*) FROM preference_examples').fetchone()[0]
    rows = db.execute('SELECT * FROM preference_examples ORDER BY created_at DESC,id DESC LIMIT ?', (CONTEXT_EXAMPLES,)).fetchall()
    examples = []
    for row in rows:
        example = {'id': row[0], **json.loads(row[1])}
        action = example['action']
        description = action.get('description', '')
        action['description'] = description[:DESCRIPTION_CHARS]
        action['description_truncated'] = len(description) > DESCRIPTION_CHARS
        if example['kind'] == 'offer':
            example['alternative_count'] = len(example.pop('alternatives'))
        examples.append(example)
    return {'enabled': True, 'boundary_hash': settings['boundary_hash'],
            'context_window': CONTEXT_EXAMPLES, 'total_examples': total,
            'omitted_older_examples': total-len(examples), 'examples': examples}


def basis_outcome(context, basis):
    """Structural evidence only; semantic similarity is the single planner's job."""
    if not basis:
        return None
    if not context.get('enabled'):
        raise RuntimeFailure('agenda_review_required')
    examples = {example['id']: example for example in context['examples']}
    ids = basis['example_ids']
    if len(ids) != len(set(ids)) or any(eid not in examples for eid in ids):
        raise RuntimeFailure('agenda_plan_invalid')
    cited = [examples[eid] for eid in ids]
    # Independent sources, not repeated clicks or several duties in one notice.
    independent = len(cited) >= 2 and all(
        set(first['source_ids']).isdisjoint(second['source_ids'])
        for index, first in enumerate(cited) for second in cited[index+1:])
    outcomes = {example['outcome'] for example in cited}
    wanted = basis['outcome']
    return wanted if independent and outcomes == {wanted} and wanted in ('accept', 'reject') else 'uncertain'


def authorizes_participation(store, item):
    """Recheck saved proof at queue time; no inferred human approval flag."""
    proof = item.get('preference_decision')
    if not proof or item.get('kind') != 'create' or item.get('activity_scope') != 'extracurricular':
        return False
    from .core import digest
    current = preference_snapshot(store.db)
    if not current.get('enabled') or proof.get('examples_hash') != digest(current['examples']):
        return False
    # Any changed example window invalidates an unexecuted learned decision.
    if (proof.get('boundary_hash') != current.get('boundary_hash')
            or proof.get('action_hash') != action_digest(item)
            or item.get('alternative_group_id') or item.get('blocked_by_offer_group')
            or item.get('blocked_by_group_decision')):
        return False
    plan = store.db.execute("SELECT payload FROM agenda_plans WHERE id=? AND status='applied'", (item.get('agenda_plan_id'),)).fetchone()
    if not plan or not any(saved.get('proposal_id') == item['id']
                           and saved.get('preference_decision') == proof
                           and saved.get('operation') == 'create'
                           and not saved.get('new_offer_key') and not saved.get('offer_group_id')
                           for saved in json.loads(plan[0])['items']):
        return False
    return basis_outcome(current, proof) == 'accept'
