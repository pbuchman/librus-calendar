"""Service entrypoint; emits sanitized counts, never credentials/bodies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from .core import AppStore
from .config import ConfigurationError, load_config


def main(argv=None, *, config=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    backup = sub.add_parser('backup')
    backup.add_argument('--destination', type=Path)
    run = sub.add_parser('sync')
    run.add_argument('--dry-run', action='store_true')
    run.add_argument('--limit', type=int, default=100, choices=range(1, 101))
    run.add_argument('--credentials', type=Path)
    reanalysis = sub.add_parser('reanalyze', help='Preview selected cached messages; --apply supersedes only untouched local proposals.')
    reanalysis.add_argument('--message-id', action='append', required=True)
    reanalysis.add_argument('--apply', action='store_true')
    metadata = sub.add_parser('metadata-refresh', help='Preview title/timestamp updates to explicit existing event IDs.')
    metadata.add_argument('--event-id', action='append', required=True)
    metadata.add_argument('--apply', action='store_true')
    pause = sub.add_parser('pause-writes')
    pause.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    store = None
    try:
        config = config if config is not None else load_config()
        if args.command in ('reanalyze', 'metadata-refresh'):
            def initialize_runtime():
                from .codex_runtime import CodexRuntime
                return CodexRuntime(config)
            state_path = args.state or config.state_path
            if args.command == 'reanalyze':
                from .sync import reanalyze_cached
                result = reanalyze_cached(state_path, args.message_id, apply=args.apply, runtime_factory=initialize_runtime, config=config)
            else:
                from .metadata_refresh import refresh_metadata
                result = refresh_metadata(state_path, args.event_id, apply=args.apply, runtime_factory=initialize_runtime, config=config)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 1 if result.get('status') in ('partial', 'failed', 'interrupted', 'refused', 'already_running') else 0
        store = AppStore(args.state, config=config)
        if args.command == 'status':
            result = store.status()
        elif args.command == 'backup':
            result = {'status': 'backed_up', 'path': store.backup(args.destination)}
        elif args.command == 'pause-writes':
            result = store.set_writes_paused(not args.resume)
        else:
            from .sync import SyncController
            def initialize_client(stage):
                from .librus_client import LibrusClient, load_credentials
                config.require_calendar()
                credentials = load_credentials(args.credentials or config.credentials_path)
                account = hashlib.sha256(credentials['login'].encode()).hexdigest()
                old = store._setting('librus_account_hash')
                if old and old != account:
                    from .codex_runtime import RuntimeFailure
                    raise RuntimeFailure('account_mismatch')
                stage('login')
                client = LibrusClient(credentials['login'], credentials['password'])
                store.set_setting('librus_account_hash', account)
                return client
            def initialize_runtime():
                from .codex_runtime import CodexRuntime
                return CodexRuntime(config)
            result = SyncController(store, client_factory=initialize_client, runtime_factory=initialize_runtime,
                                    max_messages=args.limit).run(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 1 if result.get('status') in ('partial', 'failed', 'interrupted') else 0
    except ConfigurationError:
        print(json.dumps({'status': 'configuration_required', 'private_response_logged': False}))
        return 2
    except FileNotFoundError:
        print(json.dumps({'status': 'missing_required_private_file', 'credentials_logged': False}))
        return 2
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__, 'private_response_logged': False}))
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
