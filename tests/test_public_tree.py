"""Privacy guard regressions use generated fixtures and temporary local Git only."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zlib

from scripts.check_public_tree import MAX_BYTES, Scanner, main


def synthetic_token():
    # Assemble a fake recognizable token so scanner source has no token literal.
    return 'gh' + 'p_' + 'A' * 36


def png(*extra):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + b''.join(chunk(kind, data) for kind, data in extra)
            + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00')) + chunk(b'IEND', b''))


class PublicTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'source'
        self.root.mkdir()
        self.external = Path(self.tmp.name) / 'denylist.json'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        return path

    def run_scan(self, *flags):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(['--root', str(self.root), *flags])
        return code, output.getvalue() + errors.getvalue()

    def git(self, *args, **kwargs):
        # Fixture repositories have no remote and never involve GitHub authentication.
        result = subprocess.run(['git', '-C', str(self.root), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True, **kwargs)
        return result.stdout.decode().strip()

    def init_git(self):
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Synthetic Author')
        self.git('config', 'user.email', 'synthetic@example.invalid')

    def commit(self, message='Synthetic fixture commit'):
        self.git('add', '--all')
        return self.git('commit', '-q', '-m', message) or self.git('rev-parse', 'HEAD')

    def test_clean_pre_git_tree_and_intentional_public_owner(self):
        self.write('README.md', 'https://github.com/pbuchman/librus-calendar\nowner@example.com\n'
                   '368465+pbuchman@users.noreply.github.com\nhttp://127.0.0.1:8080\n')
        self.write('deploy/web.env.example', 'LIBRUS_CALENDAR_ID=primary\nWEB_TOKEN=<replace-me>\n')
        self.assertEqual(self.run_scan()[0], 0)

    def test_external_literals_are_case_insensitive_and_literal_not_regex(self):
        literal = 'Synthetic [Private] Person'
        self.external.write_text(json.dumps({'patterns': [literal]}))
        self.write('README.md', literal.upper())
        code, output = self.run_scan('--denylist', str(self.external))
        self.assertEqual(code, 1)
        self.assertIn('working-tree:README.md:1: private-literal', output)
        self.assertNotIn(literal.lower(), output.lower())
        self.write('README.md', 'Synthetic P Person')
        self.assertEqual(self.run_scan('--denylist', str(self.external))[0], 0)

    def test_private_literal_in_filename_is_redacted(self):
        literal = 'synthetic-private-parent'
        self.external.write_text(json.dumps({'patterns': [literal]}))
        self.write(literal + '.txt', 'innocent text')
        code, output = self.run_scan('--denylist', str(self.external))
        self.assertEqual(code, 1)
        self.assertIn('redacted-location:', output)
        self.assertNotIn(literal, output)

    def test_denylist_inside_tree_is_rejected(self):
        path = self.write('audit.json', json.dumps({'patterns': ['synthetic-private']}))
        code, output = self.run_scan('--denylist', str(path))
        self.assertEqual(code, 2)
        self.assertIn('outside', output)

    def test_missing_or_invalid_denylist_fails_closed(self):
        self.assertEqual(self.run_scan('--denylist', str(self.external))[0], 2)
        for data in ('not JSON', '{}', '{"patterns": [""]}', '{"patterns": [1]}'):
            self.external.write_text(data)
            self.assertEqual(self.run_scan('--denylist', str(self.external))[0], 2)

    def test_secret_patterns_never_print_values(self):
        token = synthetic_token()
        self.write('config.txt', token + '\n' + 'pass' + 'word = "' + 'nonplaceholder-value"\n')
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn('github-token', output)
        self.assertIn('credential-assignment', output)
        self.assertNotIn(token, output)
        self.assertNotIn('nonplaceholder-value', output)

    def test_clearly_marked_synthetic_credentials_are_allowed(self):
        self.write('fixture.py', 'password = "synthetic-password"\nweb_token = "test-token"\n'
                   'password = "YOUR_LIBRUS_PASSWORD"\n')
        self.assertEqual(self.run_scan()[0], 0)

    def test_personal_contact_private_host_address_and_home_path(self):
        private_values = ['parent' + '@school' + '.edu', '100.' + '80.90.10',
                          'machine.' + 'taila1b2.' + 'ts.net', '/Users/' + 'synthetic-person/source']
        self.write('notes.txt', '\n'.join(private_values))
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        for category in ('contact-address', 'private-network-address', 'private-host', 'personal-home-path'):
            self.assertIn(category, output)
        for value in private_values:
            self.assertNotIn(value, output)

    def test_runtime_filenames_and_binary_data_are_rejected(self):
        self.write('private/messages.json', '{}')
        self.write('state.sqlite3-wal', b'\x00\xffsample')
        self.write('archive.zip', b'\x00\xffsample')
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn('private-or-generated-directory', output)
        self.assertIn('private-or-runtime-file', output)
        self.assertIn('unreviewed-binary', output)

    def test_pre_git_generated_dependency_cache_is_excluded(self):
        self.write('.venv/package/credentials.json', synthetic_token())
        self.write('__pycache__/example.pyc', b'\x00\xffsample')
        self.write('README.md', 'Safe public source')
        self.assertEqual(self.run_scan()[0], 0)

    def test_ignored_runtime_is_excluded_but_tracked_ignored_file_is_checked(self):
        self.init_git()
        self.write('.gitignore', 'private/\n.venv/\n')
        self.write('private/credentials.json', synthetic_token())
        self.assertEqual(self.run_scan()[0], 0)
        self.git('add', '-f', 'private/credentials.json')
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn('private-or-generated-directory', output)
        self.assertIn('github-token', output)

    def test_staged_secret_is_checked_after_clean_working_edit(self):
        self.init_git()
        self.write('config.txt', synthetic_token())
        self.git('add', 'config.txt')
        self.write('config.txt', 'clean working content')
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn('index:config.txt', output)

    def test_history_includes_deleted_secret_and_ignored_path(self):
        self.init_git()
        self.write('private/deleted.txt', synthetic_token())
        self.commit()
        self.git('rm', 'private/deleted.txt')
        self.write('.gitignore', 'private/\n')
        self.commit('Remove synthetic secret fixture')
        self.assertEqual(self.run_scan()[0], 0)
        code, output = self.run_scan('--all-history')
        self.assertEqual(code, 1)
        self.assertIn('history:', output)
        self.assertIn('github-token', output)
        self.assertIn('private-or-generated-directory', output)

    def test_history_includes_other_branch_and_commit_metadata(self):
        self.init_git()
        self.write('README.md', 'clean')
        self.commit()
        self.git('checkout', '-q', '-b', 'fixture-branch')
        self.write('branch-only.txt', synthetic_token())
        self.commit('Synthetic branch metadata ' + synthetic_token())
        self.git('checkout', '-q', 'main')
        code, output = self.run_scan('--all-history')
        self.assertEqual(code, 1)
        self.assertIn('history:', output)
        self.assertIn('commit:', output)

    def test_history_personal_author_email_is_rejected(self):
        self.init_git()
        address = 'synthetic' + '@personal' + '.edu'
        self.git('config', 'user.email', address)
        self.write('README.md', 'clean')
        self.commit()
        code, output = self.run_scan('--all-history')
        self.assertEqual(code, 1)
        self.assertIn('commit:', output)
        self.assertIn('contact-address', output)
        self.assertNotIn(address, output)

    def test_annotated_tag_metadata_and_ref_names_are_checked_and_redacted(self):
        self.init_git()
        self.write('README.md', 'clean')
        self.commit()
        self.git('tag', '-a', 'fixture-tag', '-m', synthetic_token())
        literal = 'synthetic-private-ref'
        self.git('branch', literal)
        self.external.write_text(json.dumps({'patterns': [literal]}))
        code, output = self.run_scan('--all-history', '--denylist', str(self.external))
        self.assertEqual(code, 1)
        self.assertIn('tag:', output)
        self.assertIn('redacted-location:', output)
        self.assertNotIn(literal, output)

    def test_direct_blob_ref_is_checked(self):
        self.init_git()
        oid = self.git('hash-object', '-w', '--stdin', input=synthetic_token().encode())
        self.git('update-ref', 'refs/audit/fixture', oid)
        code, output = self.run_scan('--all-history')
        self.assertEqual(code, 1)
        self.assertIn('history-object:', output)
        self.assertIn('github-token', output)

    def test_shallow_history_is_rejected(self):
        self.init_git()
        self.write('README.md', 'clean')
        self.commit()
        self.write('.git/shallow', self.git('rev-parse', 'HEAD') + '\n')
        code, output = self.run_scan('--all-history')
        self.assertEqual(code, 2)
        self.assertIn('shallow', output)

    def test_missing_git_history_is_error_but_empty_git_repo_is_supported(self):
        self.assertEqual(self.run_scan('--all-history')[0], 2)
        self.init_git()
        code, output = self.run_scan('--all-history')
        self.assertEqual(code, 0)
        self.assertIn('0 commits', output)

    def test_png_pixel_fixture_allowed_only_in_documented_screenshot_directory(self):
        self.write('docs/screenshots/fixture.png', png())
        self.assertEqual(self.run_scan()[0], 0)
        self.write('unreviewed.png', png())
        self.assertIn('unreviewed-binary', self.run_scan()[1])

    def test_png_text_exif_and_trailing_payload_are_rejected(self):
        for kind in (b'tEXt', b'zTXt', b'iTXt', b'eXIf'):
            self.write('docs/screenshots/fixture.png', png((kind, b'Synthetic metadata')))
            code, output = self.run_scan()
            self.assertEqual(code, 1)
            self.assertIn('image-metadata', output)
        self.write('docs/screenshots/fixture.png', png() + b'trailing private payload')
        self.assertEqual(self.run_scan()[0], 2)

    def test_oversized_file_fails_instead_of_silently_skipping(self):
        self.write('large.txt', b'a' * (MAX_BYTES + 1))
        self.assertEqual(self.run_scan()[0], 2)

    def test_symlink_is_rejected_without_reading_external_target(self):
        self.external.write_text(synthetic_token())
        (self.root / 'link.txt').symlink_to(self.external)
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn('symlink', output)
        self.assertNotIn(synthetic_token(), output)

    def test_submodule_fails_instead_of_claiming_coverage(self):
        self.init_git()
        self.write('README.md', 'clean')
        self.commit()
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.git('rev-parse', 'HEAD') + ',vendor')
        code, output = self.run_scan()
        self.assertEqual(code, 1)
        self.assertIn('unscanned-submodule', output)


if __name__ == '__main__':
    unittest.main()
