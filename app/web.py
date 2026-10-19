"""Small private inbox. Only the local Tailscale reverse proxy is trusted."""
from __future__ import annotations
import hmac
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dataclasses import replace
from urllib.parse import urlsplit
from .config import AppConfig, ConfigurationError, load_config

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent
WARSAW = ZoneInfo('Europe/Warsaw')
SOURCE_KINDS = {'message': 'Wiadomości', 'announcement': 'Ogłoszenia',
                'school_event': 'Terminarz', 'substitution': 'Zastępstwa'}
SOURCE_OUTCOMES = {'added': 'Dodano', 'linked': 'Powiązano', 'enriched': 'Uzupełniono',
                   'review': 'Do sprawdzenia', 'ignored': 'Pominięto',
                   'analysis_incomplete': 'Analiza niepełna', 'pending': 'Czeka na analizę',
                   'analyzed': 'Przeanalizowano', 'failed': 'Analiza niepełna',
                   'deleted': 'Usunięto u źródła', 'retained': 'Zachowano decyzję',
                   'ready': 'Podgląd gotowy', 'stale': 'Nieaktualny podgląd', 'activated': 'Aktywowano',
                   'applied': 'W kalendarzu'}


SOURCE_LABELS = {
    'confidence': {'high': 'wysoka', 'medium': 'średnia', 'low': 'niska', 'unknown': 'nieznana'},
    'activity_scope': {'school': 'wydarzenie szkolne', 'extracurricular': 'zajęcia dodatkowe',
                       'unknown': 'nieznany udział'},
    'relation': {'origin': 'Źródło pierwotne', 'added': 'Dodano', 'linked': 'Powiązano',
                 'enriched': 'Uzupełniono', 'retained': 'Zachowano powiązanie',
                 'corroborates_unverified_recipient': 'Potwierdzenie bez uprawnień do zmian; odbiorca niezweryfikowany',
                 'expired_unverified_fact': 'Historyczna informacja bez uprawnień; odbiorca niezweryfikowany',
                 'agenda_plan': 'Wspólny plan agendy',
                 'retains_closed_offer_decision': 'Zachowano wcześniejszą decyzję; nowe źródło nie zatwierdza udziału'},
    'decision': {'approve': 'zatwierdzono', 'approved': 'zatwierdzono',
                 'reject': 'odrzucono', 'rejected': 'odrzucono', 'ignore': 'odrzucono',
                 'ignored': 'odrzucono', 'select': 'wybrano termin', 'selected': 'wybrano termin',
                 'confirm_cancel': 'potwierdzono odwołanie', 'cancelled': 'potwierdzono odwołanie'},
}
SOURCE_FALLBACKS = {'confidence': 'nieznana', 'activity_scope': 'nieznany udział',
                    'relation': 'Powiązanie źródłowe', 'decision': 'zapisana'}


def source_label(value, family):
    fallback = SOURCE_FALLBACKS.get(family, 'Brak danych')
    return SOURCE_LABELS.get(family, {}).get(value, fallback) if isinstance(value, str) else fallback


def source_outcome(source):
    if source.get('current_preview'):
        return 'Podgląd gotowy'
    metadata = source.get('metadata') or {}
    return SOURCE_OUTCOMES.get(source.get('outcome') or metadata.get('outcome') or source.get('status'), 'Brak danych')



def retained_decision(record):
    if not isinstance(record, dict):
        return None
    proposal = record.get('proposal') or record
    if isinstance(proposal, dict):
        for field, label in (('user_rejected', 'odrzucono'), ('user_edited', 'zachowano edycję'),
                             ('user_approved', 'zatwierdzono')):
            if proposal.get(field) is True:
                return label
    return source_label(record.get('decision') or record.get('action'), 'decision')


def preview_changes(previous, planned):
    if not isinstance(previous, dict) or not isinstance(planned, dict):
        return []
    fields = {'title': 'Tytuł', 'start': 'Początek', 'end': 'Koniec', 'due_at': 'Termin',
              'all_day': 'Cały dzień', 'optional': 'Opcjonalne przypomnienie',
              'description': 'Opis', 'activity_scope': 'Udział'}
    def display(key, value):
        if value in (None, ''):
            return 'brak'
        if key in {'start', 'end', 'due_at'}:
            return human_date(value)
        if key in {'all_day', 'optional'}:
            return 'tak' if value is True else 'nie'
        if key == 'activity_scope':
            return source_label(value, 'activity_scope')
        return str(value)
    return [{'label': label, 'before': display(key, previous.get(key)),
             'after': display(key, planned.get(key))}
            for key, label in fields.items() if previous.get(key) != planned.get(key)]


