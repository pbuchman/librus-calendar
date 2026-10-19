"""Versioned sources, canonical obligations and an isolated preview journal.

Calendar mutations continue to use the original immutable operation journal.
These tables are additive: no existing proposal, offer, event or operation IDs move.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import uuid
from .source_preview import (SOURCE_PREVIEW_PLANNER_VERSION, PREVIEW_RETRY_SETTINGS,
                             current_preview_sql, preview_is_current, preview_queue_sql)

KINDS = frozenset({'message', 'announcement', 'school_event', 'substitution'})
CORROBORATION_RELATION = 'corroborates_unverified_recipient'
EXPIRED_FACT_RELATION = 'expired_unverified_fact'
CLOSED_OFFER_RELATION = 'retains_closed_offer_decision'
NON_AUTHORITY_RELATIONS = frozenset({CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION})


def authority_links(links):
    """Corroboration records visibility only, never a new identity authority."""
    return [link for link in links if link.get('relation')not in NON_AUTHORITY_RELATIONS]
SOURCE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS agenda_plans(id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,payload TEXT NOT NULL,metadata TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,error_code TEXT);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,kind TEXT NOT NULL,upstream_id TEXT NOT NULL,learner_id TEXT NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,published_at TEXT,updated_at TEXT,fetched_at TEXT NOT NULL,analyzed_at TEXT,last_error TEXT,outcome TEXT,reason TEXT);
CREATE INDEX IF NOT EXISTS sources_pending ON sources(status,kind,fetched_at);
CREATE TABLE IF NOT EXISTS source_health(kind TEXT PRIMARY KEY,last_attempt_at TEXT,last_successful_fetch TEXT,last_result TEXT,last_error TEXT,learner_verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS canonical_actions(id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS canonical_actions_proposal ON canonical_actions(proposal_id);
CREATE TABLE IF NOT EXISTS canonical_decisions(action_id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS canonical_offer_groups(group_id TEXT PRIMARY KEY,offer_key TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS canonical_offer_key ON canonical_offer_groups(offer_key);
CREATE TABLE IF NOT EXISTS action_sources(action_id TEXT NOT NULL,source_id TEXT NOT NULL,source_hash TEXT NOT NULL,source_quote TEXT NOT NULL,relation TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(action_id,source_id));
CREATE INDEX IF NOT EXISTS action_sources_source ON action_sources(source_id);
CREATE TABLE IF NOT EXISTS source_previews(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,last_error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS source_preview_version ON source_previews(source_id,source_hash);
CREATE TABLE IF NOT EXISTS source_analysis_audit(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_hash TEXT NOT NULL,analyzed_at TEXT NOT NULL,mode TEXT NOT NULL,metadata TEXT NOT NULL,outcomes TEXT NOT NULL);
'''


def normalized_text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def source_identity(kind, upstream_id, learner_id=''):
    from .core import digest
    if kind not in KINDS or not str(upstream_id).strip():
        raise ValueError('Stable source identity required.')
    # Messages historically have account scoped IDs. Keep their public source ID
    # readable, while other source identities include the observed learner.
    return ('message:' + str(upstream_id)) if kind == 'message' else kind + ':' + digest([str(learner_id), str(upstream_id)])[:32]


def source_message(source):
    """Private projection for the existing guarded proposal/Calendar pipeline."""
    return {'id': source['id'], 'subject': source.get('title', ''), 'sender': source.get('sender', ''),
            'sent_at': source.get('published_at'), 'text': source.get('text', ''), 'attachment_count': 0,
            'source_kind': source['kind'], 'source_id': source['id']}


class SourceStoreMixin:
    def latest_agenda_plan(self, preview=False):
        with self.lock:
            row = self.db.execute("SELECT * FROM agenda_plans WHERE status " + ("IN ('preview','preview_failed')" if preview else "NOT IN ('preview','preview_failed')") + " ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            return {**dict(row), 'payload': json.loads(row['payload']), 'metadata': json.loads(row['metadata'])} if row else None

    def agenda_status(self):
        result = {'engine': 'simple-agenda'}
        for name, preview in (('latest_plan', False), ('latest_preview', True)):
            row = self.latest_agenda_plan(preview)
            result[name] = {key: row[key] for key in ('id','input_hash','status','created_at','error_code','metadata')} if row else None
        return result

    def _initialize_sources(self):
        from .core import utcnow
        self.db.executescript(SOURCE_SCHEMA)
        if 'content_since' not in {r[1] for r in self.db.execute('PRAGMA table_info(sources)')}:
            self.db.execute('ALTER TABLE sources ADD COLUMN content_since TEXT')
        self.db.execute('UPDATE sources SET content_since=COALESCE(content_since,fetched_at)')
        # A migration records ownership only. It never reinterprets old payloads,
        # changes IDs or makes an old proposal executable.
        for row in self.db.execute("SELECT * FROM messages WHERE payload_hash IS NOT NULL").fetchall():
            self._mirror_message_source(row)
        for row in self.db.execute('SELECT * FROM proposals').fetchall():
            self._register_legacy_action(row)
            payload = json.loads(row['payload'])
            aid = payload.get('canonical_action_id') or row['id']
            if not self.db.execute('SELECT 1 FROM canonical_actions WHERE id=? OR proposal_id=?', (aid, row['id'])).fetchone():
                # Unmapped legacy state stays visible inventory, preserving its
                # ID and flags even after source text expired. No Calendar write.
                payload.update(canonical_action_id=aid, inventory_only=True)
                self.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?)',
                                (aid,row['id'],json.dumps(payload,ensure_ascii=False),row['status'],row['updated_at']))
        for event in self.db.execute('SELECT * FROM events').fetchall():
            if self.db.execute('SELECT 1 FROM canonical_actions WHERE proposal_id=?',(event['proposal_id'],)).fetchone():continue
            if self.db.execute("SELECT 1 FROM canonical_actions WHERE id=? OR json_extract(payload,'$.event_id')=?", (event['proposal_id'], event['id'])).fetchone():continue
            proposal = self.db.execute('SELECT payload FROM proposals WHERE id=?', (event['proposal_id'],)).fetchone()
            if proposal and self.db.execute('SELECT 1 FROM canonical_actions WHERE id=?', (json.loads(proposal[0]).get('canonical_action_id'),)).fetchone():continue
            from .codex_runtime import normalize_event
            snapshot=normalize_event(json.loads(event['snapshot']),self.config.calendar_owner)
            start=snapshot.get('start',{});end=snapshot.get('end',{})
            item={'id':event['proposal_id'],'message_id':event['message_id'],'kind':'create','title':snapshot.get('summary',''),
                  'description':'','start':start.get('date') or start.get('dateTime'),'end':end.get('date') or end.get('dateTime'),
                  'all_day':bool(start.get('date')),'temporal_kind':'event','activity_scope':'unknown','source_quote':'',
                  'status':'applied','event_id':event['id'],'inventory_only':True,'canonical_action_id':event['proposal_id']}
            self.db.execute('INSERT OR IGNORE INTO canonical_actions VALUES (?,?,?,?,?)',(event['proposal_id'],event['proposal_id'],json.dumps(item,ensure_ascii=False),'applied',event['updated_at']))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('source_schema_version','1')")

    def _mirror_message_source(self, row):
        from .core import utcnow
        data = json.loads(row['payload'])
        if self.db.execute("SELECT 1 FROM sources WHERE id=? AND kind<>'message'",(row['id'],)).fetchone():
            return
        sid = source_identity('message', row['id'])
        payload = {'id': sid, 'kind': 'message', 'upstream_id': row['id'], 'learner_id': self._setting('source_learner_id',''),
                   'title': data.get('subject', ''), 'sender': data.get('sender', ''),
                   'published_at': data.get('sent_at'), 'updated_at': None, 'text': data.get('text', ''),
                   'url': '', 'metadata': {}, 'deleted': False}
        state = {'analysis_pending':'pending','analysis_failed':'failed','retained':'retained'}.get(row['status'], 'analyzed')
        self.db.execute('INSERT INTO sources(id,kind,upstream_id,learner_id,payload,content_hash,status,published_at,updated_at,fetched_at,analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,content_hash=excluded.content_hash,status=CASE WHEN sources.content_hash<>excluded.content_hash THEN excluded.status ELSE sources.status END,fetched_at=excluded.fetched_at',
            (sid, 'message', row['id'], self._setting('source_learner_id',''), json.dumps(payload, ensure_ascii=False), row['payload_hash'], state,
             row['sent_at'], None, row['fetched_at'] or utcnow(), row['analyzed_at']))
        self.db.execute('UPDATE sources SET content_since=COALESCE(content_since,?) WHERE id=?',(row['fetched_at'] or utcnow(),sid))

    def _register_legacy_action(self, row):
        from .core import utcnow
        item = self._proposal(row)
        if item.get('canonical_action_id'):
            return
        aid = item['id']
        source = self.db.execute('SELECT id,content_hash,published_at,fetched_at FROM sources WHERE kind=? AND upstream_id=?', ('message', row['message_id'])).fetchone()
        if not source:
            return
        item.update(canonical_action_id=aid,source_id=source['id'],source_hash=source['content_hash'],source_kind='message',source_version_at=source['published_at'])
        self.db.execute('INSERT OR IGNORE INTO canonical_actions VALUES (?,?,?,?,?)', (aid,aid,json.dumps(item,ensure_ascii=False),row['status'],row['updated_at']))
        self.db.execute('INSERT OR IGNORE INTO action_sources VALUES (?,?,?,?,?,?)', (aid,source['id'],source['content_hash'],item.get('source_quote',''),'origin',utcnow()))

    def _source(self, row):
        result = json.loads(row['payload'])
        result.update({key: row[key] for key in ('id','kind','upstream_id','learner_id','content_hash','status','published_at','updated_at','fetched_at','analyzed_at','last_error','outcome','reason')})
        result['deleted'] = bool(row['deleted'])
        result['action_ids'] = [r[0] for r in self.db.execute('SELECT action_id FROM action_sources WHERE source_id=? ORDER BY action_id', (row['id'],))]
        return result

    def list_sources(self, limit=100, offset=0, kind=None, status=None):
        clauses, args = [], []
        for key, value in (('kind',kind), ('status',status)):
            if value is not None:
                clauses.append(key+'=?'); args.append(value)
        if not 1 <= int(limit) <= 500 or int(offset) < 0:
            raise ValueError('Invalid source page.')
        sql = 'SELECT * FROM sources' + (' WHERE '+' AND '.join(clauses) if clauses else '') + ' ORDER BY fetched_at DESC,id LIMIT ? OFFSET ?'
        with self.lock:
            return [self._source(row) for row in self.db.execute(sql, (*args,int(limit),int(offset)))]

    def get_source(self, source_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM sources WHERE id=?', (str(source_id),)).fetchone()
            return self._source(row) if row else None

    def source_status(self):
        counts = {}
        for row in self.db.execute("SELECT kind,COUNT(*) AS total,SUM(status='pending') AS pending,SUM(status='failed') AS failed,SUM(deleted) AS deleted FROM sources GROUP BY kind"):
            counts[row['kind']] = {key: row[key] for key in ('total','pending','failed','deleted')}
            counts[row['kind']].update(active_pending=row['pending'] + row['failed'], preview_ready=0, preview_failed=0, preview_pending=0)
        health = {row['kind']: {**dict(row), 'learner_verified': bool(row['learner_verified'])} for row in self.db.execute('SELECT * FROM source_health')}
        return {'mode': 'active', 'engine': 'simple-agenda', 'counts': counts, 'health': health,
                'active_pending': sum(row['active_pending'] for row in counts.values()),
                'preview_pending': 0, 'preview_failed': 0, 'preview_count': 0,
                'last_collection_at': self._setting('source_last_collection_at'),
                'last_collection_result': self._setting('source_last_collection_result'),
                'last_error': self._setting('source_last_error'),
                'learner_verified': self._setting('source_learner_verified') == 'true'}

    def ingest_sources(self, batch, current=None):
        """Accept only the selected learner; incomplete collection never deletes rows."""
        from .core import digest, utcnow
        if not isinstance(batch,dict) or not isinstance(batch.get('sources'),list) or len(batch['sources'])>5000:
            raise ValueError('Invalid bounded source collection.')
        learner = batch.get('learner') or {}
        learner_id = str(learner.get('id') or '')
        class_id = str(learner.get('class_id') or '')
        expected = self.config.learner_id or self._setting('source_learner_id')
        expected_class = self.config.class_id
        if not learner_id or (expected and learner_id != expected) or (expected_class and class_id != expected_class):
            raise ValueError('Selected learner context could not be verified.')
        # Validate every row before persisting any source. Observed context is a
        # pin, never permission to switch to a different learner on a later run.
        clean = []
        seen = set()
        for data in batch['sources']:
            kind = data.get('kind') if isinstance(data,dict) else None
            if kind not in KINDS-{'message'} or str(data.get('learner_id') or '') != learner_id:
                raise ValueError('Source learner or kind mismatch.')
            upstream = str(data.get('upstream_id') or '').strip()
            sid = source_identity(kind,upstream,learner_id)
            if sid in seen:
                raise ValueError('Duplicate source identity.')
            seen.add(sid)
            payload = {key:data.get(key) for key in ('title','sender','published_at','updated_at','text','url','metadata')}
            payload.update(id=sid,kind=kind,upstream_id=upstream,learner_id=learner_id,deleted=data.get('deleted') is True)
            for field in ('title','sender','text','url'):
                payload[field] = str(payload.get(field) or '')
            if len(payload['text'])>100000 or not isinstance(payload['metadata'],dict):
                raise ValueError('Invalid source body.')
            from .source_audience import AUDIENCE_KEY,valid_calendar_audience
            if AUDIENCE_KEY in payload['metadata']and not valid_calendar_audience(payload,learner):
                raise ValueError('Invalid observed calendar audience proof.')
            # Fetch timestamps / pagination positions are observation metadata,
            # not a content edit that warrants another model invocation.
            hashed_payload = {k:v for k,v in payload.items() if k!='metadata'}
            hashed_payload['metadata'] = {k:v for k,v in payload['metadata'].items() if k not in {'fetched_at','page','window_start','window_end'}}
            clean.append((payload,digest(hashed_payload)))
        stamp = current.astimezone(timezone.utc).isoformat() if current else utcnow()
        changed = 0
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if not self._setting('source_learner_id'):
                # Preserve legacy message/action identities while binding their
                # source context once to the verified active learner. Typed
                # recipient analysis still decides whether each obligation applies.
                self.db.execute("UPDATE sources SET learner_id=? WHERE kind='message' AND learner_id=''",(learner_id,))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('source_learner_id',?)",(learner_id,))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('source_learner_context',?)",(json.dumps(learner,ensure_ascii=False),))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('source_learner_verified','true')")
            self.db.execute("UPDATE source_health SET learner_verified=1 WHERE kind='message'")
            for payload,hashed in clean:
                row = self.db.execute('SELECT content_hash,status,payload FROM sources WHERE id=?',(payload['id'],)).fetchone()
                is_changed = not row or row['content_hash'] != hashed
                state = 'deleted' if payload['deleted'] else ('pending' if is_changed else row['status'])
                # An unchanged source whose retention elapsed must not bring its
                # expired text back merely because a listing still exposes it.
                if row and not is_changed and row['status']=='retained':
                    payload = json.loads(row['payload'])
                self.db.execute('INSERT INTO sources(id,kind,upstream_id,learner_id,payload,content_hash,status,deleted,published_at,updated_at,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,content_hash=excluded.content_hash,status=excluded.status,deleted=excluded.deleted,published_at=excluded.published_at,updated_at=excluded.updated_at,fetched_at=excluded.fetched_at,last_error=NULL',
                    (payload['id'],payload['kind'],payload['upstream_id'],learner_id,json.dumps(payload,ensure_ascii=False),hashed,state,int(payload['deleted']),payload.get('published_at'),payload.get('updated_at'),stamp))
                if is_changed:
                    self.db.execute('UPDATE sources SET content_since=? WHERE id=?',(stamp,payload['id']))
                    changed += 1
                    self.db.execute("UPDATE source_previews SET status='stale' WHERE source_id=? AND source_hash<>? AND status='ready'",(payload['id'],hashed))
                    # Deletion is evidence requiring review, never an instruction
                    # to delete a Calendar event.
                    if payload['deleted']:
                        self.db.execute("UPDATE sources SET outcome='review',reason='Source removed; linked Calendar facts require review.' WHERE id=?",(payload['id'],))
                        for action in self.db.execute('SELECT a.proposal_id FROM canonical_actions a JOIN action_sources s ON s.action_id=a.id WHERE s.source_id=? AND s.relation NOT IN (?,?,?)',(payload['id'],CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION)).fetchall():
                            prow=self.db.execute('SELECT * FROM proposals WHERE id=?',(action[0],)).fetchone()
                            if prow and prow['status']=='pending':
                                self._review_unwritten_locked(self._proposal(prow),'Supporting source removed; explicit review required.')
            complete=set(batch.get('complete_kinds') or ())
            # Absence is meaningful only in an explicitly complete observed
            # scope. Announcements are all-current; Calendar scans only their
            # bounded event-date window. Never infer a Calendar cancellation.
            window_start=batch.get('window_start')
            window_end=batch.get('window_end')
            for previous in self.db.execute("SELECT * FROM sources WHERE kind<>'message' AND learner_id=? AND deleted=0",(learner_id,)).fetchall():
                if previous['id'] in seen or previous['kind'] not in complete:continue
                previous_payload=json.loads(previous['payload'])
                in_scope=previous['kind']=='announcement'
                if previous['kind'] in {'school_event','substitution'} and window_start and window_end:
                    event_day=str(previous_payload.get('metadata',{}).get('event_start') or '')[:10]
                    in_scope=bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',event_day) and window_start<=event_day<=window_end)
                if not in_scope:continue
                previous_payload['deleted']=True
                self.db.execute("UPDATE sources SET deleted=1,status='deleted',payload=?,outcome='review',reason='Source unavailable in a complete current snapshot; no Calendar cancellation inferred.' WHERE id=?",(json.dumps(previous_payload,ensure_ascii=False),previous['id']))
                self.db.execute("UPDATE source_previews SET status='stale' WHERE source_id=? AND status='ready'",(previous['id'],))
                for action in self.db.execute('SELECT c.* FROM canonical_actions c JOIN action_sources a ON a.action_id=c.id WHERE a.source_id=? AND a.relation NOT IN (?,?,?)',(previous['id'],CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION)).fetchall():
                    # Another current literal source can still support the
                    # obligation; disappearing one copy does not erase it.
                    alternative=self.db.execute("SELECT s.* FROM sources s JOIN action_sources a ON a.source_id=s.id AND a.source_hash=s.content_hash WHERE a.action_id=? AND a.relation NOT IN (?,?,?) AND s.deleted=0 AND s.id<>? AND s.status IN ('analyzed','retained') ORDER BY s.published_at DESC LIMIT 1",(action['id'],CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION,previous['id'])).fetchone()
                    proposal=self.db.execute('SELECT * FROM proposals WHERE id=?',(action['proposal_id'],)).fetchone()
                    if not proposal or proposal['status']!='pending':continue
                    item=self._proposal(proposal)
                    if alternative:
                        item.update(source_id=alternative['id'],source_hash=alternative['content_hash'],source_kind=alternative['kind'])
                        self.db.execute('UPDATE proposals SET payload=? WHERE id=?',(json.dumps(item,ensure_ascii=False),proposal['id']))
                        self.db.execute('UPDATE canonical_actions SET payload=? WHERE id=?',(json.dumps(item,ensure_ascii=False),action['id']))
                    else:self._review_unwritten_locked(item,'Supporting source unavailable in a complete snapshot; explicit review required.')
            for kind in KINDS-{'message'}:
                success=kind in complete
                self.db.execute('INSERT INTO source_health VALUES (?,?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,last_successful_fetch=COALESCE(excluded.last_successful_fetch,source_health.last_successful_fetch),last_result=excluded.last_result,last_error=excluded.last_error,learner_verified=excluded.learner_verified',
                    (kind,stamp,stamp if success else None,'ok' if success else 'partial',None if success else 'source_collection_incomplete',1))
            for key,value in (('source_last_collection_at',stamp),('source_last_collection_result','ok' if batch.get('status')=='ok' and complete==KINDS-{'message'} else 'partial'),('source_last_error','' if batch.get('status')=='ok' else 'source_collection_incomplete')):
                self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,value))
        return {'changed':changed,'collected':len(clean),'status':self._setting('source_last_collection_result')}

    def pending_sources(self, limit=100, include_messages=False, preview=False):
        queue,args=preview_queue_sql({key:self._setting(key)for key in PREVIEW_RETRY_SETTINGS}) if preview else ("s.status IN ('pending','failed')",())
        sql = f"SELECT * FROM sources s WHERE ({queue}) AND s.deleted=0"
        if not include_messages:
            sql += " AND s.kind<>'message'"
        if preview:
            sql += f" AND NOT EXISTS (SELECT 1 FROM source_previews p WHERE p.source_id=s.id AND p.source_hash=s.content_hash AND p.status='ready' AND ({current_preview_sql()}))"
        with self.lock:
            return [self._source(r) for r in self.db.execute(sql+' ORDER BY s.fetched_at,s.id LIMIT ?',(*args,min(int(limit),100)))]

    def fail_source(self, source_id, code='source_analysis_failed', preview=False):
        from .core import digest,utcnow
        with self.lock,self.db:
            source=self.get_source(source_id)
            if preview:
                pid=digest([source_id,source['content_hash']])[:32]
                self.db.execute('INSERT INTO source_previews VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status,created_at=excluded.created_at,last_error=excluded.last_error',
                    (pid,source_id,source['content_hash'],json.dumps({'planner_version':SOURCE_PREVIEW_PLANNER_VERSION}),'failed',utcnow(),code))
            else:
                self.db.execute("UPDATE sources SET status='failed',last_error=?,outcome='analysis_incomplete',reason=? WHERE id=?",(code,code,source_id))

    def save_source_preview(self, source_id, proposals, metadata=None, expected_hash=None, outcomes=None, expected_decisions=None):
        from .core import digest,utcnow
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            source=self.get_source(source_id)
            if not source or source['deleted'] or (expected_hash and source['content_hash']!=expected_hash):
                raise ValueError('Source changed during preview.')
            if expected_decisions and self.source_decision_snapshot()!=expected_decisions:
                raise ValueError('Human decisions changed during preview matching.')
            pid=digest([source_id,source['content_hash']])[:32]
            payload={'planner_version':SOURCE_PREVIEW_PLANNER_VERSION,'proposals':proposals,'analysis_metadata':metadata or {},'outcomes':outcomes or [],'decision_snapshot':expected_decisions}
            self.db.execute('INSERT INTO source_previews VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status,created_at=excluded.created_at,last_error=NULL',
                (pid,source_id,source['content_hash'],json.dumps(payload,ensure_ascii=False),'ready',utcnow(),None))
            return pid

    def list_source_previews(self,limit=100,offset=0,status=None):
        if not 1<=int(limit)<=500 or int(offset)<0:
            raise ValueError('Invalid preview page.')
        current=f"s.id IS NOT NULL AND s.content_hash=p.source_hash AND s.deleted=0 AND ({current_preview_sql()})"
        display_status=f"CASE WHEN p.status IN ('ready','failed') AND NOT ({current}) THEN 'stale' ELSE p.status END"
        sql=f'SELECT p.*,{display_status} AS display_status FROM source_previews p LEFT JOIN sources s ON s.id=p.source_id'
        sql+=(f' WHERE ({display_status})=?' if status else '')+' ORDER BY p.created_at DESC,p.id LIMIT ? OFFSET ?'
        args=(status,int(limit),int(offset)) if status else (int(limit),int(offset))
        with self.lock:
            result=[]
            for row in self.db.execute(sql,args):
                try:payload=json.loads(row['payload'])
                except (ValueError,TypeError):payload={}
                if not isinstance(payload,dict):payload={}
                item={**payload,**dict(row)};item['status']=item.pop('display_status')
                item['planner_current']=preview_is_current(payload)
                result.append(item)
            return result

    def get_action(self,action_id):
        with self.lock:
            row=self.db.execute('SELECT * FROM canonical_actions WHERE id=?',(str(action_id),)).fetchone()
            if not row:return None
            proposal=self.db.execute('SELECT * FROM proposals WHERE id=?',(row['proposal_id'],)).fetchone()
            item={**json.loads(row['payload']),**self._proposal(proposal)} if proposal else json.loads(row['payload'])
            decision=self.db.execute('SELECT payload FROM decision_intents WHERE id=?',(row['proposal_id'],)).fetchone()
            links=[dict(r) for r in self.db.execute('SELECT * FROM action_sources WHERE action_id=? ORDER BY source_id',(row['id'],))]
            event_ids={r[0] for r in self.db.execute('SELECT id FROM events WHERE proposal_id=?',(row['proposal_id'],))}
            if item.get('event_id'):event_ids.add(item['event_id'])
            return {'id':row['id'],'proposal_id':row['proposal_id'],'status':item.get('status',row['status']),'payload':item,'source_links':links,
                    'decision':json.loads((self.db.execute('SELECT payload FROM canonical_decisions WHERE action_id=?',(row['id'],)).fetchone() or decision)[0]) if (self.db.execute('SELECT 1 FROM canonical_decisions WHERE action_id=?',(row['id'],)).fetchone() or decision) else None,'event_ids':sorted(event_ids),'updated_at':row['updated_at']}

    def list_actions(self,limit=100,offset=0,status=None):
        if not 1<=int(limit)<=500 or int(offset)<0:raise ValueError('Invalid action page.')
        sql='SELECT id FROM canonical_actions'+(' WHERE status=?' if status else '')+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?'
        args=(status,int(limit),int(offset)) if status else (int(limit),int(offset))
        with self.lock:return [self.get_action(r[0]) for r in self.db.execute(sql,args).fetchall()]

    def source_decision_snapshot(self):
        from .core import digest
        # Matching authority includes the exact links and current source versions,
        # not fetch timestamps. A changed/deleted/retained candidate invalidates
        # both preview and active plans, even when its human decision is unchanged.
        state={table:[tuple(r) for r in self.db.execute('SELECT * FROM '+table+' ORDER BY 1')]
               for table in ('decision_intents','proposal_groups','proposal_group_members','events','canonical_actions','canonical_decisions','canonical_offer_groups','action_sources')}
        state['source_authority']=[tuple(r)for r in self.db.execute('SELECT id,content_hash,deleted,status,published_at,updated_at FROM sources ORDER BY id')]
        state['proposal_authority']=[tuple(r)for r in self.db.execute('SELECT id,message_id,source_hash,payload,status FROM proposals ORDER BY id')]
        state['selected_context']=self.source_context_snapshot()
        state['preview_planner_version']=SOURCE_PREVIEW_PLANNER_VERSION
        return digest(state)

    def source_context_snapshot(self):
        context=json.loads(self._setting('source_learner_context','{}'))
        return {'id':context.get('id'),'class_id':context.get('class_id'),
                'pinned_id':self._setting('source_learner_id'),'verified':self._setting('source_learner_verified')}

    def _source_write_reason(self, item):
        # The shared plan may deliberately omit legacy pending intents. Its
        # omission never adopts old automatic writes, including untagged rows.
        if (self._setting('agenda_baseline_hash') and not item.get('agenda_plan_id')
                and not item.get('user_approved')):
            return 'Legacy automatic intent requires current agenda review.'
        if not item.get('canonical_action_id'):
            return None
        uncertain = item.get('deferred_unknown_operation')
        if uncertain and self.db.execute("SELECT 1 FROM operations WHERE id=? AND status IN ('unknown','inflight')", (uncertain,)).fetchone():
            return 'Prior write must be reconciled before this change can execute.'
        if self._setting('source_learner_verified') != 'true':
            return 'Selected learner context is unverified.'
        action = self.db.execute('SELECT proposal_id FROM canonical_actions WHERE id=?', (item['canonical_action_id'],)).fetchone()
        if not action or action[0] != item['id']:
            return 'A newer agenda revision supersedes this proposal.'
        group_id = item.get('alternative_group_id')
        if group_id and not self.db.execute('SELECT 1 FROM proposal_group_members WHERE group_id=? AND proposal_id=?', (group_id, item['id'])).fetchone():
            selected = self.db.execute("SELECT p.id,p.payload FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id AND m.proposal_id=g.selected_proposal_id JOIN proposals p ON p.id=m.proposal_id WHERE g.id=? AND g.status='selected'", (group_id,)).fetchone()
            selected_aid = (json.loads(selected['payload']).get('canonical_action_id') or selected['id']) if selected else None
            owned = self.db.execute('SELECT e.proposal_id,p.payload FROM events e LEFT JOIN proposals p ON p.id=e.proposal_id WHERE e.id=? AND e.calendar_id=? AND e.state=?', (item.get('event_id'), self.calendar_id, 'active')).fetchone()
            owned_aid = (json.loads(owned['payload']).get('canonical_action_id') or owned['proposal_id']) if owned and owned['payload'] else None
            if (item.get('kind') not in ('update', 'cancel') or selected_aid != item['canonical_action_id']
                    or owned_aid != selected_aid):
                return 'Only the selected offer action and its owned event may change after explicit review.'
        refs = item.get('agenda_sources')
        if refs:
            context = json.loads(self._setting('source_learner_context','{}'))
            if item.get('agenda_learner') != {'id': context.get('id'), 'class_id': context.get('class_id')}:
                return 'Selected learner/class context changed; fresh agenda review required.'
            plan = self.db.execute("SELECT status FROM agenda_plans WHERE id=?", (item.get('agenda_plan_id'),)).fetchone()
            if not plan or plan[0] != 'applied':
                return 'A persisted active agenda plan is required.'
        else:
            # Existing explicit human decisions remain executable. Old model-only
            # pending proposals require review under the new shared planner.
            if not item.get('user_approved'):
                return 'Legacy automatic intent requires agenda review.'
            refs = [{'source_id': item.get('source_id'), 'source_hash': item.get('source_hash')}]
        for ref in refs:
            source = self.get_source(ref['source_id'])
            if (not source or source['learner_id'] != self._setting('source_learner_id')
                    or source['deleted'] or source['content_hash'] != ref['source_hash']):
                return 'Supporting source version changed or is unavailable; review required.'
        return None

    def remember_canonical_decision(self,proposal_id,record=None):
        from .core import utcnow
        action=self.db.execute('SELECT id FROM canonical_actions WHERE proposal_id=?',(proposal_id,)).fetchone()
        if not action:return
        row=self.db.execute('SELECT payload FROM decision_intents WHERE id=?',(proposal_id,)).fetchone()
        if row:
            self.db.execute('INSERT OR REPLACE INTO canonical_decisions VALUES (?,?,?)',(action[0],row[0],utcnow()))

    def prune_source_content(self,current=None,apply=False):
        """30-day text; minimal future facts/decisions and unknown writes survive."""
        from .core import WARSAW,as_datetime,utcnow
        from .data_admin import minimal_proposal,FLAGS
        current=current or datetime.now(WARSAW)
        cutoff=current-timedelta(days=30)
        selected=[]
        for row in self.db.execute('SELECT * FROM sources'):
            try:old=as_datetime(row['content_since'] or row['fetched_at'])<cutoff
            except (ValueError,TypeError):old=False
            if old and row['status']!='retained':selected.append(row)
        counts={'sources_pruned':len(selected),'source_previews_pruned':0,'source_audits_pruned':0,'agenda_plans_pruned':0}
        if not apply:return counts
        for row in selected:
            sid=row['id']
            unknown=self.db.execute("SELECT 1 FROM action_sources a JOIN canonical_actions c ON c.id=a.action_id JOIN operations o ON o.proposal_id=c.proposal_id WHERE a.source_id=? AND a.relation NOT IN (?,?,?) AND o.status IN ('unknown','inflight') LIMIT 1",(sid,CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION)).fetchone()
            # Unknown/inflight journals themselves are never modified, but source
            # copies can expire: the full immutable operation retains its intent.
            payload=json.loads(row['payload'])
            payload.update(text='',title='',sender='',url='',metadata={'retained':True,**{k:v for k,v in payload.get('metadata',{}).items() if k in ('event_start','event_end','scope')}})
            self.db.execute("UPDATE sources SET payload=?,status='retained',reason=NULL,last_error=NULL WHERE id=?",(json.dumps(payload),sid))
            self.db.execute("UPDATE action_sources SET source_quote='' WHERE source_id=?",(sid,))
            for actionrow in self.db.execute('SELECT c.* FROM canonical_actions c JOIN action_sources a ON a.action_id=c.id WHERE a.source_id=? AND a.relation NOT IN (?,?,?)',(sid,CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION)).fetchall():
                item=json.loads(actionrow['payload'])
                minimal=minimal_proposal(item)
                for key in ('canonical_action_id','source_id','source_hash','source_kind','source_version_at','optional','recipient_basis'):
                    if key in item:minimal[key]=item[key]
                # Minimal typed anchors enable later evidence-assisted retrieval;
                # no original body/quote/analysis reasoning remains.
                identity=item.get('action_identity')
                if identity:minimal['action_identity']={k:v for k,v in identity.items() if k!='evidence'}
                if item.get('recipient'):minimal['recipient']={k:v for k,v in item['recipient'].items() if k!='evidence'}
                self.db.execute('UPDATE canonical_actions SET payload=? WHERE id=?',(json.dumps(minimal,ensure_ascii=False),actionrow['id']))
                if not unknown:
                    proposal=self.db.execute('SELECT * FROM proposals WHERE id=?',(actionrow['proposal_id'],)).fetchone()
                    if proposal:
                        clean=minimal_proposal(self._proposal(proposal))
                        clean.update({k:v for k,v in minimal.items() if k in ('canonical_action_id','source_id','source_hash','source_kind','source_version_at','optional','recipient_basis','action_identity','recipient')})
                        self.db.execute('UPDATE proposals SET payload=? WHERE id=?',(json.dumps(clean,ensure_ascii=False),proposal['id']))
            # An expired, unverified fact owns its own diagnostic canonical row.
            # Supporting low-authority links must never minimize somebody else's
            # human/event target, but this owned diagnostic must not keep raw
            # narrative after its source body has expired either.
            for actionrow in self.db.execute('SELECT c.* FROM canonical_actions c JOIN action_sources a ON a.action_id=c.id WHERE a.source_id=? AND a.relation=?',
                                             (sid,EXPIRED_FACT_RELATION)).fetchall():
                item=json.loads(actionrow['payload']);action=self.get_action(actionrow['id'])
                if (not item.get('expired_without_authority')or item.get('source_id')!=sid
                        or authority_links(action['source_links'])or action.get('decision')or action.get('event_ids')
                        or any(item.get(flag)for flag in FLAGS)
                        or self.db.execute('SELECT 1 FROM operations WHERE proposal_id=?',(actionrow['proposal_id'],)).fetchone()):continue
                clean=minimal_proposal(item)
                clean.update(title='Historyczna informacja szkolna',description='',source_quote='',review_reason='',
                             expired_without_authority=True,applicability_unverified=True,status='ignored')
                clean.pop('action_identity',None);clean.pop('recipient',None)
                payload=json.dumps(clean,ensure_ascii=False)
                self.db.execute('UPDATE canonical_actions SET payload=? WHERE id=?',(payload,actionrow['id']))
                self.db.execute('UPDATE proposals SET payload=? WHERE id=?',(payload,actionrow['proposal_id']))
            # Raw normalized extraction and decision_reason are content, not
            # technical diagnostics. Never retain them for a 90-day exemption.
            counts['source_previews_pruned']+=self.db.execute('DELETE FROM source_previews WHERE source_id=?',(sid,)).rowcount
            counts['source_audits_pruned']+=self.db.execute('DELETE FROM source_analysis_audit WHERE source_id=?',(sid,)).rowcount
        for table,column,key in (('source_previews','created_at','source_previews_pruned'),('source_analysis_audit','analyzed_at','source_audits_pruned')):
            for row in self.db.execute('SELECT id,'+column+' FROM '+table).fetchall():
                try:old=as_datetime(row[column])<cutoff
                except (ValueError,TypeError):old=False
                if old:counts[key]+=self.db.execute('DELETE FROM '+table+' WHERE id=?',(row['id'],)).rowcount
        for row in self.db.execute('SELECT id,payload,status,created_at FROM agenda_plans').fetchall():
            try: old = as_datetime(row['created_at']) < cutoff
            except (ValueError,TypeError): old = False
            payload = json.loads(row['payload'])
            if not old or payload.get('content_expired') or row['status'] == 'failed':continue
            if row['status'] in ('preview', 'preview_failed'):
                self.db.execute('DELETE FROM agenda_plans WHERE id=?', (row['id'],))
            else:
                # Keep operational IDs/version references; private model prose
                # follows the same 30-day content retention as existing analysis.
                fields = ('operation','action_id','proposal_id','sources','status','review_change','offer_group_id','new_offer_key','candidate_action_ids')
                kept = {'snapshot_hash':payload.get('snapshot_hash'), 'content_expired':True,
                        'items':[{key:item[key] for key in fields if key in item} for item in payload.get('items',[])]}
                self.db.execute('UPDATE agenda_plans SET payload=? WHERE id=?', (json.dumps(kept),row['id']))
            counts['agenda_plans_pruned'] += 1
        return counts

    def source_mode_blocked(self, item):
        # Compatibility for the existing executor; engine selection is retired.
        return False

    def source_write_deferred(self, item):
        if not item.get('canonical_action_id'):
            return False
        if self._setting('source_last_collection_result') != 'ok':
            return True
        health = {r['kind']: r['last_result'] for r in self.db.execute('SELECT kind,last_result FROM source_health')}
        return any(health.get(kind) != 'ok' for kind in KINDS)
