"""Pure planner-version contract for preview readers, workers and admission."""
import json
import re

SOURCE_PREVIEW_PLANNER_VERSION = 'source-preview-planner-v3'


def preview_is_current(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return False
    return isinstance(payload, dict) and payload.get('planner_version') == SOURCE_PREVIEW_PLANNER_VERSION


def current_preview_sql(alias='p'):
    """Select only the fixed metadata field, including on malformed legacy JSON."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', alias):
        raise ValueError('Invalid preview table alias.')
    return (f"CASE WHEN json_valid({alias}.payload) THEN "
            f"COALESCE(json_extract({alias}.payload,'$.planner_version')='{SOURCE_PREVIEW_PLANNER_VERSION}',0) ELSE 0 END")


PREVIEW_RETRY_SETTINGS = ('source_activation_incomplete', 'source_learner_context',
                          'source_learner_id', 'source_learner_verified')


def incomplete_activation_attempt(value):
    if not value:
        return None
    try:
        attempt = json.loads(value)
        if (not isinstance(attempt, dict) or set(attempt) != {'token', 'plan', 'sources'}
                or any(not isinstance(attempt[key], str) or not attempt[key].strip() for key in ('token', 'plan'))
                or not isinstance(attempt['sources'], list) or not 1 <= len(attempt['sources']) <= 5000
                or any(not isinstance(item, dict) or set(item) != {'id', 'hash'}
                       or any(not isinstance(item[key], str) or not item[key].strip() for key in ('id', 'hash'))
                       for item in attempt['sources'])
                or len({item['id'] for item in attempt['sources']}) != len(attempt['sources'])):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Incomplete activation needs a valid fresh retry plan.') from None
    return attempt


def preview_queue_sql(settings, alias='s'):
    """Only an exact incomplete selection can refresh already analyzed sources."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', alias):
        raise ValueError('Invalid source table alias.')
    ordinary = f"{alias}.status IN ('pending','failed')"
    try:
        attempt = incomplete_activation_attempt(settings.get('source_activation_incomplete'))
        context = json.loads(settings.get('source_learner_context') or '{}')
    except (ValueError, TypeError):
        return ordinary, ()
    learner = settings.get('source_learner_id')
    if (not attempt or not isinstance(context, dict) or not isinstance(learner, str) or not learner.strip()
            or context.get('id') != learner or not isinstance(context.get('class_id'), str)
            or not context['class_id'].strip()
            or settings.get('source_learner_verified') != 'true'):
        return ordinary, ()
    retry = (f"{alias}.status='analyzed' AND {alias}.learner_id=? AND EXISTS "
             f"(SELECT 1 FROM json_each(?) retry WHERE json_extract(retry.value,'$.id')={alias}.id "
             f"AND json_extract(retry.value,'$.hash')={alias}.content_hash) AND EXISTS "
             f"(SELECT 1 FROM source_previews receipt WHERE receipt.source_id={alias}.id "
             f"AND receipt.source_hash={alias}.content_hash AND receipt.status IN ('activated','ready','failed'))")
    return f'({ordinary} OR ({retry}))', (learner, json.dumps(attempt['sources']))