def human_date(value):
    if not value:
        return 'Jeszcze nie wykonano'
    try:
        if len(str(value)) == 10:
            return datetime.fromisoformat(str(value)).strftime('%d.%m.%Y')
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(WARSAW)
        return parsed.strftime('%d.%m.%Y, %H:%M')
    except (ValueError, TypeError):
        return str(value)


def event_date(snapshot):
    boundary = snapshot.get('start') or snapshot.get('start_time') or snapshot.get('start_date')
    if isinstance(boundary, dict):
        boundary = boundary.get('dateTime') or boundary.get('date_time') or boundary.get('date')
    return human_date(boundary) if boundary else 'Termin nieznany'


def calendar_probe_status(status):
    """Render only recognized aggregate evidence, including legacy unknowns."""
    labels = {'running': 'W toku', 'ok': 'Dostęp potwierdzony',
              'failed': 'Błąd sprawdzenia', 'interrupted': 'Przerwano'}
    result = status.get('calendar_probe_result')
    attempts = status.get('calendar_probe_attempts')
    recovered = status.get('calendar_probe_recovered')
    return {
        'result': labels.get(result, 'Brak danych') if isinstance(result, str) else 'Brak danych',
        'attempts': str(attempts) if type(attempts) is int and attempts in (1, 2) else 'Brak danych',
        'recovered': ('Tak' if recovered else 'Nie') if type(recovered) is bool else 'Brak danych',
    }


