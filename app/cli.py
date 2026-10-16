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
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    backup = sub.add_parser('backup')
    backup.add_argument('--destination', type=Path)
    migrate = sub.add_parser('migrate-decisions', help='Transfer minimal decisions directly from a read-only old database; never copy message history.')
    migrate.add_argument('--from', dest='source', type=Path, required=True)
    migrate.add_argument('--allow-legacy-binding', action='store_true', help='Explicitly accept an unbound legacy source only after a read-only Calendar owner probe and exact event-calendar match.')
    rebuild = sub.add_parser('rebuild', help='Reconstruct recent local state from Librus and read-only Calendar discovery.')
    rebuild.add_argument('--days', type=int, default=30, choices=range(1, 31))
    rebuild.add_argument('--credentials', type=Path)
    prune = sub.add_parser('prune', help='Preview old message-content removal; --apply retains all write safety state.')
    prune.add_argument('--days', type=int, default=30, choices=range(1, 31))
    prune.add_argument('--apply', action='store_true')
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
    grouping = sub.add_parser('group-existing', help='Preview an explicit private alternative grouping request; apply its fingerprint-bound private plan without Calendar writes.')
    grouping.add_argument('--request-file', type=Path, help='Private JSON containing message_id, proposal_ids and source_evidence; preview only.')
    grouping.add_argument('--plan-file', type=Path, required=True)
    grouping.add_argument('--apply', action='store_true')
    pause = sub.add_parser('pause-writes')
    pause.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    store = None
    try:
        config = config if config is not None else load_config(args.config)
        if args.command in ('reanalyze', 'metadata-refresh'):
            def initialize_runtime():
                from .codex_runtime import CodexRuntime
                return CodexRuntime(config, read_only=args.command == 'reanalyze' or not args.apply)
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
        elif args.command in ('migrate-decisions', 'prune', 'group-existing'):
            from .data_admin import migrate_decisions, prune_state
            from .sync import sync_lock
            with sync_lock(store.path) as acquired:
                if not acquired:
                    result = {'status': 'already_running', 'calendar_writes': 0}
                elif args.command == 'group-existing':
                    from .offer_groups import group_existing, read_private_json
                    if args.apply:
                        if args.request_file:
                            raise ValueError('Apply uses only the exact saved plan.')
                        request = {}
                    else:
                        if not args.request_file:
                            raise ValueError('Preview requires a private explicit grouping request.')
                        request = read_private_json(args.request_file)
                        if set(request) != {'message_id','proposal_ids','source_evidence'}:
                            raise ValueError('Unexpected grouping request fields.')
                    result = group_existing(store, plan_path=args.plan_file, apply=args.apply, **request)
                elif args.command == 'migrate-decisions':
                    probe = None
                    if args.allow_legacy_binding:
                        from .codex_runtime import CodexRuntime
                        probe = CodexRuntime(config, read_only=True).probe()
                    result = migrate_decisions(store, args.source, allow_legacy_binding=args.allow_legacy_binding, verified_calendar=probe)
                else:
                    result = prune_state(store, days=args.days, apply=args.apply)
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
                return CodexRuntime(config, read_only=args.command == 'rebuild')
            if args.command == 'rebuild':
                from .data_admin import RebuildController
                result = RebuildController(store, client_factory=initialize_client, runtime_factory=initialize_runtime,
                                           days=args.days).run()
            else:
                result = SyncController(store, client_factory=initialize_client, runtime_factory=initialize_runtime,
                                        max_messages=args.limit).run(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 1 if result.get('status') in ('partial', 'failed', 'interrupted', 'review', 'already_running') else 0
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
