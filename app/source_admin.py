"""Explicit fingerprint-bound local administration, always under the sync lock."""
import json
import os
from pathlib import Path

from .core import digest,utcnow


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


def activate_sources(store, runtime, *, plan_path, apply=False, current=None, client_factory=None):
    """Retired rollout switch; no legacy activation may bypass the agenda."""
    raise ValueError('Source engine activation is retired; use the complete agenda preview and normal sync.')


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
