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