def request_sync():
    subprocess.run(['systemctl', '--user', 'start', '--no-block', 'librus-sync.service'],
                   check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_app(store=None, sync_request=None, *, config=None, owner=None, hosts=None,
               allow_local=None, demo_mode=False, now=None):
    if demo_mode:
        # A demo may never discover a database or call the real synchronization service.
        if store is None or sync_request is None or config is None:
            raise ConfigurationError('demo_requires_injected_store_config_and_sync')
        if not isinstance(config, AppConfig):
            raise ConfigurationError('validated_configuration_required')
        owner = ''
        allow_local = True
        hosts = set(hosts or ('127.0.0.1:8795', 'localhost:8795', 'testserver'))
        if any(urlsplit('http://' + host).hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'} for host in hosts):
            raise ConfigurationError('demo_requires_loopback_hosts')
    else:
        config = config if config is not None else (store.config if store is not None and hasattr(store, 'config') else load_config())
        config = replace(config, tailscale_owner=config.tailscale_owner if owner is None else owner,
                         allowed_hosts=config.allowed_hosts if hosts is None else tuple(hosts),
                         allow_local=config.allow_local if allow_local is None else allow_local)
        config.require_web()
        owner, hosts, allow_local = config.tailscale_owner, set(config.allowed_hosts), config.allow_local
        if store is None:
            from app.core import AppStore
            store = AppStore(config=config)
        sync_request = sync_request or request_sync
    if allow_local:
        hosts.update({'127.0.0.1:8795', 'localhost:8795', 'testserver'})
    cookie_name = 'librus_csrf' if allow_local else '__Host-librus_csrf'
    clock = now or (lambda: datetime.now(WARSAW))
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(ROOT / 'templates'))
    templates.env.filters['human_date'] = human_date
    templates.env.filters['event_date'] = event_date
    templates.env.filters['calendar_probe_status'] = calendar_probe_status
    templates.env.filters['source_outcome'] = source_outcome
    templates.env.filters['source_label'] = source_label
    templates.env.filters['retained_decision'] = retained_decision

    @application.middleware('http')
    async def private_access(request: Request, call_next):
        from starlette.responses import PlainTextResponse
        peer = request.client.host if request.client else ''
        local = peer in {'127.0.0.1', '::1'}
        host = request.headers.get('host', '')
        identity = request.headers.get('tailscale-user-login', '')
        if not local or host not in hosts or (identity != owner and not (allow_local and not identity)):
            return PlainTextResponse('Dostęp wyłącznie dla właściciela przez Tailscale.', status_code=403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            expected_scheme = 'http' if demo_mode or (allow_local and urlsplit('http://' + host).hostname in {'127.0.0.1', 'localhost', '::1', 'testserver'}) else 'https'
            if request.headers.get('origin') != f'{expected_scheme}://{host}':
                return PlainTextResponse('Nieprawidłowe źródło żądania.', status_code=403)
            await request.body()
            form = await request.form()
            cookie = request.cookies.get(cookie_name, '')
            if not cookie or not hmac.compare_digest(cookie, str(form.get('csrf', ''))):
                return PlainTextResponse('Odśwież stronę i spróbuj ponownie.', status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'none'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    application.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')

    def page(request, section='messages', selected=None, notice=None, extra=None):
        csrf = request.cookies.get(cookie_name) or secrets.token_urlsafe(32)
        next_sync = (clock().astimezone(WARSAW).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat()
        events = store.list_events()
        groups = store.list_proposal_groups(current=clock())
        grouped_ids = {member['id'] for group in groups for member in group['members']}
        proposals = [item for item in store.list_proposals() if item['id'] not in grouped_ids]
        selected_reviews = [{**member, 'selected_for_offer': True} for group in groups
                            for member in group['members'] if group['status'] == 'selected'
                            and member['id'] == group['selected_proposal_id'] and member['status'] == 'review']
        proposals.extend(selected_reviews)
        for item in proposals:
            group_id = item.get('blocked_by_offer_group')
            original = store.get_proposal_group(group_id, current=clock()) if group_id else None
            membership = store.get_proposal_group_for_proposal(item['id'], current=clock()) if item.get('alternative_group') else None
            item['blocked_offer'] = bool(group_id or (item.get('alternative_group') and membership is None))
            item['blocked_offer_message_id'] = original['message_id'] if original else item.get('message_id')
            if item['status'] == 'review' and item.get('canonical_action_id'):
                event = next((event for event in events if event.get('id') == item.get('event_id')), None)
                snapshot = (event or {}).get('snapshot') or {}
                def boundary(value):
                    return (value.get('dateTime') or value.get('date_time') or value.get('date')) if isinstance(value, dict) else value
                previous = {'title': snapshot.get('summary') or snapshot.get('title'),
                            'start': boundary(snapshot.get('start') or snapshot.get('start_time') or snapshot.get('start_date')),
                            'end': boundary(snapshot.get('end') or snapshot.get('end_time') or snapshot.get('end_date')),
                            'all_day': len(str(boundary(snapshot.get('start') or snapshot.get('start_time') or snapshot.get('start_date')) or '')) == 10,
                            'description': snapshot.get('description')} if snapshot else {}
                item['agenda_context'] = present_agenda_item(
                    {'action_id': item['canonical_action_id'], 'action': {key: value for key, value in item.items() if key != 'agenda_context'},
                     'sources': item.get('agenda_sources') or [], 'operation': item['kind'],
                     'review_change': item['kind']}, previous=previous)
                item['agenda_context']['has_baseline'] = bool(previous)

        selected_review_ids = {member['id'] for member in selected_reviews}
        status = store.status()
        grouped_review_count = sum(member['status'] == 'review' and member['id'] not in selected_review_ids
                                   for group in groups for member in group['members'])
        open_groups = [group for group in groups if group['status'] == 'open']
        review_count = max(0, status.get('proposals_review', 0) - grouped_review_count) + len(open_groups)
        selected_groups = [group for group in groups if selected and group['message_id'] == selected['id']]
        context = {'next_sync': next_sync, 'request': request, 'csrf': csrf, 'section': section,
                   'status': status, 'review_count': review_count, 'messages': store.list_messages(),
                   'events': events, 'proposals': proposals, 'proposal_groups': open_groups,
                   'selected_groups': selected_groups,
                   'has_review': bool(open_groups) or any(p['status'] == 'review' for p in proposals),
                   'selected': selected, 'notice': notice or request.query_params.get('notice'),
                   'demo_mode': demo_mode, 'demo_banner': 'Demo — dane fikcyjne / fictional data',
                   'calendar_label': 'calendar@example.test' if demo_mode else (config.calendar_id or 'Nie skonfigurowano'),
                   'selected_proposals': [item for item in proposals if selected and item.get('message_id') == selected.get('id') and not item.get('selected_for_offer')]}
        context.update({'source_kinds': SOURCE_KINDS, 'source_outcomes': SOURCE_OUTCOMES,
                        'source_health': status.get('sources') or {}, 'selected_source': None,
                        'selected_action': None, 'source_items': [], 'agenda_preview': None})
        context.update(extra or {})
        response = templates.TemplateResponse(request=request, name='inbox.html', context=context)
        response.set_cookie(cookie_name, csrf, httponly=True, secure=not allow_local,
                            samesite='strict', max_age=86400)
        return response

    @application.get('/', response_class=HTMLResponse)
    def inbox(request: Request):
        return page(request)

    @application.get('/messages/{message_id}', response_class=HTMLResponse)
    def message(request: Request, message_id: str):
        selected = store.get_message(message_id)
        if not selected:
            raise HTTPException(404, 'Nie znaleziono wiadomości.')
        return page(request, selected=selected)

    @application.get('/events', response_class=HTMLResponse)
    def events(request: Request):
        return page(request, section='events')

    @application.get('/review', response_class=HTMLResponse)
    def review(request: Request):
        return page(request, section='review')

    def source_kind(kind):
        if kind == 'school_calendar':
            kind = 'school_event'
        if kind is not None and kind not in SOURCE_KINDS:
            raise HTTPException(400, 'Nieprawidłowy rodzaj źródła.')
        return kind

    def present_agenda_item(item, previous=None):
        action = item.get('action') or {}
        existing = store.get_action(item.get('action_id')) if item.get('action_id') else None
        links = list(item.get('sources') or []) + list((existing or {}).get('source_links') or [])
        source_ids = dict.fromkeys(link['source_id'] for link in links)
        baseline = (existing or {}).get('payload') if previous is None else previous
        change = item.get('review_change') or item.get('operation')
        return {**item, 'action': action, 'existing': existing,
                'sources': [store.get_source(key) for key in source_ids],
                'changes': preview_changes(baseline, {} if change == 'cancel' else action)
                if baseline and action and change in ('update', 'cancel') else []}

    def present_agenda(plan):
        if not plan:
            return None
        return {**plan, 'planned_items': [present_agenda_item(item)
                for item in (plan.get('payload') or {}).get('items') or []]}

    def sources_page(request, *, selected_source=None, selected_action=None):
        kind = source_kind(request.query_params.get('kind') or None)
        try:
            offset = int(request.query_params.get('offset', '0'))
        except ValueError:
            raise HTTPException(400, 'Nieprawidłowa strona.') from None
        if offset < 0:
            raise HTTPException(400, 'Nieprawidłowa strona.')
        items = store.list_sources(limit=51, offset=offset, kind=kind)
        visible = items[:50]
        return page(request, section='sources', extra={
            'source_items': visible, 'source_kind': kind, 'source_offset': offset,
            'source_more': len(items) > 50, 'selected_source': selected_source,
            'selected_action': selected_action,
            'source_actions': [store.get_action(key) for key in (selected_source or {}).get('action_ids', [])]})

    @application.get('/sources', response_class=HTMLResponse)
    def sources(request: Request):
        return sources_page(request)

    @application.get('/sources/{source_id}', response_class=HTMLResponse)
    def source_detail(request: Request, source_id: str):
        item = store.get_source(source_id)
        if item is None:
            raise HTTPException(404, 'Nie znaleziono źródła.')
        return sources_page(request, selected_source=item)

    @application.get('/actions/{action_id}', response_class=HTMLResponse)
    def action_detail(request: Request, action_id: str):
        action = store.get_action(action_id)
        if action is None:
            raise HTTPException(404, 'Nie znaleziono działania.')
        action = {**action, 'sources': [store.get_source(link['source_id']) for link in action.get('source_links', [])]}
        return sources_page(request, selected_action=action)

    @application.get('/preview', response_class=HTMLResponse)
    def preview(request: Request):
        return page(request, section='preview', extra={'agenda_preview': present_agenda(store.latest_agenda_plan(preview=True)) if hasattr(store, 'latest_agenda_plan') else None})

    @application.get('/api/sources')
    def sources_api(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                    kind: str | None = None, status: str | None = None):
        items = store.list_sources(limit=limit + 1, offset=offset, kind=source_kind(kind), status=status)
        return {'items': items[:limit], 'limit': limit, 'offset': offset,
                'next_offset': offset + limit if len(items) > limit else None}

    @application.get('/api/actions/{action_id}')
    def action_api(action_id: str):
        action = store.get_action(action_id)
        if action is None:
            raise HTTPException(404, 'Nie znaleziono działania.')
        return {**action, 'sources': [store.get_source(link['source_id']) for link in action.get('source_links', [])]}

    @application.get('/api/agenda')
    def agenda_api():
        return {**store.agenda_status(), 'preview': store.latest_agenda_plan(preview=True)}

    @application.get('/api/source-previews')
    def source_previews_api(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        items = store.list_source_previews(limit=limit + 1, offset=offset)
        return {'engine': 'legacy-source-planner', 'historical': True,
                'items': items[:limit], 'limit': limit, 'offset': offset,
                'next_offset': offset + limit if len(items) > limit else None}

    @application.get('/api/status')
    def status():
        return store.status()

    @application.get('/api/messages')
    def messages_api():
        return store.list_messages()

    @application.get('/api/proposals')
    def proposals_api():
        return store.list_proposals()

    @application.get('/api/proposal-groups')
    def proposal_groups_api():
        return store.list_proposal_groups(current=clock())

    @application.get('/api/events')
    def events_api():
        return store.list_events()

    @application.post('/sync')
    def sync():
        try:
            store.request_sync()
            sync_request()
        except (subprocess.SubprocessError, OSError):
            raise HTTPException(503, 'Nie udało się rozpocząć sprawdzania. Sprawdź stan usługi.') from None
        return RedirectResponse('/?notice=sync', status_code=303)

    @application.post('/writes')
    async def writes(request: Request):
        form = await request.form()
        if form.get('paused') not in {'true', 'false'}:
            raise HTTPException(400, 'Nieprawidłowa operacja.')
        store.set_writes_paused(form['paused'] == 'true')
        return RedirectResponse('/?notice=writes', status_code=303)

    @application.post('/api/proposal-groups/{group_id}/decision')
    async def resolve_group(request: Request, group_id: str):
        form = await request.form()
        decision = str(form.get('decision', ''))
        proposal_id = str(form.get('proposal_id', '')).strip() or None
        if decision not in {'select', 'reject'} or (decision == 'select' and not proposal_id):
            raise HTTPException(400, 'Wybierz jeden termin albo odrzuć całą ofertę.')
        try:
            revision = int(str(form.get('expected_revision', '')))
            store.resolve_proposal_group(group_id, decision, proposal_id=proposal_id, expected_revision=revision, current=clock())
        except (ValueError, KeyError):
            raise HTTPException(400, 'Nie można zapisać tej decyzji. Odśwież ofertę i sprawdź dostępne terminy.') from None
        target = '/review'
        return_to = str(form.get('return_to', ''))
        group = store.get_proposal_group(group_id, current=clock())
        if group and return_to == 'message':
            from urllib.parse import quote
            target = '/messages/' + quote(str(group['message_id']), safe='')
        return RedirectResponse(target + '?notice=resolved', status_code=303)

    @application.post('/proposals/{proposal_id}')
    async def resolve(request: Request, proposal_id: str):
        form = await request.form()
        action = str(form.get('action', ''))
        if action not in {'approve', 'reject', 'confirm_cancel'}:
            raise HTTPException(400, 'Nieprawidłowa operacja.')
        changes = {key: str(form[key]).strip() for key in ('title', 'start', 'end')
                   if key in form and str(form[key]).strip()}
        if 'edit_dates' in form:
            changes['all_day'] = form.get('all_day') == 'true'
        try:
            proposal = next((item for item in store.list_proposals() if str(item['id']) == proposal_id), None)
            if proposal is None:
                group = store.get_proposal_group_for_proposal(proposal_id, current=clock())
                proposal = next((member for member in group['members'] if str(member['id']) == proposal_id), None) if group else None
            if not proposal or (proposal.get('kind') == 'cancel' and action == 'approve'):
                raise ValueError('Cancellation requires explicit confirmation')
            store.resolve_proposal(proposal_id, 'ignore' if action == 'reject' else action, changes)
        except (ValueError, KeyError) as error:
            raise HTTPException(400, 'Nie można zapisać tej decyzji. Sprawdź daty i stan propozycji.') from None
        return RedirectResponse('/review?notice=resolved', status_code=303)

    application.state.demo_mode = demo_mode
    application.state.store = store
    return application


def app_factory():
    return create_app()
