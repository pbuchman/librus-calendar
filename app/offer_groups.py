"""Explicit, source-bound alternatives and atomic human decisions.

Group metadata is additive: proposal identities and write journals remain the
existing authorities. Titles never establish membership.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat

GROUP_SCHEMA = '''
CREATE TABLE IF NOT EXISTS proposal_groups(
 id TEXT PRIMARY KEY,message_id TEXT NOT NULL,source_hash TEXT NOT NULL,
 group_key TEXT NOT NULL,source_evidence TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('open','selected','rejected')),
 selected_proposal_id TEXT,revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(message_id,source_hash,group_key));
CREATE TABLE IF NOT EXISTS proposal_group_members(
 group_id TEXT NOT NULL REFERENCES proposal_groups(id),
 proposal_id TEXT NOT NULL UNIQUE,position INTEGER NOT NULL,
 PRIMARY KEY(group_id,proposal_id));
CREATE INDEX IF NOT EXISTS proposal_groups_message ON proposal_groups(message_id,source_hash);
'''
CHOICE = re.compile(r'\b(?:do\s+wyboru|jeden\s+z|jedn[ąa]\s+z|wyb(?:ierz|rać|rac|oru|ran)|alternatyw\w*|choose|either|one\s+of)\b', re.I)


def _compact(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def validate_alternatives(message, proposals):
    """Keep unsupported group claims manual and ungrouped; titles prove nothing."""
    groups, invalid = {}, set()
    source = _compact(message.get('text'))
    for index, item in enumerate(proposals):
        if not isinstance(item, dict):
            continue
        key, evidence = item.get('alternative_group'), item.get('alternative_group_evidence')
        if key is None and evidence is None:
            continue
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key):
            invalid.add(index)
            continue
        groups.setdefault(key, []).append((index, item))
        if (not isinstance(evidence, str) or not 6 <= len(evidence) <= 4000
                or _compact(evidence) not in source
                or item.get('kind', 'create') != 'create' or item.get('temporal_kind', 'event') != 'event'
                or item.get('activity_scope', 'unknown') != 'extracurricular' or item.get('event_id')):
            invalid.add(index)
    from .core import start_key
    for items in groups.values():
        starts = [start_key(item) for _, item in items]
        if (any(index in invalid for index, _ in items) or not 2 <= len(items) <= 30
                or None in starts or len(set(starts)) != len(items)
                or len({_compact(item.get('alternative_group_evidence')) for _, item in items}) != 1):
            invalid.update(index for index, _ in items)
    for index in invalid:
        proposals[index].pop('alternative_group', None)
        proposals[index].pop('alternative_group_evidence', None)
        proposals[index].update(needs_review=True, review_reason='Alternative relationship is not established by the source; review each action.')
    return groups


class ProposalGroupsMixin:
    @contextmanager
    def _group_snapshot(self):
        own = not self.db.in_transaction
        if own:
            self.db.execute('BEGIN')
        try:
            yield
        finally:
            if own:
                self.db.rollback()

    def _retire_open_groups(self, message_id, keep_source_hash=None):
        """Retire unsupported automatic sets; never manufacture a user rejection."""
        from .core import utcnow
        sql = "SELECT * FROM proposal_groups WHERE message_id=? AND status='open'"
        params = [str(message_id)]
        if keep_source_hash is not None:
            sql += ' AND source_hash<>?'
            params.append(keep_source_hash)
        for group in self.db.execute(sql, params).fetchall():
            members = self.db.execute('SELECT p.* FROM proposals p JOIN proposal_group_members m ON m.proposal_id=p.id WHERE m.group_id=?', (group['id'],)).fetchall()
            for member in members:
                item = self._proposal(member)
                if any(item.get(flag) for flag in ('user_approved','user_rejected','user_edited','deletion_approved')):
                    raise ValueError('An open group contains protected human evidence.')
                if self.db.execute("SELECT 1 FROM operations WHERE proposal_id=? AND (attempts>0 OR result IS NOT NULL OR status NOT IN ('pending','review','superseded'))", (member['id'],)).fetchone() or self.db.execute('SELECT 1 FROM events WHERE proposal_id=?', (member['id'],)).fetchone():
                    raise ValueError('An open group contains external write evidence.')
            stamp = utcnow()
            for member in members:
                self.db.execute("UPDATE proposals SET status='superseded',updated_at=?,pending_since=NULL WHERE id=?", (stamp,member['id']))
                self.db.execute("UPDATE operations SET status='superseded',last_error='Alternative source retired; no automatic replay.',updated_at=? WHERE proposal_id=? AND attempts=0",(stamp,member['id']))
            self.db.execute('DELETE FROM proposal_group_members WHERE group_id=?',(group['id'],))
            self.db.execute('DELETE FROM proposal_groups WHERE id=?',(group['id'],))

    def _protect_group_decision(self, message, item):
        """Uncertain model regrouping cannot replace a closed human decision."""
        closed = self.db.execute("SELECT * FROM proposal_groups WHERE message_id=? AND source_hash=? AND status<>'open'",(message['id'],message['payload_hash'])).fetchall()
        if not closed or item.get('kind') != 'create' or item.get('activity_scope') != 'extracurricular':
            return item
        if item.get('user_rejected') is True and item.get('blocked_by_group_decision'):
            return item
        # An already-known independent offer remains independent, including a
        # sibling offer with the same start. Dates never prove offer identity.
        if item.get('alternative_group') and self.db.execute("SELECT 1 FROM proposal_groups WHERE message_id=? AND source_hash=? AND group_key=? AND status='open'",(message['id'],message['payload_hash'],item['alternative_group'])).fetchone():
            return item
        prior = self.db.execute("SELECT g.id FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id WHERE m.proposal_id=? AND g.status<>'open' AND g.source_hash=?",(item['id'],message['payload_hash'])).fetchone()
        if prior:
            # protect_decision already established the exact source/action/
            # quote signature and restored its original proposal identity.
            item['alternative_group_id']=prior['id']
        elif not item.get('alternative_group') and not item.get('alternative_group_id'):
            from .core import start_key
            # A date collision is only a reason to require reconciliation. It
            # never transfers another offer's approval or rejection.
            known_starts = [start_key(self._proposal(row)) for row in self.db.execute(
                "SELECT p.* FROM proposals p JOIN proposal_group_members m ON m.proposal_id=p.id JOIN proposal_groups g ON g.id=m.group_id WHERE g.message_id=? AND g.source_hash=?",
                (message['id'],message['payload_hash']))]
            if start_key(item) not in known_starts:
                return item
            item.update(status='review',needs_review=True,user_approved=False,user_rejected=False,
                blocked_by_group_decision=closed[0]['id'],review_reason='A prior offer decision cannot be matched safely; this source requires review before changing the offer.')
        else:
            item.update(status='review',needs_review=True,user_approved=False,user_rejected=False,
                blocked_by_group_decision=closed[0]['id'],review_reason='A prior offer decision cannot be matched safely; this source requires review before changing the offer.')
        item.pop('alternative_group',None)
        item.pop('alternative_group_evidence',None)
        return item

    def _save_proposal_groups(self, message, normalized):
        from .core import digest, utcnow
        groups = {}
        for item in normalized:
            if item.get('alternative_group'):
                groups.setdefault(item['alternative_group'], []).append(item)
        stamp = utcnow()
        for key, items in groups.items():
            # The source/key identity is stable; a closed decision cannot acquire
            # changed members or be reopened by a later model invocation.
            group_id = digest(['alternative-group', message['id'], message['payload_hash'], key])[:32]
            existing = self.db.execute('SELECT * FROM proposal_groups WHERE id=?', (group_id,)).fetchone()
            member_ids = [item['id'] for item in items]
            prior_ids = [row[0] for row in self.db.execute('SELECT proposal_id FROM proposal_group_members WHERE group_id=? ORDER BY position', (group_id,))]
            if existing and prior_ids != member_ids:
                if existing['status'] != 'open':
                    raise ValueError('A closed alternative group is immutable.')
                # Explicit cached reanalysis may replace an untouched open set.
                self.db.execute('DELETE FROM proposal_group_members WHERE group_id=?', (group_id,))
                self.db.execute('UPDATE proposal_groups SET revision=revision+1,source_evidence=?,updated_at=? WHERE id=?',
                    (items[0]['alternative_group_evidence'], stamp, group_id))
            self.db.execute("INSERT OR IGNORE INTO proposal_groups VALUES (?,?,?,?,?,'open',NULL,1,?,?)",
                (group_id, message['id'], message['payload_hash'], key, items[0]['alternative_group_evidence'], stamp, stamp))
            offer = items[0].get('offer_identity')
            if isinstance(offer,dict):
                label = _compact(offer.get('label')).casefold()
                evidence = _compact(offer.get('evidence'))
                if len(label)>=6 and evidence and evidence in _compact(message.get('text')) and label in evidence.casefold():
                    # Only a literal participation-offer identity may connect
                    # groups across sources. It never transfers a selected date.
                    offer_key=digest(['source-offer',self._setting('source_learner_id',''),label,(items[0].get('action_identity') or {}).get('occasion','')])
                    self.db.execute('INSERT OR IGNORE INTO canonical_offer_groups VALUES (?,?)',(group_id,offer_key))
            for position, item in enumerate(items):
                prior = self.db.execute('SELECT group_id FROM proposal_group_members WHERE proposal_id=?', (item['id'],)).fetchone()
                if prior and prior[0] != group_id:
                    raise ValueError('A proposal already belongs to a different alternative group.')
                self.db.execute('INSERT OR IGNORE INTO proposal_group_members VALUES (?,?,?)', (group_id, item['id'], position))

    def _group_write_reason(self, proposal_id):
        proposal = self.db.execute('SELECT payload FROM proposals WHERE id=?',(str(proposal_id),)).fetchone()
        if proposal and json.loads(proposal['payload']).get('blocked_by_offer_group'):
            return 'Existing offer membership must be reconciled before this option can write.'
        if proposal and json.loads(proposal['payload']).get('blocked_by_group_decision'):
            return 'A prior alternative decision blocks this ambiguous replacement.'
        row = self.db.execute('''SELECT g.* FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id
                               WHERE m.proposal_id=?''', (str(proposal_id),)).fetchone()
        if not row:
            return 'Complete alternative membership must be reconciled before writing.' if proposal and json.loads(proposal['payload']).get('alternative_group') else None
        message = self.db.execute('SELECT payload_hash,status FROM messages WHERE id=?', (row['message_id'],)).fetchone()
        if not message or message['payload_hash'] != row['source_hash'] or message['status'] == 'retained':
            return 'Alternative source is unavailable or changed; review required.'
        if row['status'] != 'selected' or row['selected_proposal_id'] != str(proposal_id):
            return 'Choose one alternative through its complete offer group before writing.'
        offer=self.db.execute('SELECT offer_key FROM canonical_offer_groups WHERE group_id=?',(row['id'],)).fetchone()
        if offer:
            related=[r[0] for r in self.db.execute('SELECT group_id FROM canonical_offer_groups WHERE offer_key=? AND group_id<>?',(offer[0],row['id']))]
            for other_id in related:
                other=self.db.execute('SELECT status,selected_proposal_id FROM proposal_groups WHERE id=?',(other_id,)).fetchone()
                if other and other['status'] in ('selected','rejected'):
                    return 'A cross-source offer already has a human decision; reconcile the complete offer before another write.'
                if self.db.execute("SELECT 1 FROM operations o JOIN proposal_group_members m ON m.proposal_id=o.proposal_id WHERE m.group_id=? AND (o.attempts>0 OR o.status IN ('inflight','unknown','applied')) LIMIT 1",(other_id,)).fetchone():
                    return 'Another cross-source alternative has write evidence; no second offer event is permitted.'
        # Corrupted/injected local statuses cannot queue a second member.
        conflict = self.db.execute('''SELECT 1 FROM operations o JOIN proposal_group_members m ON m.proposal_id=o.proposal_id
            WHERE m.group_id=? AND m.proposal_id<>? AND (o.attempts>0 OR o.status IN ('inflight','unknown','applied')) LIMIT 1''',
            (row['id'], str(proposal_id))).fetchone()
        return 'Another alternative has external write evidence; review required.' if conflict else None

    def _group_view(self, row, current=None):
        if row is None:
            return None
        from .core import normalize_proposal
        result = dict(row)
        message = self.db.execute('SELECT payload_hash,status FROM messages WHERE id=?', (row['message_id'],)).fetchone()
        result['stale'] = not message or message['payload_hash'] != row['source_hash'] or message['status'] == 'retained'
        result['members'] = []
        for member in self.db.execute('''SELECT p.* FROM proposals p JOIN proposal_group_members m ON m.proposal_id=p.id
                                       WHERE m.group_id=? ORDER BY m.position,p.id''', (row['id'],)):
            item = self._proposal(member)
            reason = ''
            if result['stale']:
                reason = 'stale'
            elif row['status'] != 'open':
                reason = 'closed'
            else:
                try:
                    checked = normalize_proposal({**item, 'user_approved': True, 'confidence': 'high', 'needs_review': False}, current)
                    if checked['status'] == 'ignored':
                        reason = 'past'
                    elif item['status'] not in ('review', 'pending'):
                        reason = 'unavailable'
                    elif checked['status'] != 'pending':
                        reason = 'invalid'
                except (ValueError, TypeError, OverflowError):
                    reason = 'invalid'
            item.update(eligible=not bool(reason), ineligible_reason=reason)
            result['members'].append(item)
        from .core import as_datetime
        def chronology(item):
            try:
                return (0, as_datetime(item['start']).timestamp(), item['id'])
            except (ValueError,TypeError,KeyError):
                return (1,0,item['id'])
        result['members'].sort(key=chronology)
        return result

    def list_proposal_groups(self, message_id=None, status=None, current=None):
        if status is not None and status not in ('open', 'selected', 'rejected'):
            raise ValueError('Unsupported group status.')
        clauses, params = [], []
        for key, value in (('message_id', message_id), ('status', status)):
            if value is not None:
                clauses.append(key + '=?')
                params.append(str(value))
        sql = 'SELECT * FROM proposal_groups' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY created_at DESC,id'
        with self.lock, self._group_snapshot():
            return [self._group_view(row, current) for row in self.db.execute(sql, params).fetchall()]

    def get_proposal_group(self, group_id, current=None):
        with self.lock, self._group_snapshot():
            return self._group_view(self.db.execute('SELECT * FROM proposal_groups WHERE id=?', (str(group_id),)).fetchone(), current)

    def get_proposal_group_for_proposal(self, proposal_id, current=None):
        with self.lock, self._group_snapshot():
            row = self.db.execute('''SELECT g.* FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id
                                     WHERE m.proposal_id=?''', (str(proposal_id),)).fetchone()
            return self._group_view(row, current)

    def resolve_proposal_group(self, group_id, decision, proposal_id=None, expected_revision=None, current=None):
        from .core import normalize_proposal, utcnow
        from .data_admin import remember_decision
        if decision not in ('select', 'reject') or (decision == 'reject' and proposal_id is not None):
            raise ValueError('Group decision must select one member or reject all.')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM proposal_groups WHERE id=?', (str(group_id),)).fetchone()
            if not row:
                raise ValueError('Unknown alternative group.')
            wanted = 'selected' if decision == 'select' else 'rejected'
            if row['status'] != 'open':
                if row['status'] == wanted and row['selected_proposal_id'] == proposal_id:
                    return self._group_view(row, current)
                raise ValueError('This group already has a conflicting decision.')
            if type(expected_revision) is not int or expected_revision != row['revision']:
                raise ValueError('Offer revision changed; reload the complete group.')
            view = self._group_view(row, current)
            if view['stale']:
                raise ValueError('Alternative source changed or is unavailable.')
            members = self.db.execute('''SELECT p.* FROM proposals p JOIN proposal_group_members m ON m.proposal_id=p.id
                                        WHERE m.group_id=? ORDER BY m.position''', (row['id'],)).fetchall()
            if len(members) < 2 or any(member['source_hash'] != row['source_hash'] or member['message_id'] != row['message_id'] for member in members):
                raise ValueError('Alternative membership evidence is incomplete.')
            if self.db.execute('''SELECT 1 FROM operations o JOIN proposal_group_members m ON m.proposal_id=o.proposal_id
                WHERE m.group_id=? AND (o.attempts>0 OR o.result IS NOT NULL OR o.status NOT IN ('pending','review')) LIMIT 1''', (row['id'],)).fetchone():
                raise ValueError('An alternative already has external write evidence.')
            if self.db.execute('''SELECT 1 FROM events e JOIN proposal_group_members m ON m.proposal_id=e.proposal_id
                                  WHERE m.group_id=? LIMIT 1''', (row['id'],)).fetchone():
                raise ValueError('An alternative already has calendar history.')
            selected = next((item for item in view['members'] if item['id'] == proposal_id), None)
            if decision == 'select' and (not selected or not selected['eligible']):
                raise ValueError('Select an eligible future member of this group.')
            stamp = utcnow()
            for member in members:
                original = self._proposal(member)
                if any(original.get(flag) for flag in ('user_approved','user_rejected','user_edited','deletion_approved')):
                    raise ValueError('An alternative already carries an independent human decision.')
                item = dict(original)
                if decision == 'select' and member['id'] == proposal_id:
                    item = {**normalize_proposal({**item, 'confidence':'high','needs_review':False,'review_reason':'','user_approved':True}, current),
                            'id':member['id'],'message_id':member['message_id']}
                    if item['status'] != 'pending':
                        raise ValueError('The selected alternative is no longer in the future.')
                else:
                    item.update(status='ignored', user_rejected=True, needs_review=False,
                                review_reason='Another alternative was chosen.' if decision == 'select' else 'Entire offer rejected by the user.')
                item.update(alternative_group_id=row['id'])
                self.db.execute('DELETE FROM operations WHERE proposal_id=? AND attempts=0 AND status IN (\'pending\',\'review\')', (member['id'],))
                self.db.execute('UPDATE proposals SET payload=?,status=?,updated_at=?,pending_since=? WHERE id=?',
                    (json.dumps(item, ensure_ascii=False),item['status'],stamp,stamp if item['status']=='pending' else None,member['id']))
                remember_decision(self, member, item, original=original)
            self.db.execute('UPDATE proposal_groups SET status=?,selected_proposal_id=?,revision=revision+1,updated_at=? WHERE id=?',
                (wanted,proposal_id,stamp,row['id']))
            return self.get_proposal_group(row['id'], current)


def _legacy_scope(store, message_id, proposal_ids, evidence=None):
    from .core import digest
    if not isinstance(proposal_ids, list) or not 2 <= len(proposal_ids) <= 30 or len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError('Select two to thirty unique explicit proposal IDs.')
    message = store.get_message(message_id)
    if not message or not message.get('payload_hash') or message['status'] == 'retained':
        raise ValueError('A complete current source message is required.')
    if store.db.execute('SELECT 1 FROM events WHERE message_id=?', (str(message_id),)).fetchone():
        raise ValueError('Existing grouping refuses calendar history.')
    if store.db.execute('SELECT 1 FROM decision_intents WHERE message_id=?', (str(message_id),)).fetchone():
        raise ValueError('Existing grouping refuses human decisions.')
    if evidence is None:
        evidence = next((part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', message.get('text','')) if CHOICE.search(part) and 6 <= len(part.strip()) <= 4000), None)
    rows = []
    for proposal_id in proposal_ids:
        row = store.db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row or row['message_id'] != str(message_id) or row['source_hash'] != message['payload_hash'] or row['status'] != 'review':
            raise ValueError('Existing grouping requires current untouched review proposals from one source.')
        item = store._proposal(row)
        if any(item.get(flag) for flag in ('user_approved','user_edited','user_rejected','deletion_approved')):
            raise ValueError('Existing grouping refuses human decisions.')
        if store.db.execute('SELECT 1 FROM operations WHERE proposal_id=?', (proposal_id,)).fetchone():
            raise ValueError('Existing grouping requires proposals without operation journals.')
        rows.append(dict(row))
    # Explicit operator request confirms that these are alternatives. Source
    # evidence must still match exactly; no title/date similarity inference.
    if not isinstance(evidence, str) or not 6 <= len(evidence) <= 4000 or _compact(evidence) not in _compact(message.get('text')):
        raise ValueError('Existing grouping requires matching source evidence.')
    items = [json.loads(row['payload']) for row in rows]
    from .core import start_key
    starts = [start_key(item) for item in items]
    if (None in starts or len(set(starts)) != len(items) or any(item.get('kind') != 'create' or item.get('temporal_kind','event') != 'event' or item.get('activity_scope') != 'extracurricular' or item.get('event_id') for item in items)):
        raise ValueError('Existing grouping requires distinct extracurricular event choices.')
    return {'message_id':str(message_id), 'source_hash':message['payload_hash'], 'source_evidence':evidence,
            'proposal_ids':proposal_ids, 'proposal_fingerprints':[digest(row) for row in rows]}


def read_private_json(path):
    path = Path(path).expanduser()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 100000:
            raise ValueError('Grouping request must be an owned private regular file.')
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('Grouping request must be an object.')
    return value


def group_existing(store, *, plan_path, message_id=None, proposal_ids=None, source_evidence=None, apply=False):
    """Bounded private preview/apply; caller holds sync.lock, never calls Calendar."""
    from .core import digest, utcnow
    path = Path(plan_path).expanduser()
    if apply:
        plan = read_private_json(path)
        if not isinstance(plan, dict) or plan.get('version') != 1 or plan.get('database') != str(store.path.resolve()):
            raise ValueError('Grouping plan does not match this database.')
        scope = plan['scope']
        expected_id = digest(['alternative-group',scope['message_id'],scope['source_hash'],'legacy-' + digest(scope['proposal_ids'])[:16]])[:32]
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            existing = store.get_proposal_group(expected_id)
            if existing:
                message = store.get_message(scope['message_id'])
                if not message or message.get('payload_hash') != scope['source_hash'] or message['status'] == 'retained':
                    raise ValueError('Grouping plan source is no longer current.')
                if existing['status'] != 'open':
                    raise ValueError('A human group decision changed the saved grouping plan state.')
                if _legacy_scope(store,scope['message_id'],scope['proposal_ids'],scope['source_evidence']) != scope:
                    raise ValueError('Grouped proposal fingerprints changed after preview.')
                members = [row[0] for row in store.db.execute('SELECT proposal_id FROM proposal_group_members WHERE group_id=? ORDER BY position',(expected_id,))]
                if members != scope['proposal_ids'] or existing['source_hash'] != scope['source_hash']:
                    raise ValueError('Existing alternative group conflicts with the plan.')
                return {'status':'already_applied','groups':1,'members':len(members),'calendar_writes':0}
            actual = _legacy_scope(store,scope['message_id'],scope['proposal_ids'],scope['source_evidence'])
            if actual != scope:
                raise ValueError('Source or proposal fingerprints changed after preview.')
            stamp = utcnow()
            store.db.execute("INSERT INTO proposal_groups VALUES (?,?,?,?,?,'open',NULL,1,?,?)",
                (expected_id,scope['message_id'],scope['source_hash'],'legacy-' + digest(scope['proposal_ids'])[:16],scope['source_evidence'],stamp,stamp))
            for position, proposal_id in enumerate(scope['proposal_ids']):
                store.db.execute('INSERT INTO proposal_group_members VALUES (?,?,?)', (expected_id,proposal_id,position))
        return {'status':'applied','groups':1,'members':len(scope['proposal_ids']),'calendar_writes':0}
    with store.lock, store._group_snapshot():
        scope = _legacy_scope(store,message_id,proposal_ids,source_evidence)
        if any(store.get_proposal_group_for_proposal(pid) for pid in proposal_ids):
            raise ValueError('A selected proposal already belongs to a group.')
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError('Grouping plan requires an owned private directory.')
    plan = {'version':1,'database':str(store.path.resolve()),'scope':scope,'created_at':utcnow()}
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(plan,stream,ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    return {'status':'preview','groups':1,'members':len(proposal_ids),'calendar_writes':0,'private_plan':str(path)}
