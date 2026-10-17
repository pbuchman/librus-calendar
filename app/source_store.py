"""Versioned sources, canonical obligations and an isolated preview journal.

Calendar mutations continue to use the original immutable operation journal.
These tables are additive: no existing proposal, offer, event or operation IDs move.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import uuid

KINDS = frozenset({'message', 'announcement', 'school_event', 'substitution'})
CORROBORATION_RELATION = 'corroborates_unverified_recipient'
EXPIRED_FACT_RELATION = 'expired_unverified_fact'
CLOSED_OFFER_RELATION = 'retains_closed_offer_decision'
NON_AUTHORITY_RELATIONS = frozenset({CORROBORATION_RELATION,EXPIRED_FACT_RELATION,CLOSED_OFFER_RELATION})


def authority_links(links):
    """Corroboration records visibility only, never a new identity authority."""
    return [link for link in links if link.get('relation')not in NON_AUTHORITY_RELATIONS]
SOURCE_SCHEMA = '''
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
        for event in self.db.execute('SELECT * FROM events').fetchall():
            if self.db.execute('SELECT 1 FROM canonical_actions WHERE proposal_id=?',(event['proposal_id'],)).fetchone():continue
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
        for row in self.db.execute('SELECT kind,COUNT(*) AS total,SUM(status=\'pending\') AS pending,SUM(status=\'failed\') AS failed,SUM(deleted) AS deleted FROM sources GROUP BY kind'):
            counts[row['kind']] = {key:row[key] for key in ('total','pending','failed','deleted')}
            ready=self.db.execute("SELECT COUNT(*) FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE s.kind=? AND p.status='ready'",(row['kind'],)).fetchone()[0]
            failed=self.db.execute("SELECT COUNT(*) FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE s.kind=? AND p.status='failed' AND s.deleted=0",(row['kind'],)).fetchone()[0]
            counts[row['kind']]['preview_ready']=ready
            counts[row['kind']]['preview_failed']=failed
            counts[row['kind']]['preview_pending']=max(0,row['pending']+row['failed']-ready-failed) if self.config.multi_source_mode=='preview' and row['kind']!='message' else 0
            counts[row['kind']]['active_pending']=row['pending']+row['failed'] if self.config.multi_source_mode=='active' else 0
        health = {r['kind']:{**dict(r),'learner_verified':bool(r['learner_verified'])} for r in self.db.execute('SELECT * FROM source_health')}
        return {'mode':self.config.multi_source_mode,'counts':counts,'health':health,
                'active_pending':sum(x['pending']+x['failed'] for x in counts.values()) if self.config.multi_source_mode=='active' else 0,
                'preview_pending':sum(x['preview_pending'] for x in counts.values()) if self.config.multi_source_mode=='preview' else 0,
                'preview_failed':sum(x['preview_failed'] for x in counts.values()),
                'preview_count':self.db.execute("SELECT COUNT(*) FROM source_previews WHERE status='ready'").fetchone()[0],
                'last_collection_at':self._setting('source_last_collection_at'),
                'last_collection_result':self._setting('source_last_collection_result'),
                'last_error':self._setting('source_last_error'),
                'learner_verified':self._setting('source_learner_verified')=='true'}

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
                        if self.config.multi_source_mode=='active':
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
                if self.config.multi_source_mode=='active':
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
        sql = "SELECT * FROM sources s WHERE s.status IN ('pending','failed') AND s.deleted=0"
        if not include_messages:
            sql += " AND s.kind<>'message'"
        if preview:
            sql += " AND NOT EXISTS (SELECT 1 FROM source_previews p WHERE p.source_id=s.id AND p.source_hash=s.content_hash AND p.status='ready')"
        with self.lock:
            return [self._source(r) for r in self.db.execute(sql+' ORDER BY s.fetched_at,s.id LIMIT ?',(min(int(limit),100),))]

    def fail_source(self, source_id, code='source_analysis_failed', preview=False):
        from .core import digest,utcnow
        with self.lock,self.db:
            source=self.get_source(source_id)
            if preview:
                pid=digest([source_id,source['content_hash']])[:32]
                self.db.execute('INSERT INTO source_previews VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,last_error=excluded.last_error',
                    (pid,source_id,source['content_hash'],'{}','failed',utcnow(),code))
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
            payload={'proposals':proposals,'analysis_metadata':metadata or {},'outcomes':outcomes or [],'decision_snapshot':expected_decisions}
            self.db.execute('INSERT INTO source_previews VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status,last_error=NULL',
                (pid,source_id,source['content_hash'],json.dumps(payload,ensure_ascii=False),'ready',utcnow(),None))
            return pid

    def list_source_previews(self,limit=100,offset=0,status=None):
        if not 1<=int(limit)<=500 or int(offset)<0:
            raise ValueError('Invalid preview page.')
        sql='SELECT * FROM source_previews'+(' WHERE status=?' if status else '')+' ORDER BY created_at DESC,id LIMIT ? OFFSET ?'
        args=(status,int(limit),int(offset)) if status else (int(limit),int(offset))
        with self.lock:
            return [{**dict(r),**json.loads(r['payload'])} for r in self.db.execute(sql,args)]

    def get_action(self,action_id):
        with self.lock:
            row=self.db.execute('SELECT * FROM canonical_actions WHERE id=?',(str(action_id),)).fetchone()
            if not row:return None
            proposal=self.db.execute('SELECT * FROM proposals WHERE id=?',(row['proposal_id'],)).fetchone()
            item={**json.loads(row['payload']),**self._proposal(proposal)} if proposal else json.loads(row['payload'])
            decision=self.db.execute('SELECT payload FROM decision_intents WHERE id=?',(row['proposal_id'],)).fetchone()
            links=[dict(r) for r in self.db.execute('SELECT * FROM action_sources WHERE action_id=? ORDER BY source_id',(row['id'],))]
            item=self._legacy_origin_provenance(item,links)
            event_ids={r[0] for r in self.db.execute('SELECT id FROM events WHERE proposal_id=?',(row['proposal_id'],))}
            if item.get('event_id'):event_ids.add(item['event_id'])
            return {'id':row['id'],'proposal_id':row['proposal_id'],'status':item.get('status',row['status']),'payload':item,'source_links':links,
                    'evidence':self._linked_action_evidence(links,row['id']),
                    'original_evidence':self._original_action_evidence(row['id'],links),
                    'recovery_origin':self._current_origin_for_recovery(proposal,item,links),
                    'decision':json.loads((self.db.execute('SELECT payload FROM canonical_decisions WHERE action_id=?',(row['id'],)).fetchone() or decision)[0]) if (self.db.execute('SELECT 1 FROM canonical_decisions WHERE action_id=?',(row['id'],)).fetchone() or decision) else None,'event_ids':sorted(event_ids),'updated_at':row['updated_at']}

    def _legacy_origin_provenance(self,item,links):
        """Read projection for legacy decisions that predate typed provenance."""
        from .core import as_datetime
        if item.get('source_version_at')is not None:return item
        origins=[link for link in links if link.get('relation')=='origin']
        if len(origins)!=1:return item
        link=origins[0]
        row=self.db.execute('SELECT id,kind,content_hash,deleted,status,published_at,updated_at FROM sources WHERE id=?',(link['source_id'],)).fetchone()
        if not row or row['deleted']or row['status']=='retained'or row['content_hash']!=link['source_hash']:return item
        expected={'source_id':row['id'],'source_hash':row['content_hash'],'source_kind':row['kind']}
        if any(item.get(key)is not None and item[key]!=value for key,value in expected.items()):return item
        observed=row['updated_at']or row['published_at']
        try:as_datetime(observed)
        except (TypeError,ValueError,OverflowError):return item
        return {**item,**expected,'source_version_at':observed}

    def _original_action_evidence(self,action_id,links):
        from .core import digest
        return [{'id':digest(['original-action-quote',action_id,link['source_id'],link['source_hash'],link['source_quote']])[:32],
                 'source_id':link['source_id'],'source_hash':link['source_hash'],'text':link['source_quote']}
                for link in authority_links(links)[:8]if isinstance(link.get('source_quote'),str)and 4<=len(link['source_quote'].strip())<=4000]

    def _current_origin_for_recovery(self,proposal,item,links):
        """Expose current legacy origin separately from absent historical quotes.

        This is input to a separate fact extractor, not matching evidence. The
        actual proposal-row hash must corroborate the unique origin association.
        No stored quotation, decision or source version is repaired here.
        """
        from .core import as_datetime,digest
        if proposal is None or item.get('action_identity'):return None
        origins=[link for link in authority_links(links)if link.get('relation')=='origin']
        if len(origins)!=1:return None
        link=origins[0]
        if isinstance(link.get('source_quote'),str)and link['source_quote'].strip():return None
        row=self.db.execute('SELECT * FROM sources WHERE id=?',(link['source_id'],)).fetchone()
        if (not row or row['kind']!='message'or row['deleted']or row['status']=='retained'
                or row['upstream_id']!=proposal['message_id']
                or proposal['source_hash']!=link['source_hash']or row['content_hash']!=link['source_hash']):return None
        data=json.loads(row['payload']);body=data.get('text');observed=row['updated_at']or row['published_at']
        if not isinstance(body,str)or not 4<=len(body)<=4000 or(data.get('metadata')or{}).get('retained'):return None
        try:as_datetime(observed)
        except (ValueError,TypeError,OverflowError):return None
        return {'source_id':row['id'],'source_hash':row['content_hash'],'proposal_source_hash':proposal['source_hash'],
                'canonical_payload_hash':digest(item),'body':body,'published_at':observed}

    def _linked_action_evidence(self,links,action_id):
        """Bound context to the exact current source behind a saved quotation.

        A legacy quote may omit a nearby sentence identifying the occasion. Only
        its linked, still-current body can supply that context; retained bodies,
        title similarity and unrelated inbox content provide no matching proof.
        """
        from .core import digest
        evidence=[]
        for link in authority_links(links)[:8]:
            quote=link.get('source_quote')
            if not isinstance(quote,str)or len(quote.strip())<4 or len(quote)>4000:continue
            # A saved quote remains bounded historical proof at its recorded
            # version even after a revision. It must never gain stale body text.
            persisted=self.db.execute('SELECT 1 FROM action_sources WHERE action_id=? AND source_id=? AND source_hash=? AND source_quote=?',
                (action_id,link['source_id'],link['source_hash'],quote)).fetchone()
            snippet=quote if persisted else None
            expanded=False
            row=self.db.execute('SELECT id,content_hash,deleted,status,payload FROM sources WHERE id=?',(link['source_id'],)).fetchone()
            if row and not row['deleted'] and row['status']!='retained' and row['content_hash']==link['source_hash']:
                data=json.loads(row['payload']);body=data.get('text');metadata=data.get('metadata')or{}
                if isinstance(body,str)and len(body)<=100000 and quote in body and not metadata.get('retained'):
                    position=body.index(quote)
                    start=max(0,position-max(0,(2000-len(quote))//2))
                    end=min(len(body),start+2000)
                    start=max(0,min(start,end-2000))
                    snippet=body[start:end];expanded=True
            if snippet is not None:
                evidence.append({'id':digest([link['source_id'],link['source_hash'],snippet,expanded])[:32],
                    'source_id':link['source_id'],'source_hash':link['source_hash'],'source_quote':quote,
                    'text':snippet,'kind':'current_context'if expanded else'historical_quote'})
        return evidence

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
        return digest(state)

    def source_context_snapshot(self):
        context=json.loads(self._setting('source_learner_context','{}'))
        return {'id':context.get('id'),'class_id':context.get('class_id'),
                'pinned_id':self._setting('source_learner_id'),'verified':self._setting('source_learner_verified')}

    def candidate_actions(self,proposal,limit=12,additional=()):
        """Bounded relevant facts only; never all inboxes or all Calendar events."""
        def content_words(item):
            identity=item.get('action_identity') or {}
            text=' '.join(str(identity.get(k) or '') for k in ('object','occasion'))+' '+str(item.get('title') or '')+' '+str(item.get('source_quote') or '')
            generic={'librus','szkoła','school','klasa','class','dzieci','rodzice','proszę','termin','zajęcia','odbędzie','będzie','przygotować','przynieść'}
            return {w for w in re.findall(r'[^\W\d_]{4,}',normalized_text(text)) if w not in generic}
        words=content_words(proposal)
        ranked=[]
        available={r[0]:self.get_action(r[0]) for r in self.db.execute('SELECT id FROM canonical_actions ORDER BY id').fetchall()}
        # A speculative preview must never replace a persisted target's original
        # proof, decisions or hash-bound recovery context. New virtual targets
        # still help compare independent pending previews with each other.
        for action in additional:available.setdefault(action['id'],action)
        for action in available.values():
            item=action['payload']
            # A retained diagnostic with no human/event authority is not an
            # existing obligation candidate. Human-owned unknown inventory still
            # remains in the complete candidate set and cannot be filtered out.
            if item.get('expired_without_authority')and not action.get('event_ids')and not action.get('decision'):continue
            if 'evidence' not in action:
                action={**action,'evidence':self._linked_action_evidence(action.get('source_links',[]),action['id'])}
            tokens=content_words(item)
            for proof in action['evidence']:
                tokens.update(content_words({'source_quote':proof['text']}))
            score=len(words & tokens)
            same_source=bool(proposal.get('source_id') and any(x['source_id']==proposal['source_id'] for x in authority_links(action['source_links'])))
            if score or same_source or item.get('inventory_only'):
                ranked.append((int(same_source)*100+score,action))
        ranked.sort(key=lambda pair:(-pair[0],pair[1]['id']))
        self._source_candidates_complete = len(ranked)<=min(int(limit),12)
        return [action for _,action in ranked[:min(int(limit),12)]]

    def _source_write_reason(self,item):
        if not item.get('canonical_action_id'):
            return None
        from .core import digest
        if (not self._setting('source_activation_at')
                or self._setting('source_activation_learner')!=self._setting('source_learner_id')
                or self._setting('source_activation_binding')!=digest([self.config.calendar_id,self.config.calendar_owner])):
            return 'A verified activation receipt is required before source actions can write.'
        if self._setting('source_learner_verified')!='true':
            return 'Selected learner context is unverified; no source action may write.'
        source=self.get_source(item.get('source_id'))
        if not source or source['deleted'] or source['content_hash']!=item.get('source_hash') or source['status'] not in ('analyzed','retained'):
            return 'Source version changed or is unavailable; reanalysis required.'
        if item.get('recipient_basis',{}).get('kind')=='served_class_calendar':
            from .source_audience import calendar_recipient_basis
            context=json.loads(self._setting('source_learner_context','{}'))
            context['verified']=self._setting('source_learner_verified')=='true'
            if item['recipient_basis']!=calendar_recipient_basis(source,context):
                return 'Observed calendar audience context changed; fresh analysis required.'
        canonical=self.db.execute('SELECT proposal_id FROM canonical_actions WHERE id=?',(item['canonical_action_id'],)).fetchone()
        if not canonical or canonical[0]!=item['id']:
            return 'A newer canonical action supersedes this proposal.'
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
        counts={'sources_pruned':len(selected),'source_previews_pruned':0,'source_audits_pruned':0}
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
        return counts

    def source_mode_blocked(self,item):
        return (self.config.multi_source_mode!='active' and item.get('canonical_action_id')
                and item.get('source_kind') not in (None,'message'))

    def source_write_deferred(self,item):
        """A partial enabled channel cannot prove absence of cross-source facts."""
        if not item.get('canonical_action_id'):
            return False
        if self._setting('source_activation_incomplete',''):return True
        if self.config.multi_source_mode!='active':return False
        if item.get('recipient_basis',{}).get('kind')=='served_class_calendar':
            from .source_audience import calendar_recipient_basis
            context=json.loads(self._setting('source_learner_context','{}'))
            context['verified']=self._setting('source_learner_verified')=='true'
            source=self.get_source(item.get('source_id'))
            if (not source or self._setting('source_activation_class')!=context.get('class_id')
                    or item['recipient_basis']!=calendar_recipient_basis(source,context)):
                return True
        if self._setting('source_last_collection_result')!='ok':
            return True
        health={r['kind']:r['last_result'] for r in self.db.execute('SELECT kind,last_result FROM source_health')}
        return any(health.get(kind)!='ok' for kind in KINDS)
