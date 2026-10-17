"""Explicit fingerprint-bound local administration, always under the sync lock."""
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import uuid

from .core import WARSAW,digest,utcnow


def read_private_json(path):
    from .offer_groups import read_private_json as read
    return read(path)


def write_private_json(path,data):
    path=Path(path).expanduser()
    if not path.parent.is_dir():raise ValueError('Private output directory must exist.')
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW
    fd=os.open(path,flags,0o600)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(data,output,ensure_ascii=False,indent=2);output.write('\n');output.flush();os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True);raise
    return path


def activation_snapshot(store):
    return {'source_versions':[{ 'id':r['id'],'content_hash':r['content_hash'],'deleted':r['deleted'],'status':r['status']}
                               for r in store.db.execute('SELECT id,content_hash,deleted,status FROM sources ORDER BY id')],
            'decisions':store.source_decision_snapshot(),
            'learner_id':store._setting('source_learner_id'),
            'calendar_binding':digest([store.config.calendar_id,store.config.calendar_owner]),
            'operations':digest([tuple(r) for r in store.db.execute('SELECT id,status,attempts,updated_at FROM operations ORDER BY id')])}


def activate_sources(store,runtime,*,plan_path,apply=False,current=None,client_factory=None):
    """Saved preview payload is deliberately never read as an execution plan.

    The protected configuration is the sole mode authority. The operator changes
    preview to active while scheduled sync is stopped, then applies this receipt.
    Both modes and this command keep original Calendar write pause policy intact.
    """
    current=current or datetime.now(WARSAW)
    def incomplete_attempt():
        value=store._setting('source_activation_incomplete','')
        if not value:return None
        try:attempt=json.loads(value)
        except (TypeError,ValueError):raise ValueError('Incomplete activation needs a valid fresh retry plan.')from None
        if (not isinstance(attempt,dict)or set(attempt)!={'token','plan','sources'}
                or not isinstance(attempt['sources'],list)or not 1<=len(attempt['sources'])<=5000
                or any(not isinstance(item,dict)or set(item)!={'id','hash'}for item in attempt['sources'])):
            raise ValueError('Incomplete activation needs a valid fresh retry plan.')
        return attempt
    def ready_sources():
        from .source_store import KINDS
        required=KINDS-{'message'}
        health={r['kind']:dict(r) for r in store.db.execute('SELECT * FROM source_health')}
        if (store._setting('source_last_collection_result')!='ok'
                or any(k not in health or health[k]['last_result']!='ok' or not health[k]['learner_verified'] for k in required)):
            raise ValueError('Every required source channel must have a complete verified collection.')
        ready=[dict(r) for r in store.db.execute("SELECT p.* FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE p.status='ready' AND s.deleted=0 AND s.kind<>'message' ORDER BY p.source_id")]
        attempt=incomplete_attempt()
        if attempt:
            selected={item['source_id']:item for item in ready}
            for previous in attempt['sources']:
                source=store.get_source(previous['id'])
                if not source or source['deleted']:continue
                if source['id']in selected:continue
                # Only this failed attempt's exact current versions may reuse
                # their activated preview as a selection receipt. Its payload
                # is never executed: every selected source is freshly analyzed.
                preview=store.db.execute("SELECT * FROM source_previews WHERE source_id=? AND source_hash=? AND status IN ('ready','activated')",
                    (source['id'],source['content_hash'])).fetchone()
                if source['content_hash']!=previous['hash']or not preview:
                    raise ValueError('An incomplete activation source needs a fresh current preview.')
                selected[source['id']]=dict(preview)
            ready=[selected[key]for key in sorted(selected)]
        missing=store.db.execute("SELECT COUNT(*) FROM sources s WHERE s.kind<>'message' AND s.deleted=0 AND s.status IN ('pending','failed') AND NOT EXISTS (SELECT 1 FROM source_previews p WHERE p.source_id=s.id AND p.source_hash=s.content_hash AND p.status='ready')").fetchone()[0]
        if missing or not ready or len(ready)>5000:
            raise ValueError('Every current source version needs a ready preview before activation.')
        return ready
    if not apply:
        if store.config.multi_source_mode!='preview':raise ValueError('Activation planning requires preview mode.')
        ready=ready_sources()
        if not ready or store._setting('source_learner_verified')!='true':raise ValueError('Verified source previews are required.')
        plan={'version':1,'kind':'source_activation','created_at':utcnow(),'snapshot':activation_snapshot(store),
              'activation_guard':digest(store._setting('source_activation_incomplete','')),
              'sources':[{ 'id':p['source_id'],'hash':p['source_hash']} for p in ready]}
        plan['fingerprint']=digest(plan)
        write_private_json(plan_path,plan)
        return {'status':'preview','sources':len(ready),'calendar_writes':0,'plan_fingerprint':plan['fingerprint']}
    plan=read_private_json(plan_path)
    signed={k:v for k,v in plan.items() if k!='fingerprint'}
    if plan.get('kind')!='source_activation' or plan.get('version')!=1 or plan.get('fingerprint')!=digest(signed):raise ValueError('Invalid activation plan.')
    if store.config.multi_source_mode!='active':raise ValueError('Protected configuration must explicitly enable active mode.')
    if plan.get('activation_guard',digest(''))!=digest(store._setting('source_activation_incomplete','')):
        raise ValueError('Activation attempt changed; create a fresh retry plan.')
    if activation_snapshot(store)!=plan['snapshot']:raise ValueError('Activation snapshot changed; create a fresh plan.')
    ready_sources()
    if not callable(client_factory):raise ValueError('Fresh verified source collection is required for activation.')
    if not getattr(runtime,'read_only',False):raise ValueError('Activation validation requires a read-only Calendar runtime.')
    selected=ready_sources()
    if {item['source_id']for item in selected}!={item['id']for item in plan['sources']}:
        raise ValueError('Activation retry does not cover its incomplete selection.')
    attempt=json.dumps({'token':uuid.uuid4().hex,'plan':plan['fingerprint'],'sources':plan['sources']},sort_keys=True)
    with store.lock,store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if (activation_snapshot(store)!=plan['snapshot']or
                plan.get('activation_guard',digest(''))!=digest(store._setting('source_activation_incomplete',''))):
            raise ValueError('Activation state changed before collection.')
        attempt_context=store.source_context_snapshot()
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('source_activation_incomplete',?)",(attempt,))
    client=client_factory()
    batch=client.collect_sources(current=current,past_days=30,future_days=60)
    store.ingest_sources(batch,current)
    ready_sources()
    if activation_snapshot(store)!=plan['snapshot']:raise ValueError('Fresh source collection changed the activation snapshot.')
    probe=runtime.probe()
    if probe.get('calendar_id')!=store.calendar_id or probe.get('access_role') not in ('owner','writer'):
        raise ValueError('Exact Calendar access was not verified.')
    from .codex_runtime import event_fingerprint,marker_present,external_attendees
    for event in store.list_events(100000,include_cancelled=True):
        observed=runtime.read_event(event['id'],event['snapshot'])
        if (observed.get('id')!=event['id'] or not marker_present(observed.get('description'),event['marker'])
                or external_attendees(observed,store.config.calendar_owner)
                or event_fingerprint(observed,store.config.calendar_owner)!=event['fingerprint']):
            raise ValueError('Owned Calendar evidence changed; review before activation.')
    # Recheck after external reads. UI decisions made during Calendar validation
    # cannot be silently accepted under the previous plan's fingerprint.
    if activation_snapshot(store)!=plan['snapshot']:raise ValueError('Activation state changed during Calendar validation.')
    from .source_planner import SourcePlanner
    planner=SourcePlanner(store,runtime,expected_context=attempt_context)
    outcomes=[]
    for selected in plan['sources']:
        source=store.get_source(selected['id'])
        if not source or source['content_hash']!=selected['hash'] or source['deleted']:raise ValueError('Activation source changed.')
        outcomes.append(planner.analyze(source,current))
    with store.lock,store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if (store._setting('source_activation_incomplete','')!=attempt
                or store.source_context_snapshot()!=attempt_context):
            raise ValueError('Activation attempt changed before completion.')
        for key,value in (('source_activation_at',utcnow()),('source_activation_learner',attempt_context['pinned_id']),
                ('source_activation_class',attempt_context['class_id']or''),
                ('source_activation_binding',digest([store.config.calendar_id,store.config.calendar_owner])),('source_activation_plan',plan['fingerprint'])):
            store.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,value))
        store.db.execute("DELETE FROM settings WHERE key='source_activation_incomplete'")
    return {'status':'applied','sources':len(outcomes),'calendar_writes':0,
            'actions':sum(len(x['outcomes']) for x in outcomes),'plan_fingerprint':plan['fingerprint']}


