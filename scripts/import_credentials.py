#!/usr/bin/env python3
"""Migrate an owned temporary login<space>password file without exposing values."""
import argparse
import json
import os
from pathlib import Path
import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_config
import stat
import tempfile


def opened(path, writable=False):
    fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError('not_regular_owned_file')
    return fd, info


def migrate(source, target):
    fd, info = opened(source, True)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('input_too_large')
    text = raw.decode('utf-8').rstrip('\r\n')
    login, separator, password = text.partition(' ')
    if not separator or not login or not password:
        raise ValueError('expected_login_first_space_entire_remaining_password')
    credentials = {'login': login, 'password': password}
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_info = target.parent.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid():
        raise ValueError('credential_directory_not_owned_real_directory')
    target.parent.chmod(0o700)
    if os.path.lexists(target):
        fd, _ = opened(target)
        with os.fdopen(fd, 'r') as stream:
            existing = json.load(stream)
        if existing != credentials:
            raise ValueError('existing_credentials_differ_source_preserved')
        target.chmod(0o600)
        action = 'existing_identical'
    else:
        fd, temporary = tempfile.mkstemp(prefix='.credentials-', dir=target.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(credentials, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
            action = 'created'
        finally:
            os.unlink(temporary)
    fd, target_info = opened(target)
    with os.fdopen(fd, 'r') as stream:
        stored = json.load(stream)
    if stored != credentials or target_info.st_mode & 0o777 != 0o600:
        raise ValueError('target_verification_failed_source_preserved')
    fd, final_info = opened(source)
    with os.fdopen(fd, 'rb') as stream:
        current = stream.read(65537)
    if (info.st_dev, info.st_ino) != (final_info.st_dev, final_info.st_ino) or current != raw:
        raise ValueError('source_changed_source_preserved')
    source.unlink()
    return {'status': 'migrated', 'target_action': action, 'verified_equal': True, 'source_deleted': True, 'target_mode': '600'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--target', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.source, (args.target or load_config().credentials_path).expanduser())))
        return 0
    except ValueError as error:
        print(json.dumps({'status': str(error), 'source_deleted': False}))
    except Exception as error:
        print(json.dumps({'status': 'migration_failed', 'error_type': type(error).__name__, 'source_deleted': False}))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
