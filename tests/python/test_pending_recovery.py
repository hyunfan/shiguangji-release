"""Only actionable unfinished checks should keep an automatic task alive."""
import tempfile
import unittest
from pathlib import Path
from archive import Store, export_collection
from extractor import blank_post, media_item


class PendingRecoveryTests(unittest.TestCase):
    def test_counts_exclude_saved_unsupported_completed_and_plain_404_items(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'alice', 'alice')
            try:
                post = blank_post('1500000000000000000', 'alice')
                post['media'] = []
                for i, extra in enumerate([
                    {'cc_more': True, 'cc_remaining_indexes': ['CC-MAIN-2024-10']},
                    {'cc_pending_indexes': ['CC-MAIN-2024-10']},
                    {'status': 'saved', 'cc_more': True, 'cc_remaining_indexes': ['CC-MAIN-2024-10'], 'recovery_error_kind': 'network'},
                    {'kind': 'hls', 'status': 'link_only', 'error_kind': 'unsupported_hls', 'cc_pending_indexes': ['CC-MAIN-2024-10'], 'recovery_error_kind': 'network'},
                    {'cc_more': False, 'cc_remaining_indexes': ['CC-MAIN-2024-10']},
                    {'error_kind': 'not_found'},
                    {'error_kind': 'network'},
                    {'recovery_error_kind': 'network'},
                ]):
                    item = media_item(f'https://pbs.twimg.com/media/pending{i}.jpg')
                    item.update(extra)
                    post['media'].append(item)
                store.save_post(post)
                counts = export_collection(store, 'partial', [], full=False)['counts']
                self.assertEqual(counts['cc_pending'], 2)
                self.assertEqual(counts['media_retry_pending'], 1)
                self.assertEqual(counts['source_retry_pending'], 2)
            finally:
                store.close()
