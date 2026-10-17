"""Incremental source analysis and evidence-bound canonical action planning.

No Calendar tool is called here. A preview cannot update proposals, canonical
links, decisions or operations. Active plans compare source/decision snapshots
inside the committing transaction before the original write guards run.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import uuid

from .core import WARSAW, as_datetime, digest, normalize_proposal, utcnow
from .source_store import normalized_text, source_message, authority_links, CORROBORATION_RELATION, EXPIRED_FACT_RELATION, CLOSED_OFFER_RELATION


def _literal(quote,text):
    return isinstance(quote,str) and len(quote.strip())>=4 and normalized_text(quote) in normalized_text(text)


def _substantive_words(text):
    generic={'szkoła','szkolny','school','class','klasa','dzieci','rodzice','parents','dzień','termin','event','zajęcia','proszę','librus',
             'sala','sali','salę','aula','auli','rano','godzina','godziny','godzinie','uczeń','uczniowie','uczniów',
             'wydarzenie','spotkanie','informacja','ważne','szczegóły','kalendarz'}
    date_or_context=('stycz','lutego','marca','kwiet','maja','czerw','lipca','sierp','wrześ','wrzes','paździer','pazdzier','listopad','grudn',
                     'godzin','szkol','szkoł','klas','rodzic','nauczyc','wychow')
    return {word for word in re.findall(r'[^\W\d_]{4,}',normalized_text(text))
            if word not in generic and not word.startswith(date_or_context)}


def _recipient_evidence(quote,source):
    # Applicability can be stated in an observed heading while the body contains
    # the task. Each field is independent: joining them would invent a quotation.
    return any(_literal(quote,source.get(field,'')) for field in ('title','text'))


def _identity(raw,source):
    identity=raw.get('action_identity')
    if not isinstance(identity,dict) or set(identity)!={'action_type','object','occasion','evidence'}:
        return False
    return (identity['action_type'] in {'attend','bring','submit','pay','prepare','collect','inform','other'}
            and isinstance(identity['object'],str) and bool(identity['object'].strip())
            and isinstance(identity['occasion'],str) and _literal(identity['evidence'],source['text']))


def _source_fact_reason(item,source):
    if not _literal(item.get('source_quote'),source.get('text','')):return 'Literal source evidence is missing.'
    if not _identity(item,source):return 'Typed action identity lacks source evidence.'
    if item.get('date_certainty')not in {'explicit','inferred'}:return 'Date certainty requires review.'
    if item.get('date_certainty')=='inferred':
        try:as_datetime(source.get('published_at'))
        except (ValueError,TypeError,OverflowError):return 'An inferred date lacks observed publication evidence.'
    return None


def _unverified_recipient(item,context):
    recipient=item.get('recipient')or{}
    if recipient.get('scope')not in {'unknown','learner','class','all'}or recipient.get('condition')not in (None,'none','unknown'):return False
    for key,known in (('learner_id',context.get('id')),('class_id',context.get('class_id'))):
        if recipient.get(key)and str(recipient[key])!=str(known or''):return False
    return True


def _participation_research(item,source,context):
    recipient=item.get('recipient')or{}
    if (_source_fact_reason(item,source)or item.get('kind')!='create'or item.get('temporal_kind')!='event'
            or item.get('activity_scope')!='extracurricular'or recipient.get('scope')not in {'conditional','all','class','learner'}
            or recipient.get('condition')!='participation'):return False
    return all(not recipient.get(key)or str(recipient[key])==str(known or'')
        for key,known in (('learner_id',context.get('id')),('class_id',context.get('class_id'))))


def _source_action_reason(item,source,context):
    reason=_source_fact_reason(item,source)
    if reason:return reason
    recipient=item.get('recipient')or{}
    if recipient.get('condition')not in (None,'none'):return 'Conditional recipient applicability requires review.'
    if recipient.get('scope') not in {'learner','class','all'}:return 'Conditional or unknown recipient requires review.'
    if recipient.get('scope')=='learner' and str(recipient.get('learner_id')or'')!=str(context.get('id')or''):return 'Recipient learner is not the verified selected learner.'
    if recipient.get('scope')=='class' and str(recipient.get('class_id')or'')!=str(context.get('class_id')or''):return 'Recipient class is not the verified selected class.'
    if not _recipient_evidence(recipient.get('evidence'),source):return 'Recipient applicability lacks literal evidence.'
    return None


def prepare_source_proposals(source,raws,context,current=None):
    """Typed applicability is a hard boundary; model flags never grant approval."""
    if not isinstance(raws,list) or len(raws)>30:raise ValueError('Unbounded source analysis.')
    prepared=[]
    for raw in raws:
        if not isinstance(raw,dict):raise ValueError('Invalid source action.')
        raw=dict(raw,source_id=source['id'],source_hash=source['content_hash'],source_kind=source['kind'],
                 user_approved=False,user_edited=False,user_rejected=False,deletion_approved=False,
                 provenance={'source_id':source['id'],'source_hash':source['content_hash'],'quote':raw.get('source_quote','')},
                 source_version_at=source.get('updated_at') or source.get('published_at'))
        recipient=raw.get('recipient') or {'scope':'unknown','learner_id':None,'class_id':None,'evidence':''}
        informational=(source['kind']=='substitution' or raw.get('informational') is True
                       or raw.get('date_certainty')=='undated' or recipient.get('scope')=='other'
                       or recipient.get('condition')=='role')
        if informational:raw.update(kind='ignore',informational=True)
        try:item=normalize_proposal(raw,current)
        except (ValueError,TypeError,OverflowError):
            item={**raw,'kind':'create','status':'review','needs_review':True,'review_reason':'Invalid or uncertain date; manual correction required.',
                  'start':raw.get('start'),'end':raw.get('end'),'title':str(raw.get('title') or 'Niejasny termin')[:250]}
        if item['kind']!='ignore':
            # Past facts still require applicability before attaching provenance.
            reason=_source_action_reason(item,source,context)
            if reason:
                unverified=not _source_fact_reason(item,source)and _unverified_recipient(item,context)
                if (unverified and item['kind']=='create'and item.get('activity_scope')=='school'
                        and item.get('status')=='ignored'):
                    item.update(expired_without_authority=True,applicability_unverified=True,needs_review=False,
                        review_reason='Expired school fact; recipient applicability remains unverified. No write or identity authority.')
                else:item.update(status='review',needs_review=True,review_reason=reason)
                if (unverified and item['kind']=='create'and item.get('temporal_kind')=='event'
                        and item.get('activity_scope')=='school'and not item.get('alternative_group')and not item.get('optional')):
                    item['corroboration_only']=True
                if _participation_research(item,source,context):item['closed_offer_research']=True
        prepared.append(item)
    deduplicated=[];known=set()
    for item in prepared:
        identity=item.get('action_identity') or {};recipient=item.get('recipient') or {}
        if identity:
            key=digest([item.get('kind'),item.get('temporal_kind'),item.get('activity_scope'),
                normalized_text(identity.get('action_type')),normalized_text(identity.get('object')),normalized_text(identity.get('occasion')),
                item.get('start'),item.get('end'),item.get('due_at'),item.get('all_day'),item.get('optional'),
                recipient.get('scope'),recipient.get('learner_id'),recipient.get('class_id'),recipient.get('condition'),item.get('alternative_group')])
        else:key=digest(item)
        if key not in known:known.add(key);deduplicated.append(item)
    prepared=deduplicated
    from .offer_groups import validate_alternatives
    validate_alternatives(source_message(source),prepared)
    for item in prepared:
        if item.get('alternative_group') and item['status']!='ignored':
            item.update(status='review',needs_review=True,review_reason='Choose one alternative from this offer.')
    return prepared


def verified_match(source,item,candidates,result):
    """Only one evidence-grounded match can transfer any durable human intent.

    A model's match ID, title or date is insufficient. Both literal quotations and
    a shared substantive anchor must be in the respective source evidence.
    """
    if not isinstance(result,dict) or result.get('decision') not in {'match','distinct','uncertain'}:
        return None,'uncertain'
    if result['decision']!='match':return None,result['decision']
    matches=[x for x in candidates if x['id']==result.get('action_id')]
    if len(matches)!=1:return None,'uncertain'
    action=matches[0]
    old=action['payload']
    candidate_quote=result.get('candidate_quote')
    if not _literal(result.get('source_quote'),source['text']) or not isinstance(candidate_quote,str)or len(candidate_quote.strip())<4:
        return None,'uncertain'
    proofs=[]
    for proof in action.get('evidence',[]):
        if result.get('evidence_id') and result['evidence_id']!=proof['id']:continue
        saved=proof['source_quote']
        # Context cannot establish a different obligation elsewhere in the body:
        # the model must also quote the original saved action evidence. A long
        # saved quote may itself be the bounded context.
        if candidate_quote in proof['text'] and (saved in candidate_quote or candidate_quote in saved):
            proofs.append(proof)
    if len(proofs)!=1:return None,'uncertain'
    anchors=result.get('anchors')
    if not isinstance(anchors,list) or not 1<=len(anchors)<=4:return None,'uncertain'
    anchor_text=' '.join(str(a) for a in anchors)
    # Dates, generic school/parent language and titles cannot establish identity.
    significant=_substantive_words(anchor_text)
    if len(significant)<2:return None,'uncertain'
    if any(not _literal(a,result['source_quote']) or not _literal(a,candidate_quote) for a in anchors):return None,'uncertain'
    # A surrounding paragraph cannot reassign an untyped legacy action's
    # purpose. Some claimed substantive grounding must attach to its ORIGINAL
    # quote, not merely to a different obligation in expanded context.
    if not significant&_substantive_words(proofs[0]['source_quote']):return None,'uncertain'
    reason=_hard_match_decision(item,old)
    return (None,reason)if reason else(action,'match')


def _hard_match_decision(item,old):
    """Typed safety boundaries apply to BOTH lexical and semantic proof."""
    ai,bi=item.get('action_identity') or {},old.get('action_identity') or {}
    if ai and bi and ai.get('action_type')!=bi.get('action_type'):
        return 'distinct'
    if item.get('temporal_kind','event')!=old.get('temporal_kind','event'):
        return 'distinct'
    if item.get('closed_offer_research'):
        from .core import start_key
        if start_key(item)!=start_key(old):return 'distinct'
    a_recipient,b_recipient=item.get('recipient') or {},old.get('recipient') or {}
    for scope,key in (('class','class_id'),('learner','learner_id')):
        if (a_recipient.get('scope')==b_recipient.get('scope')==scope
                and a_recipient.get(key) and b_recipient.get(key)
                and a_recipient[key]!=b_recipient[key]):
            return 'distinct'
    # Alternative time choices are separate candidate actions, even for one offer.
    if item.get('alternative_group') and old.get('alternative_group') and item.get('start')!=old.get('start'):
        return 'distinct'
    return None


def verified_semantic_match(source,item,candidates,result):
    """Validate independent semantic confirmation against ORIGINAL quotes."""
    if (not isinstance(result,dict)or result.get('decision')!='match'
            or any(result.get(key)is not True for key in ('same_purpose','same_occasion','recipient_compatible','unique_target'))):
        return None,'uncertain'
    selected=[action for action in candidates if action['id']==result.get('action_id')]
    if len(selected)!=1:return None,'uncertain'
    action=selected[0];reason=_hard_match_decision(item,action['payload'])
    if reason:return None,reason
    quote=result.get('source_quote');old_quote=result.get('candidate_quote')
    if (not isinstance(quote,str)or len(quote.strip())<4 or quote not in source['text']
            or quote not in item.get('source_quote','')or not _substantive_words(quote)
            or not isinstance(old_quote,str)or len(old_quote.strip())<4 or not _substantive_words(old_quote)):
        return None,'uncertain'
    purpose=result.get('source_purpose_quote');field=result.get('source_purpose_field')
    if not isinstance(purpose,str)or len(purpose.strip())<4 or not _substantive_words(purpose):return None,'uncertain'
    if field=='text':
        if purpose not in quote:return None,'uncertain'
    elif field=='title':
        if source.get('kind')!='school_event'or purpose not in source.get('title',''):return None,'uncertain'
        # A structured heading cannot move its observed event to another date.
        observed=source.get('metadata',{}).get('event_start')
        if not observed:return None,'uncertain'
        try:
            if as_datetime(observed).astimezone(WARSAW).date()!=as_datetime(item['start']).astimezone(WARSAW).date():return None,'uncertain'
        except (ValueError,TypeError,OverflowError):return None,'uncertain'
    else:return None,'uncertain'
    proofs=[proof for proof in action.get('original_evidence',[])+action.get('reconstructed_evidence',[])
            if proof['id']==result.get('evidence_id')and old_quote in proof['text']]
    if len(proofs)!=1:return None,'uncertain'
    if proofs[0].get('kind')=='current_origin_reconstructed'and any(
            not isinstance(proofs[0].get(key),str)or proofs[0][key]not in old_quote for key in ('purpose_quote','date_quote')):
        return None,'uncertain'
    # Identical original proof for another compatible action cannot establish a
    # unique target merely because a model selected one ID.
    if any(other['id']!=action['id']and not _hard_match_decision(item,other['payload'])
           and any(old_quote in proof['text']for proof in other.get('original_evidence',[])+other.get('reconstructed_evidence',[]))for other in candidates):
        return None,'uncertain'
    return action,'match'


def recovered_action_evidence(candidates,result):
    """Register only independently extracted, action-specific current facts."""
    from .codex_runtime import SOURCE_RECOVERY_VERSION
    facts=result.get('facts')if isinstance(result,dict)else None
    if (not isinstance(facts,list)or len(facts)!=len(candidates)
            or any(not isinstance(fact,dict)for fact in facts)
            or {fact.get('action_id')for fact in facts}!={action['id']for action in candidates}):return {}
    recovered={}
    for action in candidates:
        entries=[fact for fact in facts if fact['action_id']==action['id']]
        if len(entries)!=1:return {}
        fact=entries[0];origin=action.get('recovery_origin')or{};old=action['payload']
        if fact.get('decision')!='fact'or any(fact.get(key)is not True for key in ('same_purpose','same_day','recipient_compatible')):continue
        quote,purpose,date=fact.get('source_quote'),fact.get('purpose_quote'),fact.get('date_quote')
        if (fact.get('source_id')!=origin.get('source_id')or fact.get('source_hash')!=origin.get('source_hash')
                or origin.get('proposal_source_hash')!=origin.get('source_hash')or origin.get('canonical_payload_hash')!=digest(old)
                or not isinstance(quote,str)or not 4<=len(quote)<=1200 or quote not in origin.get('body','')
                or not isinstance(purpose,str)or not 4<=len(purpose)<=300 or purpose not in quote
                or not isinstance(date,str)or not 4<=len(date)<=300 or date not in quote
                or not _substantive_words(purpose)&_substantive_words(old.get('title',''))):continue
        try:
            if fact.get('day')!=as_datetime(old['start']).astimezone(WARSAW).date().isoformat():continue
        except (ValueError,TypeError,OverflowError):continue
        # Register only the minimal literal span carrying this action's purpose
        # and date. Broad returned text cannot smuggle neighboring obligations
        # into the independent verifier's candidate evidence.
        if quote.count(purpose)!=1 or quote.count(date)!=1:continue
        start=min(quote.index(purpose),quote.index(date));end=max(quote.index(purpose)+len(purpose),quote.index(date)+len(date))
        fact_text=quote[start:end]
        proof={'source_id':origin['source_id'],'source_hash':origin['source_hash'],'text':fact_text,
               'purpose_quote':purpose,'date_quote':date,'day':fact['day'],
               'kind':'current_origin_reconstructed','version':SOURCE_RECOVERY_VERSION,
               'proposal_source_hash':origin['proposal_source_hash'],'canonical_payload_hash':origin['canonical_payload_hash']}
        proof['id']=digest([SOURCE_RECOVERY_VERSION,action['id'],proof])[:32]
        recovered[action['id']]=[proof]
    return recovered


def _synthetic_match(source,item,candidates):
    """Deterministic fallback for older injected runtimes; never guesses by date."""
    identity=item.get('action_identity') or {}
    matches=[]
    for action in candidates:
        old=action['payload']; prior=old.get('action_identity') or {}
        if (identity and prior and all(normalized_text(identity.get(k))==normalized_text(prior.get(k)) for k in ('action_type','object','occasion'))
                and item.get('temporal_kind')==old.get('temporal_kind')
                and (not item.get('alternative_group') or item.get('start')==old.get('start'))):matches.append(action)
    if len(matches)==1:
        action=matches[0]
        quote=action['payload'].get('source_quote','')
        anchors=[str(identity.get('object') or ''),str(identity.get('occasion') or '')]
        anchors=[x for x in anchors if len(x)>=4 and _literal(x,source['text']) and _literal(x,quote)]
        return verified_match(source,item,candidates,{'decision':'match','action_id':action['id'],'source_quote':item.get('source_quote'),
                    'candidate_quote':quote,'anchors':anchors})
    return None,'uncertain' if candidates else 'distinct'


class SourcePlanner:
    def __init__(self,store,runtime):self.store,self.runtime=store,runtime

    def analyze(self,source,current=None,preview=False):
        context=json.loads(self.store._setting('source_learner_context','{}'))
        if source['kind']=='substitution':
            raws=[];metadata={'model':None,'effort':None,'prompt_version':'substitution-policy-v1','decision_reason':'Substitutions are informational; no personal events.'}
        else:
            self.runtime.last_analysis_metadata=None
            if not hasattr(self.runtime,'analyze_source'):
                raise ValueError('Typed source analysis runtime is required.')
            raws=self.runtime.analyze_source(source,context)
            metadata=getattr(self.runtime,'last_analysis_metadata',None) or {}
        prepared=prepare_source_proposals(source,raws,context,current)
        if preview and hasattr(self.runtime,'verify_source_action')and hasattr(self.runtime,'reserve_source_plan'):
            self.runtime.reserve_source_plan(sum(item['kind']!='ignore'for item in prepared))
        snapshot=self.store.source_decision_snapshot()
        virtual=[]
        if preview:
            for row in self.store.db.execute("SELECT p.payload FROM source_previews p JOIN sources s ON s.id=p.source_id AND s.content_hash=p.source_hash WHERE p.status='ready' AND p.source_id<>? ORDER BY p.created_at",(source['id'],)):
                virtual.extend(x['action'] for x in json.loads(row[0]).get('outcomes',[]) if isinstance(x.get('action'),dict))
        matches=[];recovery_cache={}
        for item_index,item in enumerate(prepared):
            if item['kind']=='ignore':matches.append((None,'distinct'));continue
            if _source_action_reason(item,source,context)and not(item.get('corroboration_only')or item.get('closed_offer_research')):
                matches.append((None,'distinct'if item.get('expired_without_authority')else'uncertain'));continue
            candidates=self.store.candidate_actions(item,additional=virtual)
            complete=getattr(self.store,'_source_candidates_complete',False)
            if item.get('closed_offer_research'):
                matched=self._closed_offer_target(source,item,candidates)if complete else None
                if (not matched and complete and candidates and all(action.get('original_evidence')for action in candidates)
                        and hasattr(self.runtime,'verify_source_action')):
                    reserve=sum(other['kind']!='ignore'for other in prepared[item_index+1:])
                    if preview and hasattr(self.runtime,'can_verify_source_action')and not self.runtime.can_verify_source_action(reserve):
                        self._verification_deferred(item,metadata)
                    else:
                        result=self.runtime.verify_source_action(source,item,candidates)
                        verified,decision=verified_semantic_match(source,item,candidates,result)
                        if decision=='match'and self._closed_offer_eligible(item,verified):
                            matched=dict(verified,_closed_offer_proof=self._closed_offer_proof(source,item,verified))
                        from .codex_runtime import SOURCE_VERIFY_VERSION
                        metadata.setdefault('identity_verifications',[]).append(dict(
                            getattr(self.runtime,'last_verification_metadata',None)or{},version=SOURCE_VERIFY_VERSION,
                            accepted=matched is not None,closed_offer_decision_only=True))
                if not matched:
                    from .core import start_key
                    if sum(start_key(item)==start_key(action['payload'])and not _hard_match_decision(item,action['payload'])for action in candidates)>1:
                        item['closed_offer_identity_uncertain']=True
                matches.append((matched,'retain_closed_offer'if matched else'uncertain'))
                continue
            retained=self._retained_rejection(source,item,candidates) if complete and not item.get('corroboration_only')else None
            independently_verified=False
            if retained:matched,decision=retained,'match'
            elif not complete:
                matched,decision=None,'uncertain'
            elif candidates and hasattr(self.runtime,'match_source_action'):
                result=self.runtime.match_source_action(source,item,candidates)
                matched,decision=verified_match(source,item,candidates,result)
                if item.get('corroboration_only')and decision=='match':matched,decision=None,'uncertain'
                # Missing evidence is uncertainty, never permission to remove a
                # plausible candidate and manufacture a unique semantic target.
                reserve=sum(other['kind']!='ignore'for other in prepared[item_index+1:])
                missing=[action for action in candidates if not action.get('original_evidence')]
                if (decision=='uncertain'and isinstance(result,dict)and result.get('decision')in {'match','uncertain'}
                        and missing and hasattr(self.runtime,'recover_source_action_evidence')
                        and all(action.get('recovery_origin')and _substantive_words(action['payload'].get('title',''))for action in missing)):
                    uncached=[action for action in missing if action['id']not in recovery_cache]
                    if uncached:
                        if preview and hasattr(self.runtime,'can_recover_source_action_evidence')and not self.runtime.can_recover_source_action_evidence(reserve):
                            self._verification_deferred(item,metadata)
                        else:
                            recovered=self.runtime.recover_source_action_evidence(uncached,context)
                            registered=recovered_action_evidence(uncached,recovered)
                            recovery_cache.update({action['id']:registered.get(action['id'],[])for action in uncached})
                            proof_metadata=dict(getattr(self.runtime,'last_recovery_metadata',None)or{},accepted_count=len(registered))
                            metadata.setdefault('origin_recoveries',[]).append(proof_metadata)
                    candidates=[dict(action,reconstructed_evidence=recovery_cache.get(action['id'],[]))for action in candidates]
                original=candidates if all(action.get('original_evidence')or action.get('reconstructed_evidence')for action in candidates)else[]
                if (decision=='uncertain'and isinstance(result,dict)and result.get('decision')in {'match','uncertain'}
                        and original and hasattr(self.runtime,'verify_source_action')):
                    if preview and hasattr(self.runtime,'can_verify_source_action')and not self.runtime.can_verify_source_action(reserve):
                        self._verification_deferred(item,metadata)
                    else:
                        verification=self.runtime.verify_source_action(source,item,original)
                        matched,decision=verified_semantic_match(source,item,original,verification)
                        from .codex_runtime import SOURCE_VERIFY_VERSION
                        proof_metadata=dict(getattr(self.runtime,'last_verification_metadata',None)or{},
                            version=SOURCE_VERIFY_VERSION,accepted=decision=='match')
                        metadata.setdefault('identity_verifications',[]).append(proof_metadata)
                        independently_verified=decision=='match'
            else:matched,decision=_synthetic_match(source,item,candidates)
            if item.get('corroboration_only'):
                if independently_verified and self._corroboration_allowed(source,item,matched):
                    decision='corroborate';metadata['corroboration_only']=metadata.get('corroboration_only',0)+1
                else:matched,decision=None,'uncertain'
            matches.append((matched,decision))
        self._block_unproven_closed_membership(source,prepared,matches)
        retained_count=sum(decision=='retain_closed_offer'for _,decision in matches)
        if retained_count:
            metadata['closed_offer_decisions_retained']=retained_count
            metadata['decision_reason']=(metadata.get('decision_reason','')+' Zachowano wcześniejszą decyzję dotyczącą kompletnej oferty; nowe źródło nie zatwierdza udziału.')[:2000]
        if metadata.get('corroboration_only'):
            metadata['decision_reason']=(metadata.get('decision_reason','')+' Starsze źródło dodaje wyłącznie potwierdzenie istniejącego wydarzenia; nie zweryfikowano odbiorcy i nie udzielono uprawnień do zmian.')[:2000]
        if metadata.get('verification_deferred'):
            metadata['decision_reason']=(metadata.get('decision_reason','')+' Część działań wymaga dodatkowej weryfikacji tożsamości; nie potwierdzono ich powiązania.')[:2000]
        if preview:
            outcomes=self._preview_outcomes(source,prepared,matches)
            self.store.save_source_preview(source['id'],prepared,metadata,expected_hash=source['content_hash'],outcomes=outcomes,expected_decisions=snapshot)
            return {'source_id':source['id'],'status':'preview','outcomes':outcomes,'count':len(prepared)}
        return self.apply(source,prepared,matches,metadata,snapshot,current)

    @staticmethod
    def _verification_deferred(item,metadata):
        item.update(verification_deferred=True,status='review',needs_review=True,
            review_reason='Tożsamość działania wymaga dodatkowej weryfikacji; nie potwierdzono powiązania z istniejącym działaniem.')
        metadata['identity_verification_complete']=False
        metadata['verification_deferred']=metadata.get('verification_deferred',0)+1

    def _corroboration_allowed(self,source,item,matched):
        """Older corroboration cannot transfer recipient or write authority."""
        if not matched:return False
        old=matched['payload'];decision=(matched.get('decision')or{}).get('proposal')or old
        if (not any(decision.get(key)for key in ('user_approved','user_edited'))or decision.get('user_rejected')
                or old.get('kind')=='cancel'or item.get('all_day')!=old.get('all_day')
                or any(link['source_id']==source['id']for link in authority_links(matched['source_links']))):return False
        origins=[link for link in authority_links(matched['source_links'])if link.get('relation')=='origin']
        if len(origins)!=1:return False
        origin=origins[0]
        row=self.store.db.execute('SELECT source_hash FROM proposals WHERE id=?',(matched['proposal_id'],)).fetchone()
        original=self.store.get_source(origin['source_id'])
        if (not row or not original or original['deleted']or original['status']=='retained'
                or row['source_hash']!=origin['source_hash']or original['content_hash']!=origin['source_hash']):return False
        try:
            if as_datetime(item['start']).astimezone(WARSAW).date()!=as_datetime(old['start']).astimezone(WARSAW).date():return False
            if as_datetime(source.get('updated_at')or source.get('published_at'))>=as_datetime(original.get('updated_at')or original.get('published_at')):return False
        except (ValueError,TypeError,OverflowError):return False
        events=self.store.db.execute("SELECT * FROM events WHERE proposal_id=? AND calendar_id=? AND state='active'",(matched['proposal_id'],self.store.calendar_id)).fetchall()
        if len(events)!=1:return False
        from .core import contains_marker,has_external_attendees
        from .codex_runtime import event_fingerprint
        event=events[0];snapshot=json.loads(event['snapshot'])
        return (contains_marker(snapshot.get('description'),event['marker'])
                and not has_external_attendees(snapshot,self.store.config.calendar_owner)
                and event_fingerprint(snapshot,self.store.config.calendar_owner)==event['fingerprint'])

    def _closed_offer_eligible(self,item,action):
        """An exact closed time choice can retain a decision, never gain authority."""
        from .core import start_key
        if not action:return False
        old=action['payload']
        groups=self.store.db.execute("SELECT g.* FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id WHERE m.proposal_id=? AND g.status IN ('selected','rejected')",(action['proposal_id'],)).fetchall()
        return (len(groups)==1 and self.store.db.execute('SELECT COUNT(*) FROM proposal_group_members WHERE group_id=?',(groups[0]['id'],)).fetchone()[0]>=2
                and item.get('kind')==old.get('kind')=='create'
                and item.get('temporal_kind','event')==old.get('temporal_kind','event')
                and start_key(item)==start_key(old)and not _hard_match_decision(item,old))

    @staticmethod
    def _closed_offer_proof(source,item,action):
        return digest([source['id'],source['content_hash'],item,action['id'],action.get('original_evidence',[])])

    def _closed_offer_target(self,source,item,candidates):
        """Research only a proven existing complete-offer decision, never approve.

        Legacy decisions need not have typed identities. An exact persisted
        action quote plus its exact canonical time choice can retain the old
        decision; a paraphrase needs the independent original-quote verifier.
        """
        identity=item.get('action_identity')or{}
        if any(not _literal(identity.get(key),source['text'])for key in ('object','occasion')):return None
        found=[]
        for action in candidates:
            old=action['payload'];prior=old.get('action_identity')or{}
            if not self._closed_offer_eligible(item,action):continue
            typed=prior and all(normalized_text(identity.get(key))==normalized_text(prior.get(key))for key in ('action_type','object','occasion'))
            quote=normalized_text(item.get('source_quote',''))
            exact=any(quote==normalized_text(proof['text'])
                and _substantive_words(identity['object'])&_substantive_words(proof['text'])&_substantive_words(old.get('title',''))
                for proof in action.get('original_evidence',[]))
            signature=(action.get('decision')or{}).get('signature')or{}
            signature_exact=(signature.get('quote_hash')==digest(re.sub(r'\s+',' ',item.get('source_quote','')).strip())
                and _substantive_words(identity['object'])&_substantive_words(item.get('source_quote',''))&_substantive_words(old.get('title','')))
            if typed or exact or signature_exact:found.append(action)
        if len(found)!=1:return None
        # A closed member is not unique merely because a competing same-slot
        # action has no closed-group decision. Keep the whole bounded inventory
        # visible to independent verification, including proofless inventory.
        from .core import start_key
        plausible=[action for action in candidates if start_key(item)==start_key(action['payload'])
                   and not _hard_match_decision(item,action['payload'])]
        return found[0]if len(plausible)==1 else None

    def _block_unproven_closed_membership(self,source,prepared,matches):
        """Missing group markers cannot silently turn a known offer into approval."""
        known={}
        for item,(matched,decision)in zip(prepared,matches):
            if decision!='retain_closed_offer':continue
            row=self.store.db.execute('SELECT group_id FROM proposal_group_members WHERE proposal_id=?',(matched['proposal_id'],)).fetchone()
            if row:known.setdefault(row[0],[]).append(item)
        for item,(matched,decision)in zip(prepared,matches):
            if not item.get('closed_offer_research')or matched or(item.get('alternative_group')and not item.get('closed_offer_identity_uncertain')):continue
            identity=item.get('action_identity')or{};groups=[]
            for group_id,items in known.items():
                if len(items)>=2 and all(normalized_text(identity.get(k))==normalized_text(items[0]['action_identity'].get(k))for k in ('action_type','object','occasion')):
                    groups.append(group_id)
            if len(groups)==1:
                item.update(blocked_by_offer_group=groups[0],status='review',needs_review=True,
                    review_reason='Known closed offer membership is incomplete; reconcile the complete offer before any participation change.')
                if item.get('closed_offer_identity_uncertain'):
                    item.pop('alternative_group',None);item.pop('alternative_group_evidence',None)

    def _retained_rejection(self,source,item,candidates):
        """Minimal proven obligation identity can retain rejection after text expires.

        This path can only preserve an explicit dismissal; it never grants an old
        approval or authorizes a mutation without current literal source evidence.
        """
        if not _identity(item,source):return None
        identity=item.get('action_identity') or {};matches=[]
        for action in candidates:
            prior=action['payload'];decision=(action.get('decision') or {}).get('proposal') or prior
            if not decision.get('user_rejected'):continue
            old=prior.get('action_identity') or decision.get('action_identity') or {}
            if (old and all(normalized_text(identity.get(k))==normalized_text(old.get(k)) for k in ('action_type','object','occasion'))
                    and item.get('temporal_kind')==prior.get('temporal_kind')
                    and (not item.get('alternative_group') or item.get('start')==prior.get('start'))
                    and _literal(identity.get('object'),source['text']) and _literal(identity.get('occasion'),source['text'])):
                matches.append(action);continue
            signature=(action.get('decision') or {}).get('signature') or {}
            if signature.get('quote_hash') and signature['quote_hash']==digest(re.sub(r'\s+',' ',item.get('source_quote','')).strip()):matches.append(action)
        return matches[0] if len(matches)==1 else None

    def _known_offer_groups(self,prepared,matches):
        found={}
        for item,(matched,decision) in zip(prepared,matches):
            if not item.get('alternative_group') or not matched:continue
            for row in self.store.db.execute("SELECT g.* FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id WHERE m.proposal_id=?",(matched['proposal_id'],)):
                found.setdefault(item['alternative_group'],{}).setdefault(row['id'],set()).add(matched['id'])
        result={}
        for key,groups in found.items():
            proved=[gid for gid,ids in groups.items() if len(ids)>=2]
            if len(proved)==1 and len(groups)==1:result[key]=self.store.db.execute('SELECT * FROM proposal_groups WHERE id=?',(proved[0],)).fetchone()
        return result

    def _closed_offer_decisions(self,prepared,matches):
        return {key:group for key,group in self._known_offer_groups(prepared,matches).items() if group['status']!='open'}

    def _preview_outcomes(self,source,prepared,matches):
        outcomes=[];known_offers=self._known_offer_groups(prepared,matches);held_offers={key:g for key,g in known_offers.items() if g['status']!='open'}
        for index,(raw,(matched,decision)) in enumerate(zip(prepared,matches)):
            item=dict(raw);outcome='added';aid=None
            corroboration=decision in {'corroborate','retain_closed_offer'}
            if corroboration:
                item=dict(matched['payload']);aid=matched['id']
                outcome='ignored'if decision=='retain_closed_offer'and self._closed_offer_rejected(matched)else'linked'
            known=known_offers.get(item.get('alternative_group'))
            held=held_offers.get(item.get('alternative_group'))
            if known and known['status']=='open' and not matched:
                item.update(status='review',needs_review=True,blocked_by_offer_group=known['id'],
                            review_reason='This source adds an option to an existing complete offer; reconcile membership before approving the new option.')
                item.pop('alternative_group',None);item.pop('alternative_group_evidence',None)
                outcome='review'
            elif held and not matched:
                item.update(status='ignored',needs_review=False,user_rejected=True,blocked_by_group_decision=held['id'])
                outcome='ignored'
            elif decision=='uncertain' and item['kind']!='ignore' and not item.get('blocked_by_offer_group')and not item.get('expired_without_authority'):
                item.update(status='review',needs_review=True,review_reason=item.get('review_reason')if item.get('verification_deferred')else'Canonical action matching is uncertain; no automatic duplicate or update.')
            if matched and not corroboration:
                old=matched['payload'];aid=matched['id'];decision_record=(matched.get('decision') or {}).get('proposal') or old
                changed=(item.get('kind')=='cancel' or old.get('kind')=='cancel' or any(item.get(k)!=old.get(k) for k in ('start','end','due_at','all_day')) or (item.get('status')=='review' and old.get('status')=='pending'))
                same=any(x['source_id']==source['id'] for x in authority_links(matched['source_links']))
                if same and source['content_hash']!=old.get('source_hash'):changed=changed or any(item.get(k)!=old.get(k) for k in ('description','optional','activity_scope','action_identity'))
                if decision_record.get('user_rejected'):item=dict(old);outcome='ignored'
                elif not changed:item=dict(old);outcome='linked'
                else:
                    try:newer=as_datetime(item.get('source_version_at'))>as_datetime(old.get('source_version_at'));known=True
                    except (ValueError,TypeError):newer=False;known=False
                    if not same and known and not newer:item=dict(old);outcome='linked'
                    elif matched.get('decision') or any(old.get(k) for k in ('user_approved','user_edited','user_rejected')) or not known or item.get('status')=='review':
                        item.update(status='review',needs_review=True,review_reason='A prior decision or uncertain newer facts require explicit review.');outcome='review'
                    else:outcome='enriched'
            if aid is None:aid=digest(['source-action',source['id'],source['content_hash'],index,item])[:32]
            if outcome=='added' and item.get('status') in ('review','ignored'):outcome='review' if item['status']=='review' else 'ignored'
            pid=matched['proposal_id'] if matched else aid
            action={'id':aid,'proposal_id':pid,'payload':item,'status':item.get('status'),'decision':matched.get('decision') if matched else None,
                    'event_ids':matched.get('event_ids',[]) if matched else [],
                    'source_links':(matched.get('source_links',[]) if matched else [])+[{'source_id':source['id'],'source_hash':source['content_hash'],'source_quote':raw.get('source_quote',''),'relation':CLOSED_OFFER_RELATION if decision=='retain_closed_offer'else CORROBORATION_RELATION if corroboration else EXPIRED_FACT_RELATION if item.get('expired_without_authority')and not matched else outcome}]}
            outcomes.append({'action_id':aid,'proposal_id':pid,'outcome':outcome,'reason':self._closed_offer_reason()if decision=='retain_closed_offer'else self._corroboration_reason()if corroboration else item.get('review_reason',''),'action':action})
        return outcomes

    def apply(self,source,prepared,matches,metadata,snapshot,current=None):
        current=current or datetime.now(WARSAW)
        outcomes=[]
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            live=self.store.get_source(source['id'])
            if not live or live['deleted'] or live['content_hash']!=source['content_hash'] or self.store.source_decision_snapshot()!=snapshot:
                raise ValueError('Source or human decision changed during analysis.')
            # Project non-message sources only during active apply. Preview cannot
            # touch the legacy message queue or its decisions.
            if source['kind']=='message':message=self.store.get_message(source['upstream_id'])
            else:
                message=source_message(source)
                hashed=digest(message)
                self.store.db.execute("INSERT INTO messages(id,payload,payload_hash,status,sent_at,fetched_at) VALUES (?,?,?,'analyzed',?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,payload_hash=excluded.payload_hash,status='analyzed',sent_at=excluded.sent_at,fetched_at=excluded.fetched_at",
                    (message['id'],json.dumps(message,ensure_ascii=False),hashed,message.get('sent_at'),utcnow()))
                message=self.store.get_message(message['id'])
            grouped=[]
            known_groups=self._known_offer_groups(prepared,matches)
            group_decisions={key:g for key,g in known_groups.items() if g['status']!='open'}
            for index,(item,(matched,decision)) in enumerate(zip(prepared,matches)):
                known=known_groups.get(item.get('alternative_group'))
                held=group_decisions.get(item.get('alternative_group'))
                if known and known['status']=='open' and not matched:
                    item=dict(item,status='review',needs_review=True,blocked_by_offer_group=known['id'],
                              review_reason='This source adds an option to an existing complete offer; reconcile membership before approving the new option.')
                    item.pop('alternative_group',None);item.pop('alternative_group_evidence',None)
                elif held and not matched:
                    item=dict(item,status='ignored',needs_review=False,user_rejected=True,user_approved=False,
                              blocked_by_group_decision=held['id'],review_reason='Prior complete offer decision retained across sources; no repeated participation question.')
                    item.pop('alternative_group',None);item.pop('alternative_group_evidence',None)
                if decision=='uncertain' and item['kind']!='ignore' and not held and not item.get('blocked_by_offer_group')and not item.get('expired_without_authority'):
                    item=dict(item,status='review',needs_review=True,review_reason='Canonical action matching is uncertain; no automatic duplicate or update.')
                if decision in {'corroborate','retain_closed_offer'}:
                    if decision=='corroborate'and not self._corroboration_allowed(source,item,matched):raise ValueError('Corroborating authority changed during analysis.')
                    if decision=='retain_closed_offer':
                        current_action=self.store.get_action(matched['id'])
                        semantic=(matched.get('_closed_offer_proof')==self._closed_offer_proof(source,item,current_action)
                                  and self._closed_offer_eligible(item,current_action))
                        if not semantic and not self._closed_offer_target(source,item,[current_action]):raise ValueError('Closed offer decision changed during analysis.')
                    same_authority=any(link['source_id']==source['id']for link in authority_links(matched['source_links']))
                    if not same_authority:
                        self.store.db.execute('INSERT INTO action_sources VALUES (?,?,?,?,?,?) ON CONFLICT(action_id,source_id) DO UPDATE SET source_hash=excluded.source_hash,source_quote=excluded.source_quote,relation=excluded.relation,updated_at=excluded.updated_at',
                            (matched['id'],source['id'],source['content_hash'],item.get('source_quote',''),CLOSED_OFFER_RELATION if decision=='retain_closed_offer'else CORROBORATION_RELATION,utcnow()))
                    action=self.store.get_action(matched['id'])
                    outcome='ignored'if decision=='retain_closed_offer'and self._closed_offer_rejected(action)else'linked'
                else:action,outcome=self._apply_item(source,message,item,matched,index,current)
                grouped.append(action['payload'])
                outcomes.append({'action_id':action['id'],'proposal_id':action['proposal_id'],'outcome':outcome,'reason':self._closed_offer_reason()if decision=='retain_closed_offer'else self._corroboration_reason()if decision=='corroborate'else action['payload'].get('review_reason','')})
            # One complete source offer remains one existing UI group. Cross-source
            # repeats link to its canonical members rather than creating a group.
            new_grouped=[x for x in grouped if x.get('message_id')==message['id'] and x.get('alternative_group')]
            by_group={}
            for item in new_grouped:by_group.setdefault(item['alternative_group'],[]).append(item)
            for key,items in by_group.items():
                if len(items)>=2:self.store._save_proposal_groups(message,items)
                else:
                    singleton=items[0]
                    singleton.update(status='review',needs_review=True,review_reason='Cross-source offer membership requires explicit reconciliation; no partial alternative group was created.')
                    self.store.db.execute("UPDATE proposals SET payload=?,status='review',pending_since=NULL WHERE id=?",(json.dumps(singleton,ensure_ascii=False),singleton['id']))
            stamp=utcnow()
            outcome=('review' if any(x['outcome']=='review' for x in outcomes) else 'enriched' if any(x['outcome']=='enriched' for x in outcomes)
                     else 'added' if any(x['outcome']=='added' for x in outcomes) else 'linked' if outcomes else 'ignored')
            self.store.db.execute("UPDATE sources SET status='analyzed',analyzed_at=?,last_error=NULL,outcome=?,reason=? WHERE id=?",(stamp,outcome,metadata.get('decision_reason','')[:2000],source['id']))
            if source['kind']=='message':
                self.store.db.execute("UPDATE messages SET analyzed_hash=payload_hash,status='analyzed',last_error=NULL,pending_since=NULL,analysis_metadata=?,analyzed_at=? WHERE id=?",(json.dumps(metadata,ensure_ascii=False),stamp,source['upstream_id']))
            self.store.db.execute("UPDATE source_previews SET status='activated' WHERE source_id=? AND source_hash=?",(source['id'],source['content_hash']))
            self.store.db.execute('INSERT INTO source_analysis_audit VALUES (?,?,?,?,?,?,?)',(uuid.uuid4().hex,source['id'],source['content_hash'],stamp,'active',json.dumps(metadata,ensure_ascii=False),json.dumps(outcomes,ensure_ascii=False)))
            return {'source_id':source['id'],'status':'applied','outcomes':outcomes,'count':len(prepared)}

    @staticmethod
    def _corroboration_reason():
        return 'Starsze źródło potwierdza istniejące wydarzenie. Odbiorca pozostaje niezweryfikowany; powiązanie nie daje uprawnień do zmian.'

    def _closed_offer_rejected(self,action):
        group=self.store.db.execute('SELECT g.status,g.selected_proposal_id FROM proposal_groups g JOIN proposal_group_members m ON m.group_id=g.id WHERE m.proposal_id=?',(action['proposal_id'],)).fetchone()
        return bool(group and(group['status']=='rejected'or(group['status']=='selected'and group['selected_proposal_id']!=action['proposal_id'])))

    @staticmethod
    def _closed_offer_reason():
        return 'Zachowano wcześniejszą decyzję dotyczącą kompletnej oferty. Nowe źródło nie zatwierdza udziału ani zmian.'

    def _apply_item(self,source,message,item,matched,index,current):
        stamp=utcnow();outcome='added'
        observed_quote=item.get('source_quote','')
        item=dict(item)
        intended_kind=item.get('kind')
        if matched:
            # Re-read the decision-bearing proposal in the same transaction.
            matched=self.store.get_action(matched['id']);old=matched['payload']
            aid=matched['id'];pid=matched['proposal_id']
            changed=(item.get('kind')=='cancel' or old.get('kind')=='cancel'
                     or any(item.get(k)!=old.get(k) for k in ('start','end','due_at','all_day'))
                     or (item.get('status')=='review' and old.get('status')=='pending'))
            same_source=any(link['source_id']==source['id'] for link in authority_links(matched['source_links']))
            if same_source and source['content_hash']!=old.get('source_hash'):
                changed=changed or any(item.get(k)!=old.get(k) for k in ('description','optional','activity_scope','action_identity'))
            human=any(old.get(k) for k in ('user_approved','user_rejected','user_edited','deletion_approved')) or bool(matched['decision'])
            if old.get('user_rejected') or (matched['decision'] and matched['decision'].get('proposal',{}).get('user_rejected')):
                item=old;outcome='ignored'
            elif not changed:
                item=dict(old);outcome='linked'
                if same_source and source['content_hash']!=old.get('source_hash'):
                    item.update(source_hash=source['content_hash'],source_quote=observed_quote,provenance={'source_id':source['id'],'source_hash':source['content_hash'],'quote':observed_quote})
                    self.store.db.execute('UPDATE proposals SET payload=? WHERE id=?',(json.dumps(item,ensure_ascii=False),pid))
                    self.store.db.execute('UPDATE canonical_actions SET payload=?,updated_at=? WHERE id=?',(json.dumps(item,ensure_ascii=False),stamp,aid))
            else:
                # Same-source edits are revisions observed now; older independent
                # sources can add a link, never regress newer facts.
                same_source=any(link['source_id']==source['id'] for link in authority_links(matched['source_links']))
                known_versions=True
                try:
                    new_version=as_datetime(item.get('source_version_at'));old_version=as_datetime(old.get('source_version_at'))
                    newer=new_version>old_version
                    if new_version==old_version:known_versions=False
                except (ValueError,TypeError):newer=False;known_versions=False
                if not same_source and not known_versions:
                    item.update(kind=('cancel' if item['kind']=='cancel' else 'update') if matched['event_ids'] else item['kind'],
                                event_id=matched['event_ids'][0] if len(matched['event_ids'])==1 else None,
                                status='review',needs_review=True,review_reason='Conflicting facts lack a known source publication or revision time; confirm the current facts.')
                    pid=digest(['source-conflict',aid,source['id'],source['content_hash'],index])[:32]
                    outcome='review'
                    if matched['decision']:
                        self.store.db.execute('INSERT OR REPLACE INTO canonical_decisions VALUES (?,?,?)',(aid,json.dumps(matched['decision'],ensure_ascii=False),stamp))
                elif not same_source and not newer:
                    item=old;outcome='linked'
                elif human or item['status']=='review':
                    if matched['decision']:
                        self.store.db.execute('INSERT OR REPLACE INTO canonical_decisions VALUES (?,?,?)',(aid,json.dumps(matched['decision'],ensure_ascii=False),stamp))
                    # Retain the edited action itself; surface a new change for
                    # explicit review without transferring its approval.
                    item.update(kind=('cancel' if item['kind']=='cancel' else 'update') if matched['event_ids'] else item['kind'],event_id=matched['event_ids'][0] if len(matched['event_ids'])==1 else None,
                                status='review',needs_review=True,review_reason='A prior human decision governs this action; confirm the newer source change.')
                    pid=digest(['source-change',aid,source['id'],source['content_hash'],index])[:32]
                    outcome='review'
                else:
                    active_op=self.store.db.execute("SELECT * FROM operations WHERE proposal_id=? AND (attempts>0 OR status IN ('unknown','inflight','applied'))",(pid,)).fetchone()
                    if active_op and not matched['event_ids']:
                        item.update(kind='update',event_id=None,status='review',needs_review=True,
                                    deferred_unknown_operation=active_op['id'],review_reason='New source facts await read-only reconciliation of the prior unknown write; then confirm this correction.')
                        pid=digest(['deferred-source-change',aid,source['id'],source['content_hash'],index])[:32]
                        outcome='review'
                    else:
                        if matched['event_ids']:
                            if len(matched['event_ids'])!=1:
                                item.update(status='review',needs_review=True,review_reason='Multiple owned events require explicit reconciliation.')
                            else:item.update(kind='cancel' if item['kind']=='cancel' else 'update',event_id=matched['event_ids'][0])
                            pid=digest(['source-update',aid,source['id'],source['content_hash'],index])[:32]
                        outcome='enriched' if item['status']=='pending' else 'review'
        else:
            pid=digest(['source-action',source['id'],source['content_hash'],index,item])[:32];aid=pid
            if item['status']=='review':outcome='review'
            elif item['status']=='ignored':outcome='ignored'
        if matched and outcome=='review' and not matched['event_ids']:
            uncertain=self.store.db.execute("SELECT id FROM operations WHERE proposal_id=? AND status IN ('unknown','inflight')",(matched['proposal_id'],)).fetchone()
            if uncertain:
                item.update(kind='cancel' if intended_kind=='cancel' else 'update',event_id=None,status='review',needs_review=True,
                            deferred_unknown_operation=uncertain['id'],review_reason='New source change awaits the exact target from read-only reconciliation; explicit confirmation is still required.')
                pid=digest(['deferred-source-change',aid,source['id'],source['content_hash'],index])[:32]
        if matched and outcome not in {'linked','ignored'} and pid!=matched['proposal_id']:
            previous=matched['proposal_id']
            self.store.db.execute("UPDATE proposals SET status='superseded',pending_since=NULL,updated_at=? WHERE id=? AND status IN ('pending','review')",(stamp,previous))
            self.store.db.execute("UPDATE operations SET status='superseded',last_error='A newer source revision requires review.',updated_at=? WHERE proposal_id=? AND attempts=0 AND status IN ('pending','review')",(stamp,previous))
        if outcome not in {'linked','ignored'} or not matched:
            item.update(id=pid,message_id=message['id'],canonical_action_id=aid)
            # Preserve cross-source decision protection at the event boundary.
            from .data_admin import human_event_review_reason
            reason=human_event_review_reason(self.store,item)
            if reason:item.update(status='review',needs_review=True,review_reason=reason);outcome='review'
            self.store.db.execute('INSERT INTO proposals(id,message_id,source_hash,payload,status,created_at,updated_at,pending_since) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at,pending_since=excluded.pending_since',
                (pid,message['id'],message['payload_hash'],json.dumps(item,ensure_ascii=False),item['status'],stamp,stamp,stamp if item['status']=='pending' else None))
            self.store.db.execute('INSERT INTO canonical_actions VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET proposal_id=excluded.proposal_id,payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at',
                (aid,pid,json.dumps(item,ensure_ascii=False),item['status'],stamp))
            if item.get('user_rejected'):
                from .data_admin import remember_decision
                row=self.store.db.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
                remember_decision(self.store,row,item)
                self.store.remember_canonical_decision(pid)
        self.store.db.execute('INSERT INTO action_sources VALUES (?,?,?,?,?,?) ON CONFLICT(action_id,source_id) DO UPDATE SET source_hash=excluded.source_hash,source_quote=excluded.source_quote,relation=excluded.relation,updated_at=excluded.updated_at',
            (aid,source['id'],source['content_hash'],observed_quote,EXPIRED_FACT_RELATION if item.get('expired_without_authority')and not matched else outcome,stamp))
        return self.store.get_action(aid),outcome
