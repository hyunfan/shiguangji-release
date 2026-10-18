"""Conservative grouping, legacy cache refresh and export parity; no network."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from collection_export import publish_manifest
from collection_reader import _documents, _summaries, fingerprint, read_summary
from media_inventory import inventory


def video(asset='123', size='640x360', saved=False, **extra):
    url = f'https://video.twimg.com/ext_tw_video/{asset}/pu/vid/{size}/sample.mp4'
    return dict(kind='video', key=url[8:], urls=[url], status='saved' if saved else 'pending',
                path='media/' + asset + '-' + size + '.mp4' if saved else '', **extra)


def count(*media):
    return inventory([{'id': '1', 'media': list(media)}])


class InventoryTests(unittest.TestCase):
    def test_saved_rendition_means_video_is_saved_despite_missing_hls_and_hd(self):
        low, high, hls = video(saved=True), video(size='1280x720'), video(size='hls')
        hls.update(kind='hls', status='link_only')
        result = count(low, high, hls)
        self.assertEqual((result['video_groups'], result['video_saved'], result['video_missing']), (1, 1, 0))
        self.assertEqual(result['variant_records'], 3)
        self.assertEqual(result['groups_with_missing_variants'], 1)

    def test_cover_and_playlist_are_not_saved_video(self):
        cover = dict(kind='image', key='pbs.twimg.com/ext_tw_video_thumb/123/pu/img/a.jpg',
                     status='saved', path='media/a.jpg')
        hls = video(saved=True); hls['kind'] = 'hls'
        result = count(cover, hls)
        self.assertEqual((result['cover_saved'], result['image_saved'], result['video_saved']), (1, 0, 0))
        self.assertEqual(result['video_missing'], 1)

    def test_different_posts_and_distinct_ids_remain_separate(self):
        result = inventory([{'id': '1', 'media': [video(), video('456')]}, {'id': '2', 'media': [video()]}])
        self.assertEqual(result['video_groups'], 3)

    def test_conflicting_id_is_not_merged_even_if_one_candidate_is_saved(self):
        bad = video(saved=True); bad['source_url'] = video('456')['urls'][0]
        result = count(bad, video())
        self.assertEqual((result['video_groups'], result['video_missing'], result['video_identity_conflicts']), (2, 1, 1))

    def test_unknown_ids_do_not_merge_by_name(self):
        one = dict(kind='video', key='unknown', status='pending', urls=['https://example.com/a.mp4'])
        result = count(one, deepcopy(one))
        self.assertEqual((result['video_groups'], result['video_unidentified']), (2, 2))

    def test_archive_url_and_reference_only_key_join_with_full_rendition(self):
        reference = dict(kind='video', key='video.twimg.com/ext_tw_video/123/', status='pending')
        archived = video(saved=True)
        archived['urls'] = ['https://web.archive.org/web/20210101000000id_/' + archived['urls'][0]]
        result = count(reference, archived)
        self.assertEqual((result['video_groups'], result['video_saved']), (1, 1))

    def test_saved_without_usable_path_is_not_counted_as_saved(self):
        for path in ('', '../media/a.mp4', 'media/../a.mp4', 'media\\a.mp4'):
            with self.subTest(path=path):
                item = video(saved=True); item['path'] = path
                result = count(item)
                self.assertEqual((result['video_saved'], result['invalid_saved_records']), (0, 1))

    def test_exact_file_duplicates_require_valid_digest_and_size(self):
        one = video(saved=True, sha256='a'*64, bytes=512)
        two = video(size='1280x720', saved=True, sha256='A'*64, bytes=512)
        three = video('456', saved=True, sha256='a'*64, bytes=1024)
        result = count(one, two, three, video(saved=True, sha256='broken', bytes=512))
        self.assertEqual(result['duplicate_saved_records'], 1)

    def test_does_not_modify_input_or_infer_image_identity_from_filename(self):
        posts = [{'media': [video(saved=True), dict(kind='image', key='pbs.twimg.com/media/photo.jpg', status='pending')]}]
        before = deepcopy(posts)
        self.assertEqual(inventory(posts)['image_records'], 1)
        self.assertEqual(posts, before)

    def test_old_summary_cache_is_refreshed_without_writing_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            manifest = {'username': 'alice', 'posts': [{'media': [video(saved=True), video(size='1280x720')]}]}
            (folder/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (folder/'summary.json').write_text(json.dumps({'schema': 1, 'manifest_stat': fingerprint(folder/'manifest.json'),
                                                          'summary': {'content_revision': 'stale', 'posts': []}}), encoding='utf-8')
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            result = read_summary(folder)
            self.assertEqual(result['media_inventory']['video_saved'], 1)
            self.assertEqual(result['posts'], [])
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_new_export_and_cached_online_summary_agree(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            manifest = {'username': 'alice', 'posts': [{'media': [video(saved=True), video(size='1280x720')]}]}
            publish_manifest(folder, manifest)
            _summaries.clear(); _documents.clear()
            saved = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(read_summary(folder)['media_inventory'], saved['media_inventory'])
            self.assertIn('media_inventory', (folder/'data.js').read_text(encoding='utf-8'))
            self.assertIn('id="media-inventory"', (folder/'index.html').read_text(encoding='utf-8'))
