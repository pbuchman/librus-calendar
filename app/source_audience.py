"""Observed class-calendar audience, separate from action/participation evidence.

The trusted collector mints this envelope only after checking the complete served
view and unchanged selected context. Model output never supplies this authority.
Source ingestion validates the envelope; the persisted source version includes it.
"""
from copy import deepcopy
import hashlib
import json

AUDIENCE_KEY='served_class_calendar'
AUDIENCE_VERSION='served-class-calendar-v1'
VIEW_CONTRACT={
    'version':AUDIENCE_VERSION,
    'routes':['https://synergia.librus.pl/rodzic/index','https://synergia.librus.pl/informacja','https://synergia.librus.pl/terminarz'],
    'status':200,'context':'same-selected-learner-class-before-after-month-restoration',
    'calendar_controls':['scheduleForm:post:/terminarz','hidden:requestkey','select:miesiac','select:rok'],
    'optional_external_select':'language:Język','other_controls':'refused',
}


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def audience_source_fingerprint(source):
    """Stable observed content, before the envelope; no nonce/fetch-time cycle."""
    value={key:source.get(key)for key in ('kind','upstream_id','learner_id','title','sender','published_at','updated_at','text','url')}
    for key in ('title','sender','text','url'):value[key]=str(value.get(key)or'')
    value['deleted']=source.get('deleted')is True
    value['metadata']={key:item for key,item in(source.get('metadata')or{}).items()
                       if key not in {AUDIENCE_KEY,'fetched_at','page','window_start','window_end'}}
    return _digest(value)


def mint_calendar_audience(source,context):
    """Collector-only observation factory; never infer scope from source text."""
    if (source.get('kind')!='school_event'or not context.get('id')or not context.get('class_id')
            or source.get('learner_id')!=context['id']or source.get('deleted')):
        raise ValueError('Invalid served calendar audience context.')
    return {'version':AUDIENCE_VERSION,'learner_id':str(context['id']),'class_id':str(context['class_id']),
            'view_sha256':_digest(VIEW_CONTRACT),'source_sha256':audience_source_fingerprint(source)}


def valid_calendar_audience(source,context):
    metadata=source.get('metadata')or{}
    if not isinstance(metadata,dict):return False
    proof=metadata.get(AUDIENCE_KEY)
    return (isinstance(proof,dict)and set(proof)=={'version','learner_id','class_id','view_sha256','source_sha256'}
            and source.get('kind')=='school_event'and not source.get('deleted')and source.get('status')!='retained'
            and not metadata.get('retained')and context.get('verified',True)is True
            and bool(context.get('id'))and bool(context.get('class_id'))
            and proof['version']==AUDIENCE_VERSION and proof['view_sha256']==_digest(VIEW_CONTRACT)
            and proof['learner_id']==source.get('learner_id')==context['id']and proof['class_id']==context['class_id']
            and proof['source_sha256']==audience_source_fingerprint(source))


def calendar_recipient_basis(source,context):
    if not valid_calendar_audience(source,context):return None
    return {'kind':AUDIENCE_KEY,'proof':deepcopy(source['metadata'][AUDIENCE_KEY]),
            'source_hash':source.get('content_hash')}
