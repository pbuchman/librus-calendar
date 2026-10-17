"""Bounded, inert Synergia announcement/calendar reads for the active learner.

Only the four known source routes and observed calendar detail routes are read.
Links, images, attachments and scripts are never followed. No raw HTML, form
secrets or session URLs leave this module. Unknown markup is a failed collection,
not evidence that a source disappeared.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .librus_client import MAX_BODY, PocError, TextOnly, clean_text, html_to_text

ORIGIN = 'https://synergia.librus.pl'
WARSAW = ZoneInfo('Europe/Warsaw')
KINDS = ('announcement', 'school_event', 'substitution')
MAX_ANNOUNCEMENTS = 500
MAX_CALENDAR_ITEMS = 1000
MAX_MONTHS = 5
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class SourceCollectionError(PocError):
    """An allowlisted category, containing no provider content or request data."""
    code = 'source_collection_failed'

    def __init__(self, code='source_collection_failed'):
        self.code = code
        super().__init__(code)


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.walk(tag)

    def ancestor(self, tag):
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return node
            node = node.parent

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()

    def inert_html(self):
        # Reconstruct only for the existing TextOnly decoder. Attributes are
        # deliberately omitted: URLs and event handlers are not text content.
        if self.tag in TextOnly.DROP:
            return ''
        value = ''.join(child.inert_html() if isinstance(child, Node) else escape(child)
                        for child in self.children)
        return f'<{self.tag}>{value}</{self.tag}>' if self.tag else value

    def text(self):
        return html_to_text(self.inert_html())


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root, self.closed_html = Node(), False
        self.current = self.root
        if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_BODY:
            raise SourceCollectionError('source_response_limit')
        self.feed(html)
        self.close()
        if not self.closed_html or len(list(self.root.walk('html'))) != 1 or len(list(self.root.walk('body'))) != 1:
            raise SourceCollectionError('source_markup_invalid')

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                if tag == 'html':
                    self.closed_html = True
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, value):
        self.current.children.append(value)


def _norm(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def _label(value):
    return _norm(value).casefold().rstrip(':').strip()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


def _rows(table):
    for row in table.walk('tr'):
        if row.ancestor('table') is table:
            cells = [node for node in row.children if isinstance(node, Node) and node.tag in {'td', 'th'}]
            if cells:
                yield cells


def _fields(table):
    fields = {}
    for cells in _rows(table):
        if len(cells) == 2:
            key = _label(cells[0].text())
            if key in fields:
                raise SourceCollectionError('source_fields_ambiguous')
            fields[key] = cells[1]
    return fields


def _date(value, *, with_time=False):
    value = _norm(value)
    pattern = r'\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?' if with_time else r'\d{4}-\d{2}-\d{2}'
    if not re.fullmatch(pattern, value):
        raise SourceCollectionError('source_date_invalid')
    try:
        if ' ' in value:
            return datetime.fromisoformat(value).replace(tzinfo=WARSAW).isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise SourceCollectionError('source_date_invalid') from None


def _no_pagination(document, path):
    for node in document.root.walk():
        if node.has_class('pagination') or node.has_class('pager') or node.attrs.get('rel') == 'next':
            raise SourceCollectionError('source_pagination_unsupported')
        href = node.attrs.get('href', '')
        parsed = urlsplit(urljoin(ORIGIN, href))
        if parsed.path.rstrip('/') == path and parsed.query:
            raise SourceCollectionError('source_pagination_unsupported')


def _unprocessed_media_counts(node):
    """Count visible image/document references without requesting their targets."""
    images = len(list(node.walk('img')))
    document_links = 0
    for link in node.walk('a'):
        path = urlsplit(link.attrs.get('href', '')).path.casefold()
        if re.search(r'\.(?:pdf|docx?|xlsx?|pptx?|od[tps]|rtf|txt|csv|zip|jpe?g|png|gif|webp|svg|mp[34]|wav|ogg|webm)$', path):
            document_links += 1
    return {'image_count': images, 'document_link_count': document_links,
            'link_count': len(list(node.walk('a'))), 'attachment_count': images + document_links}


def parse_announcements(html, *, learner_id, fetched_at):
    """Parse every current announcement using labels, never positional values."""
    doc = Document(html)
    _no_pagination(doc, '/ogloszenia')
    tables = [table for table in doc.root.walk('table')
              if table.has_class('printable') and table.has_class('decorated')]
    if not tables:
        if _norm(next(doc.root.walk('body')).text()).casefold() == 'brak ogłoszeń':
            return []
        raise SourceCollectionError('announcement_markup_invalid')
    if len(tables) > MAX_ANNOUNCEMENTS:
        raise SourceCollectionError('announcement_limit')
    result, seen = [], set()
    for table in tables:
        fields = _fields(table)
        if not {'dodał', 'data publikacji', 'treść'} <= fields.keys():
            raise SourceCollectionError('announcement_markup_invalid')
        titles = [cells[0].text() for cells in _rows(table)
                  if len(cells) == 1 and cells[0].attrs.get('colspan') == '2' and cells[0].text()]
        if len(titles) != 1:
            raise SourceCollectionError('announcement_markup_invalid')
        title, sender = _norm(titles[0]), _norm(fields['dodał'].text())
        published = _date(fields['data publikacji'].text())
        if not title or not sender:
            raise SourceCollectionError('announcement_markup_invalid')
        upstream_id = 'metadata:' + _digest([title, sender, published])
        if upstream_id in seen:
            # A duplicate metadata identity can represent two independent
            # announcements. Neither duplicate nor collision is safe to merge.
            raise SourceCollectionError('announcement_identity_ambiguous')
        seen.add(upstream_id)
        body = fields['treść'].text()
        result.append({'id': 'announcement:' + upstream_id, 'kind': 'announcement',
                       'upstream_id': upstream_id, 'learner_id': learner_id,
                       'title': title, 'sender': sender, 'published_at': published,
                       'updated_at': None, 'fetched_at': fetched_at, 'text': body, 'deleted': False,
                       'metadata': {'identity_basis': 'metadata_key', 'identity_ambiguous': True,
                                    'publication_precision': 'date', 'content_hash': _digest([title, sender, published, body]),
                                    **_unprocessed_media_counts(fields['treść']),
                                    'scope': 'all_current_announcements'}})
    return result


def parse_learner_context(html):
    """Read only the active profile; no account/child selection is performed."""
    doc = Document(html)
    observed = {}
    accepted = {'imię i nazwisko ucznia', 'imię i nazwisko', 'imię', 'nazwisko', 'klasa', 'oddział', 'szkoła', 'nazwa szkoły', 'identyfikator ucznia', 'id ucznia'}
    for table in doc.root.walk('table'):
        for cells in _rows(table):
            if len(cells) != 2:
                continue
            key = _label(cells[0].text())
            if key not in accepted:
                continue
            value = _norm(cells[1].text())
            if key in observed and observed[key] != value:
                raise SourceCollectionError('learner_context_ambiguous')
            observed[key] = value
    name = observed.get('imię i nazwisko ucznia') or observed.get('imię i nazwisko') or _norm(observed.get('imię', '') + ' ' + observed.get('nazwisko', ''))
    class_name = observed.get('klasa') or observed.get('oddział')
    school = observed.get('szkoła') or observed.get('nazwa szkoły')
    provider_id = observed.get('identyfikator ucznia') or observed.get('id ucznia')
    if not name or not class_name or (not provider_id and not school):
        raise SourceCollectionError('learner_context_unverified')
    if provider_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', provider_id):
        raise SourceCollectionError('learner_context_unverified')
    learner_id = 'synergia:student:' + provider_id if provider_id else 'synergia:profile:' + _digest([name, school])
    class_match = re.search(r'(?<!\d)(1[0-3]|[1-9])\s*([a-z])\b', class_name, re.I)
    class_id = (class_match.group(1) + class_match.group(2)).lower() if class_match else _norm(class_name).casefold()
    return {'id': learner_id, 'class_id': class_id, 'class_name': class_name,
            'name': name, 'school': school,
            'identity_basis': 'provider_id' if provider_id else 'profile_name_school',
            'selected_only': True}


def _calendar_form(doc):
    forms = [node for node in doc.root.walk('form') if node.attrs.get('id') == 'scheduleForm']
    if len(forms) != 1:
        raise SourceCollectionError('calendar_form_invalid')
    form = forms[0]
    target = urlsplit(urljoin(ORIGIN + '/terminarz', form.attrs.get('action', '')))
    if (target.scheme, target.hostname, target.path.rstrip('/'), target.query, target.fragment) != ('https', 'synergia.librus.pl', '/terminarz', '', ''):
        raise SourceCollectionError('calendar_form_invalid')
    if form.attrs.get('method', 'get').lower() != 'post':
        raise SourceCollectionError('calendar_form_invalid')
    selected, choices = {}, {}
    for select in form.walk('select'):
        key = select.attrs.get('name')
        if key not in {'miesiac', 'rok'}:
            continue
        if key in selected:
            raise SourceCollectionError('calendar_form_invalid')
        options = list(select.walk('option'))
        values = [node.attrs.get('value', '') for node in options if 'selected' in node.attrs]
        if len(values) != 1 or not values[0].isdigit():
            raise SourceCollectionError('calendar_form_invalid')
        selected[key] = int(values[0])
        choices[key] = {node.attrs.get('value', '') for node in options}
    if set(selected) != {'miesiac', 'rok'} or not 1 <= selected['miesiac'] <= 12 or not 1900 <= selected['rok'] <= 2200:
        raise SourceCollectionError('calendar_form_invalid')
    hidden = {}
    for node in form.walk('input'):
        name = node.attrs.get('name')
        if not name:
            continue
        if node.attrs.get('type', '').casefold() != 'hidden' or name not in {'requestkey'} or name in hidden:
            raise SourceCollectionError('calendar_form_invalid')
        hidden[name] = node.attrs.get('value', '')
    if not hidden.get('requestkey'):
        raise SourceCollectionError('calendar_form_invalid')
    return {'year': selected['rok'], 'month': selected['miesiac'], 'hidden': hidden, 'choices': choices}


def parse_calendar_month(html, *, start, end):
    doc = Document(html)
    form = _calendar_form(doc)
    tables = [node for node in doc.root.walk('table') if node.has_class('kalendarz')]
    if len(tables) != 1:
        raise SourceCollectionError('calendar_markup_invalid')
    days, entries = set(), []
    for day in tables[0].walk('div'):
        if not day.has_class('kalendarz-dzien'):
            continue
        match = re.match(r'\s*(\d{1,2})(?:\D|$)', day.text())
        if not match:
            raise SourceCollectionError('calendar_markup_invalid')
        try:
            when = date(form['year'], form['month'], int(match.group(1)))
        except ValueError:
            raise SourceCollectionError('calendar_date_invalid') from None
        if when.day in days:
            raise SourceCollectionError('calendar_markup_invalid')
        days.add(when.day)
        for cell in day.walk('td'):
            title = _norm(cell.text())
            if not title:
                continue
            if not start <= when <= end:
                continue
            onclick = cell.attrs.get('onclick', '').strip()
            match = re.fullmatch(r'''location\.href\s*=\s*['"](/terminarz/(szczegoly|szczegoly_wolne)/(\d+))['"];?''', onclick)
            # The rendered text can concatenate the type and following lesson
            # number without whitespace ("Zastępstwo4."). This is the observed
            # type label inside a validated calendar event cell, not prose.
            is_substitution = bool(re.search(r'zastępstwo', title, re.I))
            if onclick and not match:
                raise SourceCollectionError('calendar_detail_route_invalid')
            # Historical calendar entries can remain visible after their detail
            # action disappears. Accept only their observed event-cell shape,
            # retaining an explicitly ambiguous metadata identity for review.
            is_unlinked_event = (cell.has_class('no-border-left') and cell.has_class('no-border-right'))
            if not match and not is_substitution and not is_unlinked_event:
                raise SourceCollectionError('calendar_identity_unverified')
            tooltip = html_to_text(cell.attrs.get('title', ''))
            published = re.search(r'Data dodania:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)', tooltip, re.I)
            published = _date(published.group(1), with_time=True) if published else None
            kind = 'substitution' if is_substitution else 'school_event'
            upstream_id = (('holiday:' if match.group(2) == 'szczegoly_wolne' else 'detail:') + match.group(3)) if match else 'metadata:' + _digest([when.isoformat(), title])
            entries.append({'kind': kind, 'upstream_id': upstream_id, 'title': title,
                            'event_start': when.isoformat(), 'tooltip': tooltip,
                            'published_at': published, 'detail_path': match.group(1) if match else None})
            if len(entries) > MAX_CALENDAR_ITEMS:
                raise SourceCollectionError('calendar_limit')
    if days != set(range(1, monthrange(form['year'], form['month'])[1] + 1)):
        raise SourceCollectionError('calendar_month_incomplete')
    return form, entries


def parse_calendar_detail(html, *, holiday=False):
    doc = Document(html)
    allowed = {'data', 'data dodania', 'nauczyciel', 'nr lekcji', 'opis', 'przedmiot', 'rodzaj', 'zakres'}
    candidates = []
    for table in doc.root.walk('table'):
        fields = _fields(table)
        recognized = ({'zakres', 'data dodania'} <= fields.keys()) if holiday else ('data' in fields and ('rodzaj' in fields or 'opis' in fields))
        if recognized:
            candidates.append(fields)
    if len(candidates) != 1:
        raise SourceCollectionError('calendar_detail_markup_invalid')
    fields = candidates[0]
    if any(key not in allowed for key in fields):
        raise SourceCollectionError('calendar_detail_markup_invalid')
    result = {key: node.text() for key, node in fields.items()}
    if not holiday and not result['data']:
        raise SourceCollectionError('calendar_detail_markup_invalid')
    if 'data dodania' in result:
        result['published_at'] = _date(result['data dodania'], with_time=True)
    return result


def _read(client, path):
    response = client.follow(ORIGIN + path)
    final = urlsplit(getattr(response, 'url', '') or ORIGIN + path)
    if final.scheme != 'https' or final.hostname != 'synergia.librus.pl' or final.path.rstrip('/') != path.rstrip('/') or final.query or final.fragment:
        raise SourceCollectionError('source_session_invalid')
    return response.text


def _select_month(client, form, year, month):
    if str(month) not in form['choices']['miesiac'] or str(year) not in form['choices']['rok']:
        raise SourceCollectionError('calendar_window_unsupported')
    data = dict(form['hidden'], miesiac=str(month), rok=str(year))
    response = client.request('POST', ORIGIN + '/terminarz', data=data)
    if response.is_redirect:
        raise SourceCollectionError('source_session_invalid')
    # A successful POST is not proof that the view changed. Parse and verify it.
    selected = _calendar_form(Document(response.text))
    if (selected['year'], selected['month']) != (year, month):
        raise SourceCollectionError('calendar_selection_failed')
    return response.text, selected


def _months(start, end):
    year, month = start.year, start.month
    result = []
    while (year, month) <= (end.year, end.month):
        result.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if len(result) > MAX_MONTHS:
        raise SourceCollectionError('calendar_window_unsupported')
    return result


def collect_calendar(client, *, learner_id, fetched_at, start, end):
    html = _read(client, '/terminarz')
    form, unused = parse_calendar_month(html, start=start, end=end)
    original = (form['year'], form['month'])
    moved = False
    result, seen = [], set()
    try:
        for year, month in _months(start, end):
            if (form['year'], form['month']) != (year, month):
                # A failed POST may still change the session view. Restore in
                # finally even when parsing/verifying its response failed.
                moved = True
                html, form = _select_month(client, form, year, month)
            form, entries = parse_calendar_month(html, start=start, end=end)
            for entry in entries:
                identity = entry['kind'] + ':' + entry['upstream_id']
                if identity in seen:
                    raise SourceCollectionError('calendar_identity_ambiguous')
                seen.add(identity)
                detail = parse_calendar_detail(_read(client, entry['detail_path']),
                    holiday=entry['upstream_id'].startswith('holiday:')) if entry['detail_path'] else {}
                publication = detail.get('published_at') or entry['published_at']
                if detail.get('published_at') and entry['published_at'] and detail['published_at'] != entry['published_at']:
                    raise SourceCollectionError('calendar_publication_ambiguous')
                body = '\n'.join(f'{key}: {value}' for key, value in detail.items() if key != 'published_at') or entry['tooltip'] or entry['title']
                metadata = {'identity_basis': 'provider_id' if entry['detail_path'] else 'metadata_key',
                            'identity_ambiguous': not bool(entry['detail_path']),
                            'event_start': entry['event_start'], 'event_end': None, 'event_precision': 'date',
                            'publication_precision': 'second' if publication and 'T' in publication else 'date' if publication else 'unknown',
                            'window_start': start.isoformat(), 'window_end': end.isoformat(),
                            'scope': 'event_date_window', 'fields': {key: value for key, value in detail.items() if key != 'published_at'},
                            'content_hash': _digest([entry['title'], body, publication])}
                result.append({'id': identity, 'kind': entry['kind'], 'upstream_id': entry['upstream_id'],
                               'learner_id': learner_id, 'title': entry['title'], 'sender': detail.get('nauczyciel', ''),
                               'published_at': publication, 'updated_at': None, 'fetched_at': fetched_at,
                               'text': body, 'deleted': False, 'metadata': metadata})
                if len(result) > MAX_CALENDAR_ITEMS:
                    raise SourceCollectionError('calendar_limit')
    finally:
        if moved:
            # Obtain fresh form state after a failed request; never replay an
            # earlier requestkey. Restore only this temporary session's view.
            latest_html = _read(client, '/terminarz')
            latest = _calendar_form(Document(latest_html))
            if (latest['year'], latest['month']) != original:
                try:
                    _select_month(client, latest, *original)
                except Exception:
                    raise SourceCollectionError('calendar_restore_failed') from None
    return result


def collect_sources(client, *, current=None, past_days=30, future_days=60):
    """Collect a complete source snapshot or explicit per-kind safe failures."""
    if past_days != 30 or future_days != 60:
        raise SourceCollectionError('source_window_invalid')
    current = current or datetime.now(WARSAW)
    if not isinstance(current, datetime):
        raise SourceCollectionError('source_window_invalid')
    if current.tzinfo is None:
        current = current.replace(tzinfo=WARSAW)
    current = current.astimezone(WARSAW)
    fetched_at = datetime.now(timezone.utc).isoformat()
    result = {'sources': [], 'complete_kinds': [], 'learner': None, 'status': 'partial', 'errors': [],
              'fetched_at': fetched_at, 'window_start': (current.date() - timedelta(days=30)).isoformat(),
              'window_end': (current.date() + timedelta(days=60)).isoformat()}
    try:
        _read(client, '/rodzic/index')
        learner = parse_learner_context(_read(client, '/informacja'))
        result['learner'] = learner
    except Exception as error:
        result['errors'].append(error.code if isinstance(error, SourceCollectionError) else 'learner_collection_failed')
        return result
    for kinds, collect in (
        (('announcement',), lambda: parse_announcements(_read(client, '/ogloszenia'), learner_id=learner['id'], fetched_at=fetched_at)),
        (('school_event', 'substitution'), lambda: collect_calendar(client, learner_id=learner['id'], fetched_at=fetched_at,
            start=date.fromisoformat(result['window_start']), end=date.fromisoformat(result['window_end']))),
    ):
        try:
            sources = collect()
            result['sources'].extend(sources)
            result['complete_kinds'].extend(kinds)
        except Exception as error:
            result['errors'].append(error.code if isinstance(error, SourceCollectionError) else kinds[0] + '_collection_failed')
    try:
        if parse_learner_context(_read(client, '/informacja')) != learner:
            raise SourceCollectionError('learner_context_changed')
    except Exception as error:
        result['sources'], result['complete_kinds'] = [], []
        result['errors'].append(error.code if isinstance(error, SourceCollectionError) else 'learner_collection_failed')
    result['status'] = 'ok' if not result['errors'] and set(result['complete_kinds']) == set(KINDS) else 'partial'
    return result
