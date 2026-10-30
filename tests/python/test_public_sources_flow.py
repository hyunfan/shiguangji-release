"""Characterization tests for public_sources.discover branches not pinned elsewhere."""
import json
import unittest

from shiguangji.sources.public_sources import discover

PID = '1467033705218666496'
IMAGE = 'https://pbs.twimg.com/media/test.jpg'
OTHER = 'https://pbs.twimg.com/media/other.jpg'
VIDEO = 'https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/test.mp4'


def fx(**tweet):
    return {'code': 200, 'tweet': {'id': PID, 'author': {'screen_name': 'alice'}, **tweet}}


def syndication(**values):
    return {'id_str': PID, 'user': {'screen_name': 'Alice'}, **values}


class PublicDiscoverFlowTests(unittest.TestCase):
    def run_discovery(self, data, *, mirror=True, post=None, recover_text=False):
        calls, logs = [], []

        def fetch(url, limit, headers=None):
            calls.append((url, limit, headers))
            value = data(url) if callable(data) else data
            if isinstance(value, Exception):
                raise value
            return json.dumps(value).encode(), {'url': url, 'content_type': 'application/json'}
        p = post or {'id': PID, 'username': 'alice', 'content_reason': ''}
        result = discover(p, fetch, lambda: None, logs.append, mirror, recover_text)
        return result, p, calls, logs

    @staticmethod
    def by_source(syn=None, fxt=None, vxt=None):
        def answer(url):
            if 'syndication' in url:
                return syn if syn is not None else {}
            if 'fxtwitter' in url:
                return fxt if fxt is not None else {}
            return vxt if vxt is not None else {}
        return answer

    def test_requires_exact_numeric_id_and_handle(self):
        for post in ({'id': 'abc', 'username': 'alice'}, {'id': PID, 'username': 'a-b'}, {'id': PID}):
            with self.subTest(post=post), self.assertRaises(ValueError):
                discover(post, None, lambda: None, lambda _: None)

    def test_restricted_mirror_tweet_and_author_stop_later_sources(self):
        for tweet in ({'protected': True}, {'author': {'screen_name': 'alice', 'protected': True}},
                      {'message': 'login_required'}, {'requires_login': True}):
            with self.subTest(tweet=tweet):
                result, p, calls, _ = self.run_discovery(self.by_source(fxt=fx(**tweet), vxt={'tweetID': PID}))
                self.assertEqual(result, [])
                self.assertEqual(p['source_checks']['fxtwitter']['status'], 'restricted')
                self.assertEqual(p['source_checks']['vxtwitter']['status'], 'restricted')
                self.assertEqual(len(calls), 2)

    def test_mirror_api_errors_are_named_without_response_text(self):
        result, p, _, logs = self.run_discovery(self.by_source(fxt={**fx(), 'code': 500}, vxt={'error': 'secret body'}))
        self.assertEqual(result, [])
        for source in ('fxtwitter', 'vxtwitter'):
            self.assertEqual((p['source_checks'][source]['status'], p['source_checks'][source]['error']),
                             ('unavailable', 'mirror_api_error'))
        self.assertFalse(any('secret' in line for line in logs))
        self.assertEqual(len(logs), 3)

    def test_same_media_from_several_sources_keeps_first_provenance_and_merges_urls(self):
        syn = syndication(mediaDetails=[{'media_url_https': IMAGE, 'type': 'photo'}])
        fxt = fx(media={'photos': [{'url': IMAGE + ':orig'}, {'url': OTHER}],
                        'videos': [{'url': VIDEO, 'thumbnail_url': IMAGE}]})
        vxt = {'tweetID': PID, 'user_screen_name': 'alice', 'mediaURLs': [VIDEO]}
        result, p, calls, _ = self.run_discovery(self.by_source(syn, fxt, vxt))
        self.assertEqual([c[2] for c in calls], [{'Accept': 'application/json'}] * 3)
        self.assertEqual([(m['source'], m['kind']) for m in result],
                         [('syndication', 'image'), ('fxtwitter', 'image'), ('fxtwitter', 'video')])
        image, other, video = result
        self.assertEqual(image['urls'][0], IMAGE)
        self.assertIn(IMAGE + ':orig', image['urls'])
        self.assertEqual(other['evidence_url'], f'https://api.fxtwitter.com/alice/status/{PID}')
        self.assertIn(VIDEO, video['urls'])
        self.assertEqual({k: v['media'] for k, v in p['source_checks'].items()},
                         {'syndication': 1, 'fxtwitter': 3, 'vxtwitter': 1})
        self.assertEqual({k: v['status'] for k, v in p['source_checks'].items()},
                         {'syndication': 'found', 'fxtwitter': 'found', 'vxtwitter': 'found'})

    def test_text_recovery_stops_after_first_source_with_text(self):
        syn = syndication(text='   ')
        fxt = fx(text='  recovered words  ', media={'photos': [{'url': IMAGE}]})
        result, p, calls, _ = self.run_discovery(self.by_source(syn, fxt), recover_text=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(p['text'], 'recovered words')
        self.assertEqual((p['text_source'], p['content_status']), ('public_metadata', 'recovered'))
        provenance = dict(p['text_provenance'])
        provenance.pop('checked_at')
        self.assertEqual(provenance, {'source': 'fxtwitter', 'url': f'https://api.fxtwitter.com/alice/status/{PID}',
                                      'scope': 'current_public', 'post_id': PID, 'author_id': '', 'username': 'alice'})
        self.assertTrue(p['source_checks']['fxtwitter']['text_recovered'])
        self.assertNotIn('vxtwitter', p['source_checks'])
        self.assertEqual(len(result), 1)

    def test_existing_text_is_not_replaced(self):
        p = {'id': PID, 'username': 'alice', 'text': 'kept'}
        _, p, calls, _ = self.run_discovery(self.by_source(syndication(text='new')), post=p, recover_text=True)
        self.assertEqual(p['text'], 'kept')
        self.assertTrue(p['source_checks']['syndication']['text_recovered'])
        self.assertEqual(len(calls), 1)

    def test_http_errors_map_to_status_and_close_response(self):
        class Failure(Exception):
            def __init__(self, code):
                self.code, self.closed = code, False

            def close(self):
                self.closed = True
        errors = {'syndication': Failure(404), 'fxtwitter': Failure(429), 'vxtwitter': Failure('x')}
        answer = lambda url: next(v for k, v in errors.items() if k in url)
        _, p, _, _ = self.run_discovery(answer)
        self.assertEqual([(r['status'], r['error']) for r in p['source_checks'].values()],
                         [('not_found', 'HTTP 404'), ('rate_limited', 'HTTP 429'), ('unavailable', 'Failure')])
        self.assertTrue(all(e.closed for e in errors.values()))


if __name__ == '__main__':
    unittest.main()
