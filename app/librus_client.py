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

ALLOWED_HOSTS = {'synergia.librus.pl', 'api.librus.pl', 'wiadomosci.librus.pl'}
LOGIN_URL = 'https://synergia.librus.pl/loguj/portalRodzina'
OAUTH_URL = 'https://api.librus.pl/OAuth/Authorization?client_id=46'
INBOX_URL = 'https://wiadomosci.librus.pl/api/inbox/messages'
MAX_BODY = 2_000_000


class PocError(Exception):
    pass


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
        initial = self.request('GET', LOGIN_URL)
        target = initial.headers.get('Location')
        if not target:
            raise PocError('Synergia did not provide the expected OAuth redirect.')
        self.request('GET', urljoin(LOGIN_URL, target))
        response = self.request('POST', OAUTH_URL, data={'action': 'login', 'login': login, 'pass': password},
                                headers={'X-Requested-With': 'XMLHttpRequest', 'Origin': 'https://api.librus.pl', 'Referer': OAUTH_URL})
        result = self.json(response)
        target = result.get('goTo')
        if not target:
            raise PocError('Login did not return an OAuth target; check credentials or changed login requirements.')
        self.follow(urljoin(OAUTH_URL + '&response_type=code&scope=mydata', target))
        if not any(c.name == 'oauth_token' and c.domain.lstrip('.') == 'synergia.librus.pl' for c in self.session.cookies):
            raise PocError('Login did not create a Synergia OAuth session.')
        response = self.follow('https://synergia.librus.pl/wiadomosci3')
        if 'Brak dostępu' in response.text:
            raise PocError('Synergia denied access to messages.')
        if not any(c.domain.lstrip('.') == 'wiadomosci.librus.pl' for c in self.session.cookies):
            raise PocError('Login did not create a messages-domain session.')

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443) or parsed.username:
            raise PocError('Refused an unexpected authentication or API destination.')
        response = self.session.request(method, url, allow_redirects=False, timeout=(10, 25), **kwargs)
        response.raise_for_status()
        if len(response.content) > MAX_BODY:
            raise PocError('Response exceeded the POC size limit.')
        return response

    def follow(self, url):
        for _ in range(11):
            response = self.request('GET', url)
            location = response.headers.get('Location')
            if not location:
                return response
            url = urljoin(url, location)
            target = urlparse(url)
            # Librus bootstrap currently emits an HTTP messages redirect.
            # Upgrade only this exact trusted destination; never send a cleartext request.
            if target.scheme == 'http' and target.hostname == 'wiadomosci.librus.pl' and target.port in (None, 80):
                url = urlunparse(target._replace(scheme='https', netloc='wiadomosci.librus.pl'))
        raise PocError('Authentication redirect limit exceeded.')

    @staticmethod
    def json(response):
        try:
            data = response.json()
        except ValueError:
            raise PocError('Librus returned a non-JSON API response.') from None
        if not isinstance(data, dict):
            raise PocError('Librus returned an unexpected API object.')
        return data

    def messages(self, limit=10, page=1):
        response = self.request('GET', INBOX_URL, params={'page': page, 'limit': limit})
        if response.is_redirect:
            raise PocError('Librus API session expired.')
        payload = self.json(response)
        if not isinstance(payload.get('data'), list) or len(payload['data']) > limit:
            raise PocError('Unexpected inbox schema or server ignored the fetch limit.')
        return payload['data'], payload.get('total')

    def message(self, message_id):
        response = self.request('GET', INBOX_URL + '/' + quote(message_id, safe=''))
        if response.is_redirect:
            raise PocError('Librus API session expired.')
        data = self.json(response).get('data')
        if not isinstance(data, dict) or str(data.get('messageId')) != message_id:
            raise PocError('Unexpected message schema or identity mismatch.')
        try:
            html = base64.b64decode(data['Message'], validate=True).decode('utf-8', errors='replace')
        except (KeyError, ValueError, TypeError):
            raise PocError('Message body is not the expected base64 content.') from None
        return {'message_id': message_id, 'subject': clean_text(data.get('topic', '')),
                'sender': clean_text(data.get('senderName') or ' '.join(str(data.get(k) or '') for k in ('senderFirstName', 'senderLastName'))).strip(),
                'sent_at': clean_text(data.get('sendDate', '')), 'text': html_to_text(html),
                'read_date_returned': clean_text(data.get('readDate') or ''),
                'attachment_count': len(data.get('attachments') or [])}



def load_credentials(path=None):
    if path is None:
        from .config import load_config
        path = load_config().credentials_path
    data = read_private_json(path)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ('login','password')):
        raise PocError('Private credential file is invalid.')
    return data
