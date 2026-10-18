"""Public export checks use invented values and never inspect a real library."""
import tempfile
from pathlib import Path
import unittest
import zipfile

from audit_publication import findings, scan


class PublicationAuditTests(unittest.TestCase):
    def test_reports_secret_location_without_echoing_value(self):
        secret = 'ghp_' + 'a' * 36
        for name in ('config.py', 'LICENSE'):
            with self.subTest(name=name):
                result = findings(name, ('first\n' + secret).encode())
                self.assertEqual(result, [{'file':name, 'kind':'github_token', 'line':2}])
                self.assertNotIn(secret, str(result))

    def test_personal_paths_and_email_need_review_but_examples_are_allowed(self):
        path = str(Path('C:/') / 'Users' / 'private-person' / 'Desktop')
        email = 'person@' + 'mail.invalid'
        kinds = {r['kind'] for r in findings('README.md', (path + '\n' + email).encode())}
        self.assertEqual(kinds, {'personal_home_path', 'email_to_review'})
        self.assertEqual(findings('tests/sample.py', b'C:/Users/sample/Desktop'), [])

    def test_custom_private_terms_are_reported_without_value(self):
        result = findings('sample.py', b'handle = "invented_person"', ['invented_person'])
        self.assertEqual(result[0]['kind'], 'private_term')
        self.assertNotIn('invented_person', str(result))

    def test_configuration_and_shell_files_are_scanned(self):
        secret = 'github_pat_' + 'z' * 40
        for name in ('.github/workflows/checks.yml', 'config.yaml', 'config.toml',
                     'config.ini', 'config.cfg', 'config.conf', '.env.example',
                     'start.bat', 'start.ps1', 'start.sh'):
            with self.subTest(name=name):
                result = findings(name, ('value = ' + secret).encode())
                self.assertEqual([item['kind'] for item in result], ['github_token'])
                self.assertNotIn(secret, str(result))

    def test_only_declared_whole_urls_in_declared_fixture_files_are_exempt(self):
        fixtures = (
            ('tests/python/test_source_http.py',
             'https://' + 'user:pass' + '@pbs.twimg.com/media/a.jpg'),
            ('tests/python/test_task_history.py',
             'http://' + 'private:secret' + '@127.0.0.1:7890'),
        )
        for name, url in fixtures:
            with self.subTest(name=name):
                self.assertEqual(findings(name, ('url = "' + url + '"').encode()), [])
                for changed_name, changed_url in (
                    ('tests/python/new_test.py', url), ('settings.yaml', url),
                    (name, url.replace(':pass@', ':changed@').replace(':secret@', ':changed@')),
                    (name, url + '?extra=value'), (name, url + '/extra'),
                    (name, url.replace('127.0.0.1', 'example.invalid').replace('pbs.twimg.com', 'example.invalid')),
                ):
                    result = findings(changed_name, changed_url.encode())
                    self.assertEqual([item['kind'] for item in result], ['credential_url'])
                    self.assertNotIn(changed_url, str(result))

    def test_exempt_fixture_does_not_hide_another_credential_on_same_line(self):
        allowed = 'http://' + 'private:secret' + '@127.0.0.1:7890'
        other = 'https://' + 'invented:password' + '@example.invalid/'
        data = ('urls = ["' + allowed + '", "' + other + '"]').encode()
        result = findings('tests/python/test_task_history.py', data)
        self.assertEqual(result, [{'file': 'tests/python/test_task_history.py',
                                  'kind': 'credential_url', 'line': 1}])

    def test_generated_data_is_detected_inside_release_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'release.zip'
            with zipfile.ZipFile(target, 'w') as archive:
                archive.writestr('app.py', 'print("sample")')
                archive.writestr('collections/alice/archive.sqlite', b'private-data')
            result = scan(target)
            self.assertEqual(len(result['files']), 2)
            self.assertEqual(result['findings'][0]['kind'], 'private_or_generated_path')


if __name__ == '__main__':
    unittest.main()
