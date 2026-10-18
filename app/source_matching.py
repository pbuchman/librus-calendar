"""Shared deterministic exclusions; uncertainty never proves distinct identity."""


def _hard_match_decision(item, old):
    """Typed safety boundaries apply to BOTH lexical and semantic proof."""
    ai, bi = item.get('action_identity') or {}, old.get('action_identity') or {}
    if ai and bi and ai.get('action_type') != bi.get('action_type'):
        return 'distinct'
    if item.get('temporal_kind', 'event') != old.get('temporal_kind', 'event'):
        return 'distinct'
    if item.get('closed_offer_research'):
        from .core import start_key
        if start_key(item) != start_key(old):
            return 'distinct'
    a_recipient, b_recipient = item.get('recipient') or {}, old.get('recipient') or {}
    for scope, key in (('class', 'class_id'), ('learner', 'learner_id')):
        if (a_recipient.get('scope') == b_recipient.get('scope') == scope
                and a_recipient.get(key) and b_recipient.get(key)
                and a_recipient[key] != b_recipient[key]):
            return 'distinct'
    # Alternative time choices are separate candidate actions, even for one offer.
    if item.get('alternative_group') and old.get('alternative_group') and item.get('start') != old.get('start'):
        return 'distinct'
    return None


def candidate_is_distinct(item, old):
    """Apply existing exclusions only to known fields; keep unknown inventory."""
    if old.get('inventory_only'):
        return False
    left, right = dict(item), dict(old)
    action_types = {'attend', 'bring', 'submit', 'pay', 'prepare', 'collect', 'inform', 'other'}
    for value in (left, right):
        identity = value.get('action_identity')
        if (not isinstance(identity, dict) or not isinstance(identity.get('action_type'), str)
                or identity['action_type'] not in action_types):
            value['action_identity'] = None
        recipient = value.get('recipient')
        value['recipient'] = dict(recipient) if isinstance(recipient, dict) else {}
        for key in ('class_id', 'learner_id'):
            identifier = value['recipient'].get(key)
            if not isinstance(identifier, str) or not identifier.strip():
                value['recipient'][key] = None
        if value.get('closed_offer_research') is not True:
            value['closed_offer_research'] = False
        if not isinstance(value.get('alternative_group'), str) or not value['alternative_group'].strip():
            value.pop('alternative_group', None)
    temporal_kinds = {'event', 'deadline', 'reminder'}
    if any(not isinstance(value.get('temporal_kind'), str) or value['temporal_kind'] not in temporal_kinds
           for value in (left, right)):
        # The matching validator's legacy default is not evidence of a type.
        left['temporal_kind'] = right['temporal_kind'] = 'event'
    if left.get('closed_offer_research') or (left.get('alternative_group') and right.get('alternative_group')):
        from .core import start_key
        if start_key(left) is None or start_key(right) is None:
            left['closed_offer_research'] = False
            left.pop('alternative_group', None)
    return _hard_match_decision(left, right) == 'distinct'
