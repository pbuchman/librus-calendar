#!/usr/bin/env python3
"""Conservative source/privacy guard, not proof that publication is safe.

Default: tracked and intended untracked working files (or a pre-Git source tree).
--all-history additionally inspects every locally reachable ref, commit/tag metadata,
historical path and blob, including deleted files. A shallow clone is rejected.
Ignored untracked runtime files are outside the intended source tree; tracked and
historical files never receive that exemption. Images still require visual review.

Private literal patterns must live outside the repository in a JSON object with a
``patterns`` string list. Supply --denylist or PUBLIC_TREE_DENYLIST. No matched
value or content is printed. Exit codes: 0 clean, 1 findings, 2 incomplete/error.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import zlib


CACHE_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache',
              '.mypy_cache', '.ruff_cache', 'htmlcov', 'build', 'dist'}
PRIVATE_DIRS = {'private', '.private', 'runtime', 'state', 'backups', 'exports', 'logs', 'secrets', '.secrets'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.log', '.jsonl', '.ndjson', '.har', '.ics',
                    '.key', '.pem', '.p12', '.pfx', '.zip', '.gz', '.tar', '.7z', '.pcap', '.bak', '.pyc'}
PRIVATE_NAMES = {'credentials.json', 'tokens.json', 'cookies.json', 'auth.json',
                 'private-denylist.json', '.netrc', '.npmrc', 'id_rsa', 'id_ed25519'}
MAX_BYTES = 8 * 1024 * 1024
PNG_DIRS = ('docs/screenshots/', 'docs/assets/')
EMAIL = re.compile(r'(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])')
NOREPLY = re.compile(r'(?:\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com\Z', re.I)
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
HOME_PATH = re.compile(r'(?:/Use' + r'rs/|/ho' + r'me/|[A-Za-z]:\\Users\\)'
                       r'(?!USER\b|example\b|runner\b)[^\s/\\"\'<>]+', re.I)
PRIVATE_HOST = re.compile(r'\b(?:[A-Za-z0-9-]+\.)+(?:ts\.net|tailnet\.[A-Za-z0-9.-]+|local|internal)\b', re.I)
SECRET_PATTERNS = (
    ('private-key', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----')),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('aws-key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('provider-token', re.compile(r'\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,}|AIza[A-Za-z0-9_-]{30,})\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b')),
    ('url-credentials', re.compile(r'https?://[^\s/:"\'<>]+:[^\s/@"\'<>]+@', re.I)),
)
ASSIGNMENT = re.compile(
    r'\b(?:password|passwd|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|'
    r'web[_-]?token|bearer[_-]?token|secret)\b[\s"\']*[:=][\s]*["\']([^"\'\r\n]{8,})["\']', re.I)
SYNTHETIC = re.compile(r'(?:example|synthetic|dummy|fake|test|fixture)[-_]|YOUR_[A-Z0-9_]+\Z|'
                       r'(?:change|replace)[-_]me\b|not[-_]a[-_]secret\b', re.I)


class ScanError(Exception):
    """A safe diagnostic describing incomplete coverage."""


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    category: str
    line: int = 0


def git(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    # Local, read-only Git only. Never echo stderr, which may contain private paths.
    try:
        result = subprocess.run(['git', '-C', str(root), *args], input=input_data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise ScanError('Git is unavailable; requested coverage is incomplete.') from exc
    if result.returncode:
        raise ScanError('A local Git read failed; requested coverage is incomplete.')
    return result.stdout


def load_patterns(root: Path, filename: str | None) -> tuple[str, ...]:
    if not filename:
        return ()
    path = Path(filename).expanduser().resolve()
    if path.is_relative_to(root.resolve()):
        raise ScanError('The private denylist must be outside the scanned repository.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ScanError('The external denylist could not be loaded.') from exc
    if (not isinstance(data, dict) or set(data) != {'patterns'} or not isinstance(data['patterns'], list)
            or any(not isinstance(value, str) or not value.strip() for value in data['patterns'])):
        raise ScanError('The external denylist must contain only a nonempty-string patterns list.')
    return tuple(value.casefold() for value in data['patterns'])


class Scanner:
    def __init__(self, root: Path, patterns: tuple[str, ...] = ()):
        self.root = root.resolve()
        self.patterns = patterns
        self.findings: set[Finding] = set()
        self.files = 0
        self.commits = 0
        self.refs = 0
        self._scanned_blobs: set[str] = set()

    def text_categories(self, text: str):
        for pattern in self.patterns:
            start = text.casefold().find(pattern)
            if start >= 0:
                yield 'private-literal', start
        for category, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                yield category, match.start()
        for match in ASSIGNMENT.finditer(text):
            value = match.group(1)
            # Expressions and clearly marked fixtures are not literal credentials.
            if not SYNTHETIC.match(value) and not value.startswith(('$', '${', '{{', '<')):
                yield 'credential-assignment', match.start()
        for match in EMAIL.finditer(text):
            address = match.group().lower()
            domain = address.rsplit('@', 1)[1]
            if not (domain in {'example.com', 'example.org', 'example.net', 'example.invalid'}
                    or domain.endswith(('.invalid', '.test', '.example')) or NOREPLY.fullmatch(address)):
                yield 'contact-address', match.start()
        for match in IPV4.finditer(text):
            try:
                address = ipaddress.IPv4Address(match.group())
            except ipaddress.AddressValueError:
                continue
            networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10')
            # Network-range definitions are public examples; host addresses are not.
            if any(text[match.start():].startswith(network) for network in networks):
                continue
            if any(address in ipaddress.IPv4Network(network) for network in networks):
                yield 'private-network-address', match.start()
        for category, pattern in (('personal-home-path', HOME_PATH), ('private-host', PRIVATE_HOST)):
            for match in pattern.finditer(text):
                yield category, match.start()

    def safe_location(self, location: str) -> str:
        if any(self.text_categories(location)) or any(ord(char) < 32 or ord(char) == 127 for char in location):
            identity = hashlib.sha256(location.encode('utf-8', errors='surrogateescape')).hexdigest()[:12]
            return '[redacted-location:' + identity + ']'
        return location

    def add(self, location: str, category: str, line: int = 0):
        self.findings.add(Finding(self.safe_location(location), category, line))

    def scan_text(self, location: str, text: str):
        for category, offset in self.text_categories(text):
            self.add(location, category, text.count('\n', 0, offset) + 1)

    def scan_path(self, location: str, path: str):
        self.scan_text(location, path)
        parts = PurePosixPath(path).parts
        name = parts[-1].lower() if parts else ''
        if any(part.lower() in PRIVATE_DIRS or part.lower() in CACHE_DIRS for part in parts[:-1]):
            self.add(location, 'private-or-generated-directory')
        if (name in PRIVATE_NAMES or name.startswith('.env') and name != '.env.example'
                or name.endswith('.env') or PurePosixPath(name).suffix in PRIVATE_SUFFIXES
                or re.search(r'\.(?:sqlite3?|db)(?:-|\.)', name)):
            self.add(location, 'private-or-runtime-file')
        if name.endswith('.png') and not path.startswith(PNG_DIRS):
            self.add(location, 'unreviewed-binary')

    def scan_png(self, location: str, data: bytes):
        position = 8
        seen_end = False
        while position + 12 <= len(data):
            size, kind = struct.unpack('>I4s', data[position:position + 8])
            end = position + 12 + size
            if end > len(data):
                raise ScanError('A PNG is malformed; image coverage is incomplete.')
            payload = data[position + 8:position + 8 + size]
            expected_crc = struct.unpack('>I', data[position + 8 + size:end])[0]
            if zlib.crc32(kind + payload) & 0xffffffff != expected_crc:
                raise ScanError('A PNG checksum failed; image coverage is incomplete.')
            if kind in {b'tEXt', b'zTXt', b'iTXt', b'eXIf'}:
                self.add(location, 'image-metadata')
            position = end
            if kind == b'IEND':
                seen_end = True
                break
        if not seen_end or position != len(data):
            raise ScanError('A PNG has missing or trailing data; image coverage is incomplete.')

    def scan_content(self, location: str, path: str, data: bytes):
        self.files += 1
        if len(data) > MAX_BYTES:
            raise ScanError('A source file exceeds the scan size limit; coverage is incomplete.')
        # Raw string checks apply to binary data too, including UTF-16 literal leaks.
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            if not path.startswith(PNG_DIRS) or not path.lower().endswith('.png'):
                self.add(location, 'unreviewed-binary')
            self.scan_png(location, data)
            self.scan_text(location, data.decode('latin-1'))
            return
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            self.add(location, 'unreviewed-binary')
            self.scan_text(location, data.decode('latin-1'))
            for encoding in ('utf-16-le', 'utf-16-be'):
                self.scan_text(location, data.decode(encoding, errors='ignore'))
            return
        if '\x00' in text:
            self.add(location, 'unreviewed-binary')
        self.scan_text(location, text)

    def is_repository(self):
        # Avoid accidentally scanning a parent checkout when this directory is not a repository.
        return (self.root / '.git').exists()

    def working_tree(self):
        if self.is_repository():
            paths = git(self.root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')
            names = {os.fsdecode(path) for path in paths if path}
        else:
            names = set()
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                for name in list(dirs):
                    entry = Path(directory) / name
                    if entry.is_symlink():
                        names.add(entry.relative_to(self.root).as_posix())
                dirs[:] = [name for name in dirs if name not in CACHE_DIRS and not (Path(directory) / name).is_symlink()]
                names.update((Path(directory) / name).relative_to(self.root).as_posix() for name in files)
        for name in sorted(names):
            location = 'working-tree:' + name
            self.scan_path(location, name)
            path = self.root / name
            if path.is_symlink():
                self.add(location, 'symlink')
                continue
            if not path.exists():
                # A tracked file deleted in the working tree is covered by the index/history.
                continue
            try:
                with path.open('rb') as source:
                    data = source.read(MAX_BYTES + 1)
            except OSError as exc:
                raise ScanError('A working file could not be read; coverage is incomplete.') from exc
            self.scan_content(location, name, data)
        if self.is_repository():
            # Scan the staged version too: a later working edit must not conceal staged secrets.
            for entry in git(self.root, 'ls-files', '--stage', '-z').split(b'\0'):
                if not entry:
                    continue
                header, path_bytes = entry.split(b'\t', 1)
                mode, oid, stage = header.decode('ascii').split()
                name = os.fsdecode(path_bytes)
                self.scan_entry('index:' + name, name, mode, oid, stage)

    def scan_entry(self, location: str, path: str, mode: str, oid: str, stage: str = '0'):
        self.scan_path(location, path)
        if stage != '0':
            raise ScanError('The Git index has unresolved conflicts; coverage is incomplete.')
        if mode == '160000':
            self.add(location, 'unscanned-submodule')
            return
        if mode == '120000':
            self.add(location, 'symlink')
        if oid not in self._scanned_blobs:
            self._scanned_blobs.add(oid)
            size = int(git(self.root, 'cat-file', '-s', oid))
            if size > MAX_BYTES:
                raise ScanError('A Git blob exceeds the scan size limit; coverage is incomplete.')
            self.scan_content(location, path, git(self.root, 'cat-file', 'blob', oid))

    def all_history(self):
        if not self.is_repository():
            raise ScanError('--all-history requires a Git repository.')
        if git(self.root, 'rev-parse', '--is-shallow-repository').strip() != b'false':
            raise ScanError('A shallow Git clone cannot provide full-history coverage.')
        for entry in git(self.root, 'for-each-ref', '--format=%(refname) %(objecttype) %(objectname)').splitlines():
            ref, kind, oid = entry.decode('utf-8', errors='surrogateescape').rsplit(' ', 2)
            self.refs += 1
            self.scan_text('ref:' + ref, ref)
            if kind == 'tag':
                self.scan_text('tag:' + oid[:12], git(self.root, 'cat-file', 'tag', oid).decode('utf-8', errors='replace'))
        for raw_commit in git(self.root, 'rev-list', '--all').splitlines():
            commit = raw_commit.decode('ascii')
            self.commits += 1
            self.scan_text('commit:' + commit[:12], git(self.root, 'cat-file', 'commit', commit).decode('utf-8', errors='replace'))
            for entry in git(self.root, 'ls-tree', '-r', '--full-tree', '-z', commit).split(b'\0'):
                if not entry:
                    continue
                header, name_bytes = entry.split(b'\t', 1)
                mode, _kind, oid = header.decode('ascii').split()
                name = os.fsdecode(name_bytes)
                self.scan_entry('history:' + commit[:12] + ':' + name, name, mode, oid)
        # Include blobs reached directly by unusual refs and annotated tag chains.
        objects = git(self.root, 'rev-list', '--objects', '--all')
        ids = [entry.split(b' ', 1)[0] for entry in objects.splitlines()]
        if ids:
            types = git(self.root, 'cat-file', '--batch-check=%(objectname) %(objecttype)',
                        input_data=b'\n'.join(ids) + b'\n')
            for entry in types.splitlines():
                oid, kind = entry.decode('ascii').split()
                if kind == 'blob' and oid not in self._scanned_blobs:
                    self.scan_entry('history-object:' + oid[:12], '', '100644', oid)
                elif kind == 'tag':
                    self.scan_text('tag:' + oid[:12], git(self.root, 'cat-file', 'tag', oid).decode('utf-8', errors='replace'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--working-tree', action='store_true', help='Scan working files and staged content (the default).')
    parser.add_argument('--all-history', action='store_true', help='Also inspect all locally reachable Git history and metadata.')
    parser.add_argument('--denylist', default=os.environ.get('PUBLIC_TREE_DENYLIST'), help='External JSON file of private literal patterns.')
    args = parser.parse_args(argv)
    try:
        if not args.root.is_dir():
            raise ScanError('The scan root is not a directory.')
        scanner = Scanner(args.root, load_patterns(args.root, args.denylist))
        scanner.working_tree()
        if args.all_history:
            scanner.all_history()
        for finding in sorted(scanner.findings):
            line = ':' + str(finding.line) if finding.line else ''
            print(f'{finding.location}{line}: {finding.category}')
        if scanner.findings:
            print(f'FAIL: {len(scanner.findings)} findings; no matched content was printed.')
            return 1
        print(f'PASS: {scanner.files} file versions, {scanner.commits} commits, {scanner.refs} refs; '
              'automated patterns only, manual publication review still required.')
        return 0
    except (ScanError, ValueError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, ScanError) else 'Invalid Git data; coverage is incomplete.'
        print('ERROR: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
