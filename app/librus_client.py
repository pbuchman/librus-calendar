#!/usr/bin/env python3
"""Narrow Librus client with HTTPS-only credentials and inert body decoding."""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import urljoin, urlparse, quote, urlunparse

from .source_incidents import (classified, note_http_request, note_http_response, preserve_classification,
                               shape, source_step)

ALLOWED_HOSTS = {'synergia.librus.pl', 'api.librus.pl', 'wiadomosci.librus.pl'}
LOGIN_URL = 'https://synergia.librus.pl/loguj/portalRodzina'
OAUTH_URL = 'https://api.librus.pl/OAuth/Authorization?client_id=46'
INBOX_URL = 'https://wiadomosci.librus.pl/api/inbox/messages'
MAX_BODY = 2_000_000


class PocError(Exception):
    pass


class DiscoveryParsingError(PocError):
    code = 'discovery_parsing_failed'


class MessageParsingError(PocError):
    diagnostic_code = 'message_fetch_parsing'


def now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    # Strip terminal escape sequences and non-printing control characters.
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    return ''.join(c for c in value if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))


class TextOnly(HTMLParser):
    BLOCKS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'blockquote'}
    DROP = {'script', 'style', 'iframe', 'object', 'svg', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.hidden.append(tag)
        if not self.hidden and tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if not self.hidden and tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_to_text(html):
    parser = TextOnly()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in clean_text(''.join(parser.parts)).splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def read_private_json(path):
    path = Path(path).expanduser()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PocError('Private JSON file must be owned by this user, regular, and mode 600.')
        with os.fdopen(fd, 'r') as stream:
            fd = None
            return json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)


