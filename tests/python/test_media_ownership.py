"""Invented source payloads: an author's post must not inherit foreign media."""
import json
import unittest

from shiguangji.platforms.extractor import extract

PID = '1346889436626259968'
OTHER = '1346889436626259999'
URL = f'https://twitter.com/alice/status/{PID}'
IMAGE = 'https://pbs.twimg.com/media/own.jpg'


def parse(payload):
    return next(p for p in extract(json.dumps(payload), URL, '20210201000000', 'alice') if p['id'] == PID)


def legacy(entry):
    return {'id_str': PID, 'user': {'screen_name': 'alice'}, 'full_text': 'My post',
            'extended_entities': {'media': [dict(media_url_https=IMAGE, **entry)]}}


class MediaOwnershipTests(unittest.TestCase):
    def test_foreign_source_ids_and_expanded_urls_are_rejected(self):
        for entry in ({'source_status_id_str': OTHER}, {'source_tweet_id': OTHER},
                      {'expanded_url': f'https://x.com/bob/status/{OTHER}/photo/1'}):
            with self.subTest(entry=entry):
                post = parse(legacy(entry))
                self.assertEqual(post['text'], 'My post')
                self.assertEqual(post['media'], [])

    def test_retweet_repeated_root_media_is_not_owned(self):
        for key, value in (('retweeted_status_id_str', OTHER),
                           ('retweeted_status', {'id_str': OTHER}),
                           ('retweeted_status_result', {'result': {'rest_id': OTHER}})):
            with self.subTest(key=key):
                payload = legacy({})
                payload[key] = value
                self.assertEqual(parse(payload)['media'], [])

    def test_quote_with_own_attachment_and_old_handle_is_preserved(self):
        payload = legacy({'source_status_id_str': PID,
                          'expanded_url': f'https://x.com/oldalice/status/{PID}/photo/1'})
        payload['quoted_status_id_str'] = OTHER
        self.assertEqual(len(parse(payload)['media']), 1)

    def test_unavailable_media_is_not_downloaded(self):
        self.assertEqual(parse(legacy({'ext_media_availability': {'status': 'Unavailable'}}))['media'], [])

    def test_v2_foreign_media_is_rejected_even_with_a_matching_key(self):
        payload = {'data': {'id': PID, 'author_id': '123', 'text': 'My post',
                            'attachments': {'media_keys': ['3_123']}},
                   'includes': {'users': [{'id': '123', 'username': 'alice'}],
                                'media': [{'media_key': '3_123', 'type': 'photo', 'url': IMAGE,
                                           'source_status_id_str': OTHER}]}}
        self.assertEqual(parse(payload)['media'], [])