def cleanup_existing(store,*,plan_path,request=None,apply=False):
    """Retire explicitly supplied local proposals without touching Calendar.

    Decisions, IDs and all journal records survive. An attempted or unknown write
    is never changed by this command. No real production IDs exist in this code.
    """
    def snapshot(ids):
        records=[]
        for pid in ids:
            row=store.db.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
            if not row:raise ValueError('Unknown cleanup proposal.')
            records.append({'proposal':dict(row),'operations':[dict(r) for r in store.db.execute('SELECT * FROM operations WHERE proposal_id=? ORDER BY id',(pid,))],
                            'events':[dict(r) for r in store.db.execute('SELECT * FROM events WHERE proposal_id=? ORDER BY id',(pid,))]})
        return {'records':records,'decisions':store.source_decision_snapshot()}
    if not apply:
        if not isinstance(request,dict) or set(request)!={'changes'} or not isinstance(request['changes'],list) or not 1<=len(request['changes'])<=100:
            raise ValueError('Explicit bounded cleanup changes are required.')
        changes=request['changes']
        ids=[]
        for change in changes:
            if not isinstance(change,dict) or set(change)!={'proposal_id','reason'} or not isinstance(change['proposal_id'],str) or not isinstance(change['reason'],str) or not change['reason'].strip():raise ValueError('Invalid cleanup request.')
            ids.append(change['proposal_id'])
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate cleanup IDs.')
        plan={'version':1,'kind':'source_cleanup','created_at':utcnow(),'changes':changes,'snapshot':snapshot(ids)}
        plan['fingerprint']=digest(plan);write_private_json(plan_path,plan)
        return {'status':'preview','proposals':len(ids),'calendar_writes':0,'plan_fingerprint':plan['fingerprint']}
    plan=read_private_json(plan_path);unsigned={k:v for k,v in plan.items() if k!='fingerprint'}
    if plan.get('kind')!='source_cleanup' or plan.get('version')!=1 or plan.get('fingerprint')!=digest(unsigned):raise ValueError('Invalid cleanup plan.')
    ids=[c['proposal_id'] for c in plan['changes']]
    with store.lock,store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if snapshot(ids)!=plan['snapshot']:raise ValueError('Cleanup snapshot changed; review a new plan.')
        for change in plan['changes']:
            row=store.db.execute('SELECT * FROM proposals WHERE id=?',(change['proposal_id'],)).fetchone()
            item=store._proposal(row)
            if item.get('user_approved') or item.get('user_edited') or row['status']=='applied':raise ValueError('Cleanup cannot retire an applied or approved action.')
            if store.db.execute("SELECT 1 FROM operations WHERE proposal_id=? AND (attempts>0 OR status IN ('inflight','unknown','applied'))",(row['id'],)).fetchone():raise ValueError('Cleanup cannot alter external write evidence.')
        for change in plan['changes']:
            row=store.db.execute('SELECT * FROM proposals WHERE id=?',(change['proposal_id'],)).fetchone()
            item=store._proposal(row)
            # Explicitly ignored prior actions stay rejected; automatic obsolete
            # ones are superseded without inventing a human approval/rejection.
            status='ignored' if item.get('user_rejected') else 'superseded'
            item.update(status=status,needs_review=False,review_reason=change['reason'][:1000])
            stamp=utcnow()
            store.db.execute('UPDATE proposals SET payload=?,status=?,updated_at=?,pending_since=NULL WHERE id=?',(json.dumps(item,ensure_ascii=False),status,stamp,row['id']))
            store.db.execute("UPDATE operations SET status='superseded',last_error='Explicit local cleanup; no Calendar change.',updated_at=? WHERE proposal_id=? AND attempts=0 AND status IN ('pending','review')",(stamp,row['id']))
            store.db.execute('UPDATE canonical_actions SET payload=?,status=?,updated_at=? WHERE proposal_id=?',(json.dumps(item,ensure_ascii=False),status,stamp,row['id']))
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('source_cleanup_last_applied_at',?)",(utcnow(),))
    return {'status':'applied','proposals':len(ids),'calendar_writes':0,'plan_fingerprint':plan['fingerprint']}