class LibrusClient:
    def __init__(self, login, password):
        import requests
        self.session = requests.Session()
        self.session.max_redirects = 10
        self.session.headers.update({'User-Agent': 'Librus-Personal-POC/0.1', 'Accept': 'application/json,text/html'})
        with source_step('login_entry', 'abort_login'):
            initial = self.request('GET', LOGIN_URL)
            note_http_response(initial, 'GET')
            target = initial.headers.get('Location')
            if not target:
                raise classified(PocError('Synergia did not provide the expected OAuth redirect.'),
                                 'login_redirect_missing', expected='redirect_location', actual='missing')
        with source_step('login_oauth_bootstrap', 'abort_login'):
            self.request('GET', urljoin(LOGIN_URL, target))
        with source_step('login_submit', 'abort_login'):
            response = self.request('POST', OAUTH_URL, data={'action': 'login', 'login': login, 'pass': password},
                                    headers={'X-Requested-With': 'XMLHttpRequest', 'Origin': 'https://api.librus.pl', 'Referer': OAUTH_URL})
            note_http_response(response, 'POST')
            result = self.json(response)
            target = result.get('goTo')
            if not target:
                raise classified(PocError('Login did not return an OAuth target; check credentials or changed login requirements.'),
                                 'login_target_missing', expected='oauth_target',
                                 actual=shape(target) if 'goTo' in result else 'missing')
            try:
                oauth_target = urljoin(OAUTH_URL + '&response_type=code&scope=mydata', target)
            except (TypeError, ValueError) as error:
                raise classified(error, 'login_target_invalid', expected='oauth_target', actual=shape(target))
        with source_step('login_oauth_redirect', 'abort_login'):
            self.follow(oauth_target)
            if not any(c.name == 'oauth_token' and c.domain.lstrip('.') == 'synergia.librus.pl' for c in self.session.cookies):
                raise classified(PocError('Login did not create a Synergia OAuth session.'),
                                 'login_oauth_session_missing', expected='oauth_cookie', actual='missing')
        with source_step('login_message_session', 'abort_login'):
            response = self.follow('https://synergia.librus.pl/wiadomosci3')
            note_http_response(response, 'GET')
            if 'Brak dostępu' in response.text:
                raise classified(PocError('Synergia denied access to messages.'),
                                 'login_message_access_denied', expected='messages_access', actual='access_denied')
            if not any(c.domain.lstrip('.') == 'wiadomosci.librus.pl' for c in self.session.cookies):
                raise classified(PocError('Login did not create a messages-domain session.'),
                                 'login_message_session_missing', expected='messages_cookie', actual='missing')

    def request(self, method, url, **kwargs):
        note_http_request(method)
        try:
            parsed = urlparse(url)
            trusted = (parsed.scheme == 'https' and parsed.hostname in ALLOWED_HOSTS
                       and parsed.port in (None, 443) and not parsed.username)
        except ValueError as error:
            raise classified(error, 'unexpected_destination', expected='trusted_endpoint', actual='unexpected_destination')
        if not trusted:
            raise classified(PocError('Refused an unexpected authentication or API destination.'),
                             'unexpected_destination', expected='trusted_endpoint', actual='unexpected_destination')
        response = self.session.request(method, url, allow_redirects=False, timeout=(10, 25), **kwargs)
        note_http_response(response, method)
        response.raise_for_status()
        if len(response.content) > MAX_BODY:
            raise classified(PocError('Response exceeded the POC size limit.'), 'response_limit', actual='above_limit')
        return response

    def follow(self, url):
        for hop in range(11):
            note_http_request('GET', hop=hop)
            response = self.request('GET', url)
            note_http_response(response, 'GET')
            location = response.headers.get('Location')
            if not location:
                return response
            url = urljoin(url, location)
            target = urlparse(url)
            # Librus bootstrap currently emits an HTTP messages redirect.
            # Upgrade only this exact trusted destination; never send a cleartext request.
            if target.scheme == 'http' and target.hostname == 'wiadomosci.librus.pl' and target.port in (None, 80):
                url = urlunparse(target._replace(scheme='https', netloc='wiadomosci.librus.pl'))
        raise classified(PocError('Authentication redirect limit exceeded.'), 'redirect_limit', actual='above_limit', limit=11)

    @staticmethod
    def json(response):
        try:
            data = response.json()
        except ValueError:
            raise classified(PocError('Librus returned a non-JSON API response.'),
                             'json_decode_failed', expected='json_object', actual='invalid_json') from None
        if not isinstance(data, dict):
            raise classified(PocError('Librus returned an unexpected API object.'),
                             'json_shape_invalid', expected='json_object', actual=shape(data))
        return data

    @staticmethod
    def _check_inbox_session(response):
        if response.is_redirect:
            raise classified(PocError('Librus API session expired.'), 'source_session_invalid',
                             expected='nonredirect_response', actual='redirect_response')

    def messages(self, limit=10, page=1):
        with source_step('message_list', 'abort_page'):
            response = self.request('GET', INBOX_URL, params={'page': page, 'limit': limit})
            note_http_response(response, 'GET')
            self._check_inbox_session(response)
            try:
                payload = self.json(response)
            except PocError as error:
                raise preserve_classification(DiscoveryParsingError('Inbox response could not be parsed.'), error) from None
            data = payload.get('data')
            if not isinstance(data, list) or len(data) > limit:
                evidence = {'expected': 'array', 'actual': shape(data) if 'data' in payload else 'missing'}
                if isinstance(data, list):
                    evidence.update(actual='above_limit', observed_count=len(data))
                raise classified(DiscoveryParsingError('Unexpected inbox schema or server ignored the fetch limit.'),
                                 'message_page_shape', **evidence)
            return data, payload.get('total')

    def message(self, message_id):
        with source_step('message_body', 'continue_fetch'):
            response = self.request('GET', INBOX_URL + '/' + quote(message_id, safe=''))
            note_http_response(response, 'GET')
            self._check_inbox_session(response)
            try:
                payload = self.json(response)
            except PocError as error:
                raise preserve_classification(MessageParsingError('Message response could not be parsed.'), error) from None
            data = payload.get('data')
            if not isinstance(data, dict):
                raise classified(MessageParsingError('Unexpected message schema or identity mismatch.'),
                                 'message_body_shape', expected='object',
                                 actual=shape(data) if 'data' in payload else 'missing')
            if str(data.get('messageId')) != message_id:
                raise classified(MessageParsingError('Unexpected message schema or identity mismatch.'),
                                 'message_identity_mismatch', expected='requested_identity',
                                 actual='missing_identity' if data.get('messageId') is None else 'other_identity')
            try:
                html = base64.b64decode(data['Message'], validate=True).decode('utf-8', errors='replace')
            except (KeyError, ValueError, TypeError) as error:
                actual = 'missing' if isinstance(error, KeyError) else 'wrong_type' if isinstance(error, TypeError) else 'invalid_base64'
                raise classified(MessageParsingError('Message body is not the expected base64 content.'),
                                 'message_body_encoding', expected='strict_base64', actual=actual) from None
            return {'message_id': message_id, 'subject': clean_text(data.get('topic', '')),
                    'sender': clean_text(data.get('senderName') or ' '.join(str(data.get(k) or '') for k in ('senderFirstName', 'senderLastName'))).strip(),
                    'sent_at': clean_text(data.get('sendDate', '')), 'text': html_to_text(html),
                    'read_date_returned': clean_text(data.get('readDate') or ''),
                    'attachment_count': len(data.get('attachments') or [])}

    def collect_sources(self, *, current=None, past_days=30, future_days=60):
        """Read the selected learner's text sources using this authenticated session."""
        from .source_collectors import collect_sources
        return collect_sources(self, current=current, past_days=past_days, future_days=future_days)



def load_credentials(path=None):
    if path is None:
        from .config import load_config
        path = load_config().credentials_path
    data = read_private_json(path)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ('login','password')):
        raise PocError('Private credential file is invalid.')
    return data
