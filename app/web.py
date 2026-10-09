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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent
WARSAW = ZoneInfo('Europe/Warsaw')


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

    def page(request, section='messages', selected=None, notice=None):
        csrf = request.cookies.get(cookie_name) or secrets.token_urlsafe(32)
        next_sync = (clock().astimezone(WARSAW).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat()
        context = {'next_sync': next_sync, 'request': request, 'csrf': csrf, 'section': section,
                   'status': store.status(), 'messages': store.list_messages(),
                   'events': store.list_events(), 'proposals': store.list_proposals(),
                   'selected': selected, 'notice': notice or request.query_params.get('notice'),
                   'demo_mode': demo_mode, 'demo_banner': 'Demo — dane fikcyjne / fictional data',
                   'calendar_label': 'calendar@example.test' if demo_mode else (config.calendar_id or 'Nie skonfigurowano'),
                   'selected_proposals': [item for item in store.list_proposals() if selected and item.get('message_id') == selected.get('id')]}
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

    @application.get('/api/status')
    def status():
        return store.status()

    @application.get('/api/messages')
    def messages_api():
        return store.list_messages()

    @application.get('/api/proposals')
    def proposals_api():
        return store.list_proposals()

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
