"""Private, bounded technical form captures. Never serialize raw markup or values.

Static-looking identifiers are clues, not a determination of a form's purpose.
This intentionally narrow filter cannot establish the semantic meaning of names.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from .health_state import FORM_DIAGNOSTIC_CODES

MAX_BYTES = 128 * 1024
MAX_FILES = 100
MAX_FORMS = 16
MAX_CONTROLS = 32
MAX_ANCESTRY = 8
MAX_CLASSES = 4
ARTIFACT_NAME = re.compile(r'form-[a-f0-9]{32}\.json\Z')
CONTROLS = frozenset(('input', 'select', 'button', 'textarea'))


class _Descriptors:
    def __init__(self, sensitive_values):
        self.sensitive_values = sensitive_values
        self.redacted_identifiers = 0
        self.omitted_classes = 0
        self.omitted_path_segments = 0

    def identifier(self, value):
        if value is None or value == '':
            return None
        if (not isinstance(value, str) or len(value) > 32
                or not re.fullmatch(r'[A-Za-z_][A-Za-z_.:\[\]-]*', value)
                or re.search(r'[a-fA-F]{12,}', value)
                or re.search(r'[A-Za-z]{24,}', value)
                or value.casefold() in self.sensitive_values
                or (len(value) >= 12 and sum(
                    left.islower() and right.isupper() or left.isupper() and right.islower()
                    for left, right in zip(value, value[1:])) >= 8)):
            self.redacted_identifiers += 1
            return '[redacted]'
        return value

    def classes(self, node):
        values = (node.attrs.get('class') or '').split()
        self.omitted_classes += max(0, len(values) - MAX_CLASSES)
        return [self.identifier(value) for value in values[:MAX_CLASSES]]

    def structure(self, node):
        return dict(tag=self.identifier(node.tag), id=self.identifier(node.attrs.get('id')),
                    classes=self.classes(node), role=self.identifier(node.attrs.get('role')),
                    hidden='hidden' in node.attrs,
                    aria_hidden=(node.attrs.get('aria-hidden') or '').casefold() == 'true',
                    handler_present=any(key.startswith('on') for key in node.attrs))

    def control(self, node, association):
        return dict(**self.structure(node), name=self.identifier(node.attrs.get('name')),
            type=self.identifier(node.attrs.get('type', 'text') if node.tag == 'input' else node.attrs.get('type')),
            association=association, explicit_form_attribute='form' in node.attrs,
            form=self.identifier(node.attrs.get('form')))

    def route(self, value):
        if value is None or value == '':
            return ''
        try:
            target = urlsplit(value)
            if target.scheme and target.scheme.casefold() not in {'http', 'https'}:
                raise ValueError()
            path = target.path
            segments = path.split('/')
            self.omitted_path_segments += max(0, len(segments) - 12)
            return '/'.join(self.identifier(segment) or '' for segment in segments[:12])
        except (ValueError, TypeError):
            self.redacted_identifiers += 1
            return '[redacted]'


def _sensitive_response_values(doc):
    """Ephemeral suppression set only; these values never enter the artifact."""
    values = set()

    def remember(value):
        if not isinstance(value, str):
            return
        # Longer values cannot pass the identifier bound in the first place.
        for candidate in (value, unquote_plus(value)):
            candidate = candidate.strip()
            if 0 < len(candidate) <= 32:
                values.add(candidate.casefold())

    for node in doc.root.walk():
        if node.tag in CONTROLS | {'form', 'option'}:
            remember(node.attrs.get('value'))
        if node.tag == 'form':
            try:
                target = urlsplit(node.attrs.get('action') or '')
            except ValueError:
                continue  # Its route is separately redacted as invalid.
            remember(target.fragment)
            for component in (target.query, target.fragment):
                for unused, value in parse_qsl(component, keep_blank_values=True, max_num_fields=4096):
                    remember(value)
    return values


def describe_forms(doc, code):
    """Select attributes explicitly; text, values and arbitrary attributes are inert."""
    if code not in FORM_DIAGNOSTIC_CODES:
        raise ValueError('Unsupported private diagnostic code.')
    sanitizer = _Descriptors(_sensitive_response_values(doc))
    forms = list(doc.root.walk('form'))
    controls = [node for node in doc.root.walk() if node.tag in CONTROLS]
    result = []
    for position, form in enumerate(forms[:MAX_FORMS], 1):
        ancestry, node = [], form
        while node is not None and node.tag:
            ancestry.append(node)
            node = node.parent
        associated = []
        for node in controls:
            explicit = node.attrs.get('form')
            if node.ancestor('form') is form or (explicit and explicit == form.attrs.get('id')):
                associated.append(node)
        descriptor = dict(position=position, **sanitizer.structure(form),
            name=sanitizer.identifier(form.attrs.get('name')),
            method=sanitizer.identifier(form.attrs.get('method', 'get')),
            action_path=sanitizer.route(form.attrs.get('action', '')),
            ancestry=[sanitizer.structure(node) for node in reversed(ancestry[:MAX_ANCESTRY])],
            omitted_ancestor_count=max(0, len(ancestry) - MAX_ANCESTRY),
            control_count=len(associated), controls=[], omitted_control_count=max(0, len(associated)-MAX_CONTROLS))
        for node in associated[:MAX_CONTROLS]:
            descriptor['controls'].append(sanitizer.control(node,
                'descendant' if node.ancestor('form') is form else 'explicit'))
        result.append(descriptor)
    form_ids = {form.attrs.get('id') for form in forms if form.attrs.get('id')}
    unassociated = [node for node in controls if node.ancestor('form') is None
        and node.attrs.get('form') not in form_ids]
    unassociated_descriptors = [sanitizer.control(node, 'unassociated') for node in unassociated[:MAX_CONTROLS]]
    artifact = dict(schema_version=1, captured_at=datetime.now(timezone.utc).isoformat(),
        diagnostic_code=code, form_count=len(forms), forms=result,
        omitted_form_count=len(forms)-len(result),
        document_control_count=len(controls),
        unassociated_control_count=len(unassociated), unassociated_controls=unassociated_descriptors,
        omitted_unassociated_control_count=max(0, len(unassociated)-MAX_CONTROLS),
        redacted_identifier_count=sanitizer.redacted_identifiers,
        omitted_class_count=sanitizer.omitted_classes,
        omitted_path_segment_count=sanitizer.omitted_path_segments)
    # The bound is measured after encoding. Any additional omission is explicit.
    while len(json.dumps(artifact, ensure_ascii=True).encode()) > MAX_BYTES:
        candidate = next((form for form in reversed(result) if form['controls']), None)
        if candidate is not None:
            candidate['controls'].pop()
            candidate['omitted_control_count'] += 1
        elif unassociated_descriptors:
            unassociated_descriptors.pop()
            artifact['omitted_unassociated_control_count'] += 1
        elif result:
            result.pop()
            artifact['omitted_form_count'] += 1
        else:
            raise ValueError('Private diagnostic bound exceeded.')
    return artifact


def _open_parent(path):
    """Reject symlinks in every path component, including state-directory parents."""
    absolute = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Private diagnostic parent is unsafe.')
        return fd
    except BaseException:
        os.close(fd)
        raise


class PrivateFormCapture:
    """Write beside the state DB with owner-only permissions and bounded retention."""
    def __init__(self, state_path):
        self.parent = Path(state_path).parent

    def __call__(self, doc, code):
        payload = json.dumps(describe_forms(doc, code), ensure_ascii=True).encode()
        parent_fd = _open_parent(self.parent)
        directory_fd = None
        try:
            try:
                os.mkdir('calendar-form-diagnostics', mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            directory_fd = os.open('calendar-form-diagnostics', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Private diagnostic directory is unsafe.')
            # Lock the directory inode so concurrent collectors keep the same cap.
            import fcntl
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            artifact_id = secrets.token_hex(16)
            name = 'form-' + artifact_id + '.json'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._retain(directory_fd, name)
                os.fsync(directory_fd)
            except BaseException:
                os.unlink(name, dir_fd=directory_fd)
                raise
            return artifact_id
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            os.close(parent_fd)

    @staticmethod
    def _retain(directory_fd, current):
        owned = []
        for name in os.listdir(directory_fd):
            if not ARTIFACT_NAME.fullmatch(name):
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600):
                owned.append((info.st_mtime_ns, name))
        # Always retain the newly written artifact, even with an unusual clock.
        older = sorted((item for item in owned if item[1] != current), reverse=True)
        for unused, name in older[MAX_FILES-1:]:
            os.unlink(name, dir_fd=directory_fd)
