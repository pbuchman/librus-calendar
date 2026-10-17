#!/usr/bin/env python3
"""Private multi-source model evaluation: isolated state, no discovery/Calendar.

Input (owned 0600 JSON) is {learner:{id,class_id}, cases:[{id,source,expected,
expected_outcomes?,expected_targets?}]}. expected is an unordered list of proposal dictionaries; each
field supplied is compared exactly, and source_quote is mandatory for each item.
expected_outcomes and expected_targets follow expected proposal order. Each target
is null (unconstrained) or {seed_index,proposal_index}, zero-based indices into
the explicit seeds and their input proposals. Targets require the applied canonical
ID and a current source/hash link; ambiguous seed-to-proposal maps are rejected.
Optional legacy_rows imports only exact messages, proposals, proposal_groups and
proposal_group_members into isolated state before ordinary AppStore migration.
An explicit legacy_offer_group_index case uses an empty extraction oracle and
reports existing-state preservation separately before any guarded selection.
Legacy seed legacy_proposal_source_hashes only asserts captured row hashes.
Explicit expected_scope='actionable'
excludes only kind='ignore' from matching; all produced quotations remain checked.
An optional private details file contains the exact input/output transcript.
Stdout reports counts and SHA-256 case references only. Existing files are never
replaced. This command never discovers production configuration by default.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import datetime
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def _json_digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON field.')
        result[key]=value
    return result


def read_evaluation_input(path):
    """Parse and hash the exact same protected bytes through one owned fd."""
    descriptor=os.open(Path(path).expanduser(),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode)or info.st_uid!=os.getuid()or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('Evaluation input must be an owned 0600 regular file.')
        with os.fdopen(descriptor,'rb')as stream:
            descriptor=None;payload=stream.read(20_000_001);after=os.fstat(stream.fileno())
        attributes=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_mode,value.st_uid)
        if attributes(info)!=attributes(after):raise ValueError('Evaluation input changed during read.')
        if len(payload)>20_000_000:raise ValueError('Evaluation input exceeded its limit.')
        return json.loads(payload,object_pairs_hook=_unique_object),hashlib.sha256(payload).hexdigest()
    finally:
        if descriptor is not None:os.close(descriptor)


def evaluation_code_snapshot(root=ROOT):
    root=Path(root)
    files=set((root/'app').rglob('*.py'))
    files.update(p for p in(root/'app/schemas').rglob('*')if p.is_file())
    files.add(root/'scripts/evaluate_sources.py')
    manifest={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()for path in sorted(files)}
    return manifest,_json_digest(manifest)


class RecordingRuntime:
    """Analysis-only wrapper: record actual calls without repeating inference."""
    read_only=True
    def __init__(self,runtime):
        if not runtime.read_only:raise ValueError('Read-only analysis is required.')
        self.runtime=runtime;self.model_calls=0;self.records=[]
        invoke=runtime._invoke
        def counted(*values,**options):
            if options.get('tools')or(len(values)>2 and values[2]):raise ValueError('Evaluation tools are forbidden.')
            self.model_calls+=1
            return invoke(*values,**options)
        runtime._invoke=counted
    @property
    def last_analysis_metadata(self):return self.runtime.last_analysis_metadata
    @last_analysis_metadata.setter
    def last_analysis_metadata(self,value):self.runtime.last_analysis_metadata=value
    @property
    def last_verification_metadata(self):return getattr(self.runtime,'last_verification_metadata',None)
    @last_verification_metadata.setter
    def last_verification_metadata(self,value):self.runtime.last_verification_metadata=value
    @property
    def last_recovery_metadata(self):return getattr(self.runtime,'last_recovery_metadata',None)
    @last_recovery_metadata.setter
    def last_recovery_metadata(self,value):self.runtime.last_recovery_metadata=value
    def __getattr__(self,name):
        # Preserve optional production branches for older injected runtimes;
        # never expose a verifier/recovery capability they do not actually have.
        metadata_attribute={'verify_source_action':'last_verification_metadata',
                            'recover_source_action_evidence':'last_recovery_metadata'}.get(name)
        if metadata_attribute is None:raise AttributeError(name)
        method=getattr(self.runtime,name)
        def recorded_call(*args,**kwargs):
            result=method(*args,**kwargs)
            self.records.append({'method':name,'result':deepcopy(result),
                                 'metadata':deepcopy(getattr(self,metadata_attribute))})
            return result
        return recorded_call
    def analyze_source(self,source,context):
        raw=self.runtime.analyze_source(source,context)
        self.records.append({'method':'analyze_source','raw':deepcopy(raw),'metadata':deepcopy(self.last_analysis_metadata)})
        return raw
    def match_source_action(self,source,item,candidates):
        result=self.runtime.match_source_action(source,item,candidates)
        self.records.append({'method':'match_source_action','result':deepcopy(result)})
        return result


def analyze_and_record(planner,source,current):
    """Observe SourcePlanner.analyze; never clone its production branches."""
    from app import source_planner
    record={};prepare=source_planner.prepare_source_proposals;apply=planner.apply
    offset=len(planner.runtime.records)
    def recorded_prepare(source,raw,context,current=None):
        record['raw']=deepcopy(raw)
        result=prepare(source,raw,context,current)
        record['prepared']=deepcopy(result)
        return result
    def recorded_apply(source,prepared,matches,metadata,snapshot,current=None):
        record['metadata']=deepcopy(metadata);record['decision_snapshot']=snapshot
        record['pairings']=[{'action_id':matched['id']if matched else None,'decision':decision}for matched,decision in matches]
        return apply(source,prepared,matches,metadata,snapshot,current)
    with patch.object(source_planner,'prepare_source_proposals',recorded_prepare),patch.object(planner,'apply',recorded_apply):
        record['applied']=deepcopy(planner.analyze(source,current))
    record['runtime_calls']=deepcopy(planner.runtime.records[offset:])
    return record


def analyze_pending_and_record(planner,source,current):
    pending={item['id']for item in planner.store.pending_sources(100,include_messages=True)}
    if source['id']in pending:return analyze_and_record(planner,source,current)
    if source['status']in {'pending','failed'}and not source['deleted']:
        raise ValueError('Fixture pending queue exceeds its bound.')
    return {'raw':[],'prepared':[],'runtime_calls':[],'pairings':[],
            'metadata':{'model':None,'effort':None,'prompt_version':'incremental-noop-v1'},
            'applied':{'source_id':source['id'],'status':'skipped','outcomes':[],'count':0}}


def fixture_current(value):
    from app.core import WARSAW
    current=datetime.fromisoformat(value or '2030-01-01T12:00:00+01:00')
    if current.tzinfo is None or current.utcoffset()is None:raise ValueError('Fixture time requires a timezone.')
    return current.astimezone(WARSAW)


def _perfect_matching(edges,count):
    """Bipartite matching handles overlapping field-subset oracles correctly."""
    if len(edges)!=count:return None
    owners={}
    def assign(expected,seen):
        for actual in edges[expected]:
            if actual in seen:continue
            seen.add(actual)
            if actual not in owners or assign(owners[actual],seen):owners[actual]=expected;return True
        return False
    for expected in sorted(range(len(edges)),key=lambda index:len(edges[index])):
        if not assign(expected,set()):return None
    result=[None]*len(edges)
    for actual,expected in owners.items():result[expected]=actual
    return result


def seed_target_ids(store,seed,applied):
    """Map input proposal order only when production produced a unique 1:1 map.

    Source preparation can deduplicate input rows. Never silently interpret an
    input proposal index as a different position in that shortened result.
    Legacy proposals register a canonical action with their own proposal ID;
    ordinary source apply supplies the canonical action ID explicitly.
    """
    outcomes=applied['outcomes']
    if len(outcomes)!=len(seed['proposals']):return None
    targets=[];proposals=set()
    for outcome in outcomes:
        proposal_id=outcome.get('proposal_id')
        action_id=proposal_id if seed.get('legacy') else outcome.get('action_id')
        if (not isinstance(action_id,str)or not action_id or not isinstance(proposal_id,str)
                or proposal_id in proposals or action_id in targets
                or not store.db.execute('SELECT 1 FROM canonical_actions WHERE id=? AND proposal_id=?',
                                        (action_id,proposal_id)).fetchone()):return None
        targets.append(action_id);proposals.add(proposal_id)
    return targets


def expected_target_references(case):
    references=case.get('expected_targets',[None]*len(case['expected']))
    if not isinstance(references,list)or len(references)!=len(case['expected']):
        raise ValueError('Expected targets must align with expected proposal order.')
    for reference in references:
        if reference is not None and (not isinstance(reference,dict)or set(reference)!={'seed_index','proposal_index'}
                or any(type(reference[key])is not int or reference[key]<0 for key in reference)):
            raise ValueError('Invalid explicit canonical target reference.')
    return references


def resolve_expected_targets(case,seeds,seed_targets):
    """Validate private references and resolve them without publishing any IDs."""
    targets=[]
    for reference in expected_target_references(case):
        if reference is None:targets.append(None);continue
        seed_index,proposal_index=reference['seed_index'],reference['proposal_index']
        if (seed_index>=len(seeds)or proposal_index>=len(seeds[seed_index]['proposals'])or seed_targets[seed_index]is None
                or proposal_index>=len(seed_targets[seed_index])):
            raise ValueError('Canonical target has no unambiguous seeded proposal.')
        targets.append(seed_targets[seed_index][proposal_index])
    return targets


def linked_outcome_targets(store,source,applied):
    """An outcome ID alone cannot prove that this source version was linked."""
    return [outcome.get('action_id')if store.db.execute(
        '''SELECT 1 FROM canonical_actions a JOIN action_sources s ON s.action_id=a.id
           JOIN sources current ON current.id=s.source_id AND current.content_hash=s.source_hash
           WHERE a.id=? AND s.source_id=? AND s.source_hash=? AND current.deleted=0''',
        (outcome.get('action_id'),source['id'],source['content_hash'])).fetchone()else None
        for outcome in applied['outcomes']]


def compare_oracle(case,actual,outcomes,text,quote_policy='exact',*,expected_target_ids=None,actual_target_ids=None):
    references=expected_target_references(case)
    target_count=sum(reference is not None for reference in references)
    if target_count and (expected_target_ids is None or actual_target_ids is None):
        raise ValueError('Explicit target constraints require resolved seeded and linked outcome IDs.')
    if (expected_target_ids is not None and (not isinstance(expected_target_ids,list)or len(expected_target_ids)!=len(case['expected']))
            or actual_target_ids is not None and (not isinstance(actual_target_ids,list)or len(actual_target_ids)!=len(actual))):
        raise ValueError('Canonical target mapping does not align with proposal rows.')
    if expected_target_ids is not None and any((reference is None)!=(target is None)
            or target is not None and (not isinstance(target,str)or not target)
            for reference,target in zip(references,expected_target_ids)):
        raise ValueError('An explicit canonical reference cannot resolve to an unconstrained target.')
    indices=[i for i,item in enumerate(actual)if case.get('expected_scope','all')=='all'or item.get('kind')!='ignore']
    selected=[actual[i]for i in indices];expected=case['expected']
    semantic=[[i for i,item in enumerate(selected)if all(item.get(k)==v for k,v in wanted.items()if k!='source_quote')]for wanted in expected]
    quoted=[[i for i in compatible if quote_policy=='literal'or selected[i].get('source_quote')==wanted['source_quote']]for wanted,compatible in zip(expected,semantic)]
    literal=all(isinstance(item.get('source_quote'),str)and len(item['source_quote'].strip())>=4 and item['source_quote']in text for item in actual)
    expected_literal=all(len(wanted['source_quote'].strip())>=4 and wanted['source_quote']in text for wanted in expected)
    aligned=quoted
    if 'expected_outcomes'in case:
        aligned=[[i for i in compatible if outcomes[indices[i]]==wanted]for compatible,wanted in zip(quoted,case['expected_outcomes'])]
    outcome_assignment=_perfect_matching(aligned,len(selected))
    if target_count:
        aligned=[[i for i in compatible if target is None or actual_target_ids[indices[i]]==target]
                 for compatible,target in zip(aligned,expected_target_ids)]
    assignment=_perfect_matching(aligned,len(selected))
    return {'semantic_passed':_perfect_matching(semantic,len(selected))is not None,
            'quotes_passed':literal and expected_literal and _perfect_matching(quoted,len(selected))is not None,
            'outcomes_passed':outcome_assignment is not None,'targets_passed':not target_count or assignment is not None,
            'target_check_count':target_count,'oracle_actual_count':len(selected),
            'matched_actual_indices':[indices[i]for i in assignment]if assignment is not None else None}


def durable_state_snapshot(store):
    """Full durable intent, excluding only proposals.updated_at bookkeeping.

    Production queue_operations rewrites that single timestamp on every prepare.
    All IDs, payloads, statuses, decisions and complete operation rows must stay
    identical. Timestamp changes are counted separately by repeat_fixture.
    """
    tables=('proposals','canonical_actions','canonical_decisions','decision_intents','operations',
            'proposal_groups','proposal_group_members','canonical_offer_groups','action_sources','events')
    result={}
    for table in tables:
        columns=[row[1]for row in store.db.execute('PRAGMA table_info('+table+')')if not(table=='proposals'and row[1]=='updated_at')]
        result[table]=[tuple(row)for row in store.db.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY 1')]
    return result


def repeat_fixture(store,planner,ingest,source,current):
    before=durable_state_snapshot(store);timestamps=dict(store.db.execute('SELECT id,updated_at FROM proposals'))
    calls=planner.runtime.model_calls
    repeated=ingest(source,current);recordings=[]
    for pending in store.pending_sources(100,include_messages=True):recordings.append(analyze_and_record(planner,pending,current))
    queued=store.queue_operations(current);after=durable_state_snapshot(store)
    changed=[table for table in before if before[table]!=after[table]]
    return {'source_id':repeated['id'],'recordings':recordings,'new_local_operations':len(queued),
            'model_calls':planner.runtime.model_calls-calls,'state_unchanged':not changed,'changed_tables':changed,
            'proposal_timestamp_changes':sum(timestamps.get(row[0])!=row[1]for row in store.db.execute('SELECT id,updated_at FROM proposals')),
            'before_state_sha256':_json_digest(before),'after_state_sha256':_json_digest(after)}


def _event_snapshots(store):
    return {row['id']:json.loads(row['snapshot'])for row in store.db.execute('SELECT id,snapshot FROM events ORDER BY id')}


LEGACY_ROW_LIMITS={'messages':10,'proposals':30,'proposal_groups':10,'proposal_group_members':30}


def _legacy_digest(value):
    return isinstance(value,str)and re.fullmatch(r'[0-9a-f]{64}',value)is not None


def import_legacy_rows(store,rows):
    """Import only explicit untouched legacy offer rows into an empty fixture DB.

    Reopening AppStore after this import runs its ordinary ownership migration.
    No source, canonical, operation, event, setting or decision row is imported.
    Authoritative table identities are never regenerated from payload markers.
    """
    from app.core import digest
    if not isinstance(rows,dict)or set(rows)!=set(LEGACY_ROW_LIMITS):raise ValueError('Invalid legacy row table allowlist.')
    for table,limit in LEGACY_ROW_LIMITS.items():
        items=rows[table]
        if not isinstance(items,list)or not 1<=len(items)<=limit:raise ValueError('Unbounded legacy fixture rows.')
        columns={row['name']:row for row in store.db.execute('PRAGMA table_info('+table+')')}
        if store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]:raise ValueError('Legacy import requires an empty fixture.')
        for item in items:
            if not isinstance(item,dict)or set(item)!=set(columns):raise ValueError('Legacy rows require exact known columns.')
            for key,value in item.items():
                column=columns[key]
                if value is None:
                    if column['notnull']or column['pk']:raise ValueError('Legacy required column is missing.')
                elif column['type']=='INTEGER':
                    if type(value)is not int:raise ValueError('Invalid legacy integer column.')
                elif not isinstance(value,str)or len(value)>256_000:raise ValueError('Unbounded legacy text column.')
        if table!='proposal_group_members':
            ids=[item['id']for item in items]
            if any(not isinstance(value,str)or not 1<=len(value)<=200 for value in ids)or len(set(ids))!=len(ids):
                raise ValueError('Legacy fixture IDs must be explicit and unique.')
    messages={row['id']:row for row in rows['messages']};proposals={row['id']:row for row in rows['proposals']}
    groups={row['id']:row for row in rows['proposal_groups']};payloads={}
    for mid,row in messages.items():
        payload=json.loads(row['payload'],object_pairs_hook=_unique_object)
        if (not isinstance(payload,dict)or set(payload)!={'id','subject','sender','sent_at','text','attachment_count'}
                or payload['id']!=mid or not isinstance(payload['text'],str)or len(payload['text'])>100_000
                or not _legacy_digest(row['payload_hash'])or digest(payload)!=row['payload_hash']
                or row['status']!='analyzed'or row['analyzed_hash']!=row['payload_hash']
                or row['last_error']is not None or row['pending_since']is not None or row['attempts']<0):
            raise ValueError('Legacy message is not exact current analyzed evidence.')
        payloads[mid]=payload
    for pid,row in proposals.items():
        payload=json.loads(row['payload'],object_pairs_hook=_unique_object);message=messages.get(row['message_id'])
        if (not isinstance(payload,dict)or not message or row['source_hash']!=message['payload_hash']
                or row['status']!='review'or row['pending_since']is not None
                or payload.get('id',pid)!=pid or payload.get('message_id',row['message_id'])!=row['message_id']
                or payload.get('status','review')!='review'or payload.get('kind')!='create'
                or payload.get('temporal_kind','event')!='event'or payload.get('activity_scope')!='extracurricular'
                or any(payload.get(key)for key in ('canonical_action_id','event_id','user_approved','user_rejected',
                    'user_edited','deletion_approved','deferred_unknown_operation','inventory_only'))):
            raise ValueError('Legacy offer members must be untouched review proposals.')
    if len({row['message_id']for row in groups.values()})!=len(groups):
        raise ValueError('Legacy fixture permits one explicit offer per message.')
    if {row['message_id']for row in groups.values()}!=set(messages):raise ValueError('Legacy message association is incomplete.')
    members_seen=set()
    for gid,row in groups.items():
        message=messages.get(row['message_id']);members=[item for item in rows['proposal_group_members']if item['group_id']==gid]
        evidence=row['source_evidence'];compact=lambda value:' '.join(value.split())
        if (not message or row['source_hash']!=message['payload_hash']or row['status']!='open'
                or row['selected_proposal_id']is not None or row['revision']<1
                or not 1<=len(row['group_key'])<=80 or not 6<=len(evidence)<=4000
                or compact(evidence)not in compact(payloads[row['message_id']]['text'])
                or not 2<=len(members)<=30 or [item['position']for item in members]!=list(range(len(members)))):
            raise ValueError('Legacy offer group evidence or order is invalid.')
        for member in members:
            proposal=proposals.get(member['proposal_id'])
            if (not proposal or member['proposal_id']in members_seen
                    or proposal['message_id']!=row['message_id']or proposal['source_hash']!=row['source_hash']):
                raise ValueError('Legacy offer membership is incomplete or duplicated.')
            members_seen.add(member['proposal_id'])
    if members_seen!=set(proposals)or any(member['group_id']not in groups for member in rows['proposal_group_members']):
        raise ValueError('Legacy fixture has orphaned rows.')
    with store.db:
        for table in LEGACY_ROW_LIMITS:
            columns=list(rows[table][0]);marks=','.join('?'for _ in columns)
            store.db.executemany('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+marks+')',
                                 [tuple(row[column]for column in columns)for row in rows[table]])


def legacy_offer_preservation(store,rows,index):
    """Compare authoritative legacy state before any simulated selection.

    Only messages.fetched_at is observational re-ingestion bookkeeping. Every
    proposal/group/member column, including their timestamps, must stay exact.
    """
    group=rows['proposal_groups'][index];mid=group['message_id'];gid=group['id']
    expected_members=[row for row in rows['proposal_group_members']if row['group_id']==gid]
    member_ids={row['proposal_id']for row in expected_members}
    expected_proposals=[row for row in rows['proposals']if row['id']in member_ids]
    expected_message=next(row for row in rows['messages']if row['id']==mid)
    actual_message=store.db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    fetched_changed=actual_message is not None and actual_message['fetched_at']!=expected_message['fetched_at']
    message_ok=actual_message is not None and {key:value for key,value in dict(actual_message).items()if key!='fetched_at'}=={
        key:value for key,value in expected_message.items()if key!='fetched_at'}
    expected_groups=sorted((row for row in rows['proposal_groups']if row['message_id']==mid),key=lambda row:row['id'])
    expected_group_ids={row['id']for row in expected_groups}
    expected_all_members=sorted((row for row in rows['proposal_group_members']if row['group_id']in expected_group_ids),
                                key=lambda row:(row['group_id'],row['position']))
    actual_groups=[dict(row)for row in store.db.execute('SELECT * FROM proposal_groups WHERE message_id=? ORDER BY id',(mid,))]
    actual_members=[dict(row)for row in store.db.execute('''SELECT m.* FROM proposal_group_members m
        JOIN proposal_groups g ON g.id=m.group_id WHERE g.message_id=? ORDER BY m.group_id,m.position''',(mid,))]
    actual_proposals=[dict(row)for row in store.db.execute('SELECT * FROM proposals WHERE message_id=? ORDER BY id',(mid,))]
    passed=(message_ok and actual_groups==expected_groups and actual_members==expected_all_members
            and actual_proposals==sorted(expected_proposals,key=lambda row:row['id']))
    return passed,fetched_changed,member_ids


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--cases',type=Path,required=True)
    parser.add_argument('--details',type=Path)
    parser.add_argument('--progress',action='store_true',help='Emit safe per-case counts as each case completes.')
    args=parser.parse_args(argv)
    os.umask(0o077)
    results=[];transcript=[];initial_provenance={}
    try:
        code_manifest,code_hash=evaluation_code_snapshot()
        from app.config import load_config
        from app.codex_runtime import CodexRuntime
        from app.core import AppStore,WARSAW,digest
        from app.source_admin import write_private_json
        from app.source_planner import prepare_source_proposals,SourcePlanner
        _,config_file_hash=read_evaluation_input(args.config)
        config=load_config(args.config,environ={})
        document,input_hash=read_evaluation_input(args.cases)
        if (not isinstance(document,dict) or not {'learner','cases'}<=set(document) or set(document)-{'learner','cases','seeds','quote_policy','legacy_rows'}
                or not isinstance(document['cases'],list) or not 1<=len(document['cases'])<=100
                or document.get('quote_policy','exact')not in {'exact','literal'}
                or not isinstance(document.get('seeds',[]),list)or len(document.get('seeds',[]))>100):
            raise ValueError('Invalid private evaluation shape.')
        learner=document['learner']
        references=[case.get('id')if isinstance(case,dict)else None for case in document['cases']]
        if (not isinstance(learner,dict)or not learner.get('id')
                or any(not isinstance(reference,str)or not reference or len(reference)>200 for reference in references)
                or len(set(references))!=len(references)):
            raise ValueError('Explicit learner and unique case references are required.')
        oracle={'quote_policy':document.get('quote_policy','exact'),'cases':[
            {key:value for key,value in case.items()if key!='source'}for case in document['cases']]}
        initial_provenance={'code_sha256':code_hash,'input_sha256':input_hash,'config_sha256':_json_digest(asdict(config)),
                            'config_file_sha256':config_file_hash,'oracle_sha256':_json_digest(oracle),'seeds_sha256':_json_digest(document.get('seeds',[])),
                            'legacy_rows_sha256':_json_digest(document.get('legacy_rows',{})),
                            'model':config.analysis_model,'effort':config.analysis_effort,'stability':'pending'}
        def verified_provenance(case=None):
            manifest,now_code=evaluation_code_snapshot()
            _,now_input=read_evaluation_input(args.cases)
            _,now_config=read_evaluation_input(args.config)
            stable=manifest==code_manifest and now_code==code_hash and now_input==input_hash and now_config==config_file_hash
            checked=dict(initial_provenance,stability='verified_at_checkpoint'if stable else'changed',
                         code_stable=now_code==code_hash,input_stable=now_input==input_hash,config_stable=now_config==config_file_hash)
            if case is not None:checked['case_sha256']=_json_digest(case)
            if not stable:
                initial_provenance.update(checked)
                raise ValueError('Evaluation code, input or configuration changed.')
            return checked
        verified_provenance()
        if args.details:
            outputs=[args.details]+[args.details.with_name(args.details.stem+f'-{kind}-{index:03d}.json')
                                    for index in range(len(document['cases']))for kind in ('extraction','case')]
            if any(path.exists()or path.is_symlink()for path in outputs):raise ValueError('Existing evidence cannot be replaced.')
        runtime=RecordingRuntime(CodexRuntime(config,read_only=True))
        with tempfile.TemporaryDirectory(prefix='librus-source-eval-') as temporary:
            store=AppStore(Path(temporary)/'state.sqlite3',config=replace(config,multi_source_mode='active'))
            try:
                if 'legacy_rows'in document:
                    import_legacy_rows(store,document['legacy_rows']);store.close()
                    store=AppStore(Path(temporary)/'state.sqlite3',config=replace(config,multi_source_mode='active'))
                    for legacy_index in range(len(document['legacy_rows']['proposal_groups'])):
                        if not legacy_offer_preservation(store,document['legacy_rows'],legacy_index)[0]:
                            raise ValueError('Ordinary migration changed explicit legacy offer rows.')
                # Bind only the explicit private fixture's learner. No account,
                # live state or external discovery is read by the evaluator.
                store.ingest_sources({'sources':[],'learner':learner,'complete_kinds':[],'status':'partial'})
                # These receipts authorize only local operation preparation in
                # the temporary fixture DB. This evaluator has no execution
                # path, connector probe or Calendar call.
                store.set_setting('source_activation_at','isolated-evaluation')
                store.set_setting('source_activation_learner',str(learner['id']))
                store.set_setting('source_activation_class',str(learner.get('class_id')or''))
                store.set_setting('source_activation_binding',digest([config.calendar_id,config.calendar_owner]))
                def ingest_fixture(data,current):
                    from app.source_store import source_identity
                    data=dict(data,learner_id=str(learner['id']))
                    if data['kind']=='message':
                        metadata=data.get('metadata',{})
                        if not isinstance(metadata,dict):raise ValueError('Invalid message fixture metadata.')
                        attachment_count=metadata.get('attachment_count',0)
                        if type(attachment_count)is not int or not 0<=attachment_count<=1000:
                            raise ValueError('Invalid bounded message attachment count.')
                        store.save_message({'id':data['upstream_id'],'subject':data.get('title',''),'sender':data.get('sender',''),
                                            'sent_at':data.get('published_at'),'text':data.get('text',''),
                                            'attachment_count':attachment_count})
                    # This complete *fixture* inventory uses the ordinary
                    # ingestion path and cannot tombstone earlier test sources.
                    inventory={};offset=0
                    while True:
                        page=store.list_sources(500,offset=offset)
                        inventory.update((s['id'],s)for s in page if s['kind']!='message')
                        if len(page)<500:break
                        offset+=len(page)
                    if data['kind']!='message':inventory[source_identity(data['kind'],data['upstream_id'],data['learner_id'])]=data
                    store.ingest_sources({'sources':list(inventory.values()),'learner':learner,
                        'complete_kinds':['announcement','school_event','substitution'],'status':'ok'},current)
                    # The isolated selected-message fixture is also complete;
                    # no live inbox discovery is claimed or performed here.
                    with store.db:
                        stamp=current.isoformat()
                        store.db.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?)',('message',stamp,stamp,'ok',None,1))
                    return store.get_source(source_identity(data['kind'],data['upstream_id'],data['learner_id']))
                seeds=document.get('seeds',[]);seed_targets=[]
                for seed in seeds:
                    if (not isinstance(seed,dict) or not {'source','proposals'}<=set(seed)
                            or set(seed)-{'source','proposals','decisions','current','events','legacy','legacy_proposal_source_hashes'}
                            or not isinstance(seed['proposals'],list)or len(seed['proposals'])>30
                            or type(seed.get('legacy',False))is not bool):
                        raise ValueError('Invalid explicit evaluation seed.')
                    current=fixture_current(seed.get('current'))
                    src=ingest_fixture(seed['source'],current)
                    if seed.get('legacy'):
                        if src['kind']!='message':raise ValueError('A legacy fixture requires a message source.')
                        proposals=store.save_proposals(src['upstream_id'],seed['proposals'],current)
                        applied={'outcomes':[{'proposal_id':proposal['id']}for proposal in proposals]}
                    else:
                        items=prepare_source_proposals(src,seed['proposals'],learner,current)
                        applied=SourcePlanner(store,runtime).apply(src,items,[(None,'distinct') for _ in items],
                            {'model':None,'effort':None,'prompt_version':'explicit-private-evaluation-seed','decision_reason':'Explicit private fixture metadata.'},store.source_decision_snapshot(),current)
                    if 'legacy_proposal_source_hashes'in seed:
                        hashes=seed['legacy_proposal_source_hashes']
                        if (not seed.get('legacy')or not isinstance(hashes,list)or len(hashes)!=len(seed['proposals'])
                                or len(applied['outcomes'])!=len(hashes)or any(not _legacy_digest(value)for value in hashes)
                                or any(store.db.execute('SELECT source_hash FROM proposals WHERE id=?',(outcome['proposal_id'],)).fetchone()[0]!=value
                                       for outcome,value in zip(applied['outcomes'],hashes))):
                            raise ValueError('Captured legacy proposal hashes do not match generated fixture rows.')
                    for decision in seed.get('decisions',[]):
                        if (not isinstance(decision,dict)or not {'index','decision'}<=set(decision)
                                or set(decision)-{'index','decision','patch'}or type(decision['index'])is not int
                                or not 0<=decision['index']<len(applied['outcomes'])):
                            raise ValueError('Invalid explicit seeded decision.')
                        target=applied['outcomes'][decision['index']]['proposal_id']
                        class SeedClock(datetime):
                            @classmethod
                            def now(cls,tz=None):
                                return current.astimezone(tz) if tz else current.replace(tzinfo=None)
                        # Replay an explicitly dated historical decision through
                        # the real local guards at that fixture's selected clock.
                        with patch('app.core.datetime',SeedClock):
                            store.resolve_proposal(target,decision['decision'],decision.get('patch'))
                    events=seed.get('events',[])
                    if not isinstance(events,list) or len(events)>10:
                        raise ValueError('Unbounded explicit event seed.')
                    if events:
                        operations=store.queue_operations(current)
                        from app.codex_runtime import event_fingerprint
                        for event in events:
                            if (not isinstance(event,dict)or set(event)!={'index','id'}or type(event['index'])is not int
                                    or not 0<=event['index']<len(applied['outcomes'])or not isinstance(event['id'],str)
                                    or not event['id']or len(event['id'])>200):
                                raise ValueError('Invalid minimal explicit event seed.')
                            target=applied['outcomes'][event['index']]['proposal_id']
                            selected=[op for op in operations if op['proposal_id']==target]
                            if len(selected)!=1:
                                raise ValueError('Explicit event seed is not a valid local operation.')
                            operation=selected[0]
                            snapshot=dict(operation['event'],id=event['id'])
                            store.record_result(operation['operation_id'],{'status':'applied','event_id':event['id'],
                                'snapshot':snapshot,'fingerprint':event_fingerprint(snapshot,owner_email=config.calendar_owner)})
                    seed_targets.append(seed_target_ids(store,seed,applied))
                baseline_events=_event_snapshots(store);invalid_offer_proposals=set()
                for index,case in enumerate(document['cases']):
                    if not isinstance(case,dict) or not {'id','source','expected'}<=set(case) or set(case)-{'id','source','expected','expected_scope','expected_outcomes','expected_targets','current','expected_new_operations','expected_new_actions','expected_new_proposals','expected_new_groups','forbidden_new_action_dates','repeat','expected_repeat_new_operations','expected_preserved_events','expected_group_sizes','select_offer_index','legacy_offer_group_index'}:
                        raise ValueError('Invalid evaluation case.')
                    expected=case['expected']
                    if not isinstance(expected,list)or len(expected)>30 or any(not isinstance(x,dict)or not isinstance(x.get('source_quote'),str)for x in expected):
                        raise ValueError('Every expected proposal needs an exact source_quote.')
                    if case.get('expected_scope','all')not in {'all','actionable'}:raise ValueError('Invalid explicit oracle scope.')
                    if 'expected_outcomes'in case and (not isinstance(case['expected_outcomes'],list)or len(case['expected_outcomes'])!=len(expected)
                            or any(value not in {'added','linked','enriched','review','ignored'}for value in case['expected_outcomes'])):
                        raise ValueError('Expected outcomes must align with expected proposal order.')
                    expected_target_ids=resolve_expected_targets(case,seeds,seed_targets)
                    for key in ('expected_new_operations','expected_new_actions','expected_new_proposals','expected_new_groups','expected_repeat_new_operations','select_offer_index','legacy_offer_group_index'):
                        if key in case and(type(case[key])is not int or case[key]<0):raise ValueError('Invalid expected count or selection.')
                    legacy_group=None
                    if 'legacy_offer_group_index'in case:
                        legacy_rows=document.get('legacy_rows')
                        if (not legacy_rows or case['legacy_offer_group_index']>=len(legacy_rows['proposal_groups'])or expected
                                or case['source'].get('kind')!='message'):
                            raise ValueError('Legacy offer preservation requires an explicit raw group and empty extraction oracle.')
                        legacy_group=legacy_rows['proposal_groups'][case['legacy_offer_group_index']]
                        if case['source'].get('upstream_id')!=legacy_group['message_id']:
                            raise ValueError('Legacy preservation source does not identify its raw message.')
                    if 'expected_group_sizes'in case and (not isinstance(case['expected_group_sizes'],list)
                            or len(case['expected_group_sizes'])>30
                            or any(type(size)is not int or not 2<=size<=30 for size in case['expected_group_sizes'])):
                        raise ValueError('Invalid expected alternative group sizes.')
                    if 'select_offer_index'in case and 'expected_group_sizes'in case and (
                            len(case['expected_group_sizes'])!=1 or case['select_offer_index']>=case['expected_group_sizes'][0]):
                        raise ValueError('Selection must identify a member of the one expected group.')
                    if case.get('expected_repeat_new_operations',0)!=0:raise ValueError('An unchanged repeat cannot create operations.')
                    if 'repeat'in case and type(case['repeat'])is not bool:raise ValueError('Invalid repeat policy.')
                    if 'forbidden_new_action_dates'in case and (not isinstance(case['forbidden_new_action_dates'],list)
                            or any(not isinstance(day,str)or len(day)!=10 for day in case['forbidden_new_action_dates'])):
                        raise ValueError('Invalid explicit forbidden action dates.')
                    verified_provenance(case)
                    current=fixture_current(case.get('current'))
                    before_counts={table:store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]for table in ('canonical_actions','proposals','proposal_groups')}
                    before_action_ids={row[0]for row in store.db.execute('SELECT id FROM canonical_actions')}
                    before_calls=runtime.model_calls
                    normalized=ingest_fixture(case['source'],current)
                    planner=SourcePlanner(store,runtime)
                    recording=analyze_pending_and_record(planner,normalized,current)
                    raw,actual,applied=recording['raw'],recording['prepared'],recording['applied']
                    outcomes=[o['outcome'] for o in applied['outcomes']]
                    comparison=compare_oracle(case,actual,outcomes,normalized['text'],document.get('quote_policy','exact'),
                        expected_target_ids=expected_target_ids,actual_target_ids=linked_outcome_targets(store,normalized,applied))
                    semantic_passed,quote_passed=comparison['semantic_passed'],comparison['quotes_passed']
                    matches=semantic_passed and quote_passed and comparison['outcomes_passed'] and comparison['targets_passed']
                    count_deltas={table:store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]-count for table,count in before_counts.items()}
                    if 'expected_new_actions'in case:matches=matches and count_deltas['canonical_actions']==case['expected_new_actions']
                    if 'expected_new_proposals'in case:matches=matches and count_deltas['proposals']==case['expected_new_proposals']
                    if 'expected_new_groups'in case:matches=matches and count_deltas['proposal_groups']==case['expected_new_groups']
                    forbidden=set(case.get('forbidden_new_action_dates',[]))
                    forbidden_safe=not any(item.get('kind')!='ignore'and any(str(item.get(key)or '')[:10]in forbidden for key in ('start','due_at'))
                        for row in store.db.execute('SELECT id,payload FROM canonical_actions')if row['id']not in before_action_ids
                        for item in [json.loads(row['payload'])])
                    matches=matches and forbidden_safe
                    reference=hashlib.sha256(str(case['id']).encode()).hexdigest()[:16]
                    case_hash=_json_digest(case)
                    checkpoint=verified_provenance(case)
                    if args.progress:print(json.dumps({'event':'extraction_completed','case_ref':reference,'semantic_passed':semantic_passed,
                        'case_sha256':case_hash,'code_sha256':code_hash,'input_sha256':input_hash,
                        'quotes_passed':quote_passed,'targets_passed':comparison['targets_passed'],'target_check_count':comparison['target_check_count'],
                        'expected_count':len(expected),'raw_count':len(raw),'actual_count':len(actual),
                        'actionable_count':sum(item.get('kind')!='ignore'for item in actual),'oracle_actual_count':comparison['oracle_actual_count'],
                        'provenance':checkpoint},ensure_ascii=True),flush=True)
                    if args.details:write_private_json(args.details.with_name(args.details.stem+f'-extraction-{index:03d}.json'),
                        {'id':case['id'],'source':normalized,'expected':expected,'actual':actual,'recording':recording,'current':current.isoformat(),
                         'provenance':checkpoint})
                    legacy_state_passed=None;legacy_fetched_changed=None;legacy_member_ids=set()
                    if legacy_group is not None:
                        legacy_state_passed,legacy_fetched_changed,legacy_member_ids=legacy_offer_preservation(store,legacy_rows,case['legacy_offer_group_index'])
                        legacy_state_passed=(legacy_state_passed and recording['applied']['status']=='skipped'
                            and runtime.model_calls-before_calls==0 and count_deltas['proposals']==0 and count_deltas['proposal_groups']==0)
                        matches=matches and legacy_state_passed
                    groups=store.list_proposal_groups(message_id=legacy_group['message_id']if legacy_group is not None else None,current=current)
                    group_sizes=sorted(len(group['members']) for group in groups)
                    group_sizes_passed=group_sizes==sorted(case['expected_group_sizes'])if 'expected_group_sizes'in case else None
                    case_errors=[];offer_failed=group_sizes_passed is False or legacy_state_passed is False
                    if legacy_state_passed is False:case_errors.append('legacy_offer_state_changed')
                    if group_sizes_passed is False:case_errors.append('offer_group_sizes_mismatch')
                    if offer_failed:matches=False
                    selection_status='not_requested';selection_passed=None;selected_member_ids=set();max_one_passed=None
                    if 'select_offer_index' in case:
                        case_proposals=legacy_member_ids if legacy_state_passed else{
                            outcome['proposal_id']for item,outcome in zip(actual,applied['outcomes'])if item.get('kind')!='ignore'}
                        possible=(not offer_failed and len(groups)==1 and len(groups[0]['members'])>=2
                            and case['select_offer_index']<len(groups[0]['members'])
                            and {member['id']for member in groups[0]['members']}<=case_proposals
                            and (legacy_group is None or groups[0]['id']==legacy_group['id']))
                        selection_passed=False;selection_status='not_possible'
                        if possible:
                            group=groups[0];chosen=group['members'][case['select_offer_index']]
                            try:
                                store.resolve_proposal_group(group['id'],'select',chosen['id'],expected_revision=group['revision'],current=current)
                                selection_passed=True;selection_status='selected'
                                selected_member_ids={member['id']for member in group['members']}
                            except ValueError:selection_status='rejected'
                        if not selection_passed:
                            offer_failed=True;matches=False;case_errors.append('offer_selection_'+selection_status)
                    if offer_failed:
                        invalid_offer_proposals.update(outcome['proposal_id']for outcome in applied['outcomes'])
                        invalid_offer_proposals.update(legacy_member_ids)
                    # A failed model offer is evidence, not a fixture/programmer
                    # exception. Never let its pending work leak into a later
                    # case's global queue, and never call an unrun check passed.
                    blocked_pending=any(row[0]in invalid_offer_proposals for row in store.db.execute("SELECT id FROM proposals WHERE status='pending'"))
                    operations_checked=not offer_failed and not blocked_pending
                    operation_status='completed'if operations_checked else'not_run_offer_failure'if offer_failed else'not_run_prior_offer_pending'
                    if not operations_checked:
                        matches=False
                        if not offer_failed:case_errors.append('prior_invalid_offer_pending')
                    queued=store.queue_operations(current)if operations_checked else None
                    if operations_checked and 'expected_new_operations' in case:matches=matches and len(queued)==case['expected_new_operations']
                    if selection_passed and operations_checked:
                        max_one_passed=sum(operation['proposal_id']in selected_member_ids for operation in queued)<=1
                        matches=matches and max_one_passed
                    ignored_ids={outcome['proposal_id']for item,outcome in zip(actual,applied['outcomes'])if item.get('kind')=='ignore'}
                    ignored_safe=not any(operation['proposal_id']in ignored_ids for operation in queued)if operations_checked else None
                    matches=matches and ignored_safe is True
                    repeat_new_operations=None;repeat_applied=None;repeat_calls=None;repeat_state_unchanged=None;repeat_timestamp_changes=None
                    replay_passed=None
                    if case.get('repeat')and operations_checked:
                        repeat_applied=repeat_fixture(store,planner,ingest_fixture,case['source'],current)
                        repeat_new_operations=repeat_applied['new_local_operations'];repeat_calls=repeat_applied['model_calls']
                        repeat_state_unchanged=repeat_applied['state_unchanged'];repeat_timestamp_changes=repeat_applied['proposal_timestamp_changes']
                        replay_passed=repeat_calls==0 and repeat_new_operations==0 and repeat_state_unchanged
                        matches=matches and replay_passed
                    events_now=_event_snapshots(store)
                    preserved=all(event_id in baseline_events and events_now.get(event_id)==baseline_events[event_id]for event_id in case.get('expected_preserved_events',[]))
                    matches=matches and preserved
                    checkpoint=verified_provenance(case)
                    results.append({'case_ref':reference,'case_sha256':case_hash,'passed':matches,'expected_count':len(expected),'actual_count':len(actual),'outcomes':outcomes,'semantic_passed':semantic_passed,'quotes_passed':quote_passed,
                                    'raw_count':len(raw),'actionable_count':sum(item.get('kind')!='ignore'for item in actual),'oracle_actual_count':comparison['oracle_actual_count'],'expected_scope':case.get('expected_scope','all'),
                                    'outcomes_passed':comparison['outcomes_passed'],'targets_passed':comparison['targets_passed'],'target_check_count':comparison['target_check_count'],
                                    'new_local_actions':count_deltas['canonical_actions'],'new_local_proposals':count_deltas['proposals'],'new_local_groups':count_deltas['proposal_groups'],'case_model_calls':runtime.model_calls-before_calls,
                                    'new_local_operations':len(queued)if operations_checked else None,'repeat_new_local_operations':repeat_new_operations,'repeat_model_calls':repeat_calls,'repeat_state_unchanged':repeat_state_unchanged,'repeat_proposal_timestamp_changes':repeat_timestamp_changes,
                                    'ignored_operations_safe':ignored_safe,'forbidden_new_action_dates_safe':forbidden_safe,'preserved_seed_events':preserved,'offer_group_sizes':group_sizes,
                                    'offer_group_sizes_passed':group_sizes_passed,'offer_selection_status':selection_status,'offer_selection_passed':selection_passed,
                                    'offer_max_one_operation_passed':max_one_passed,'operation_checks_performed':operations_checked,'operation_check_status':operation_status,
                                    'acceptance_kind':'existing_legacy_offer_state'if legacy_group is not None else'source_analysis',
                                    'analysis_status':recording['applied']['status'],'legacy_offer_state_passed':legacy_state_passed,
                                    'legacy_message_fetch_timestamp_changed':legacy_fetched_changed,'legacy_offer_member_count':len(legacy_member_ids)if legacy_group is not None else None,
                                    'replay_checks_passed':replay_passed,'case_errors':case_errors,'provenance':checkpoint})
                    if args.progress:print(json.dumps({'event':'case_completed',**results[-1]},ensure_ascii=True),flush=True)
                    transcript.append({'id':case['id'],'source':normalized,'expected':expected,'actual':actual,'applied':applied,'repeat':repeat_applied,'passed':matches,
                                       'recording':recording,'comparison':comparison,'metadata':recording['metadata'],'current':current.isoformat(),
                                       'events':events_now,'state':durable_state_snapshot(store),'groups':store.list_proposal_groups(current=current)})
                    if args.details:write_private_json(args.details.with_name(args.details.stem+f'-case-{index:03d}.json'),
                        {'result':results[-1],'transcript':transcript[-1],'provenance':checkpoint})
            finally:store.close()
        provenance={**verified_provenance(),'stability':'verified',
                    'prompt_versions':sorted({entry['metadata'].get('prompt_version','unknown')for entry in transcript if entry.get('metadata')}),
                    'verification_versions':sorted({call['metadata']['version']for entry in transcript
                        for call in entry['recording']['runtime_calls']if call['method']=='verify_source_action'
                        and call.get('metadata')and call['metadata'].get('version')}),
                    'recovery_versions':sorted({call['metadata']['version']for entry in transcript
                        for call in entry['recording']['runtime_calls']if call['method']=='recover_source_action_evidence'
                        and call.get('metadata')and call['metadata'].get('version')})}
        if args.details:write_private_json(args.details,{'results':results,'transcript':transcript,'provenance':provenance,'code_manifest':code_manifest})
        output={'status':'passed' if all(x['passed'] for x in results) else 'failed','cases':len(results),'passed':sum(x['passed'] for x in results),'calendar_writes':0,'calendar_reads':0,'discovery_calls':0,'model_calls':runtime.model_calls,'results':results,'provenance':provenance}
        print(json.dumps(output,ensure_ascii=True));return 0 if output['status']=='passed' else 1
    except Exception as error:
        if args.details and 'transcript' in locals():
            try:write_private_json(args.details,{'status':'incomplete','results':results,'transcript':transcript,'error_type':type(error).__name__,'provenance':initial_provenance})
            except Exception:pass
        print(json.dumps({'status':'failed','error_type':type(error).__name__,'completed_cases':len(locals().get('results',[])),
                          'private_response_logged':False,'calendar_writes':0,'calendar_reads':0,'discovery_calls':0,'provenance':initial_provenance}));return 1


if __name__=='__main__':raise SystemExit(main())
