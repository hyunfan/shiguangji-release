"""API v2 resource-record regressions, using synthetic post bodies and URLs."""
import copy
import json
import unittest

from shiguangji.platforms.extractor import extract


class ApiV2ExtractionTests(unittest.TestCase):
    def fixture(self, text="A synthetic post"):
        return {
            "data": {"id": "123", "author_id": "10", "text": text,
                     "created_at": "2024-01-02T03:04:05.000Z",
                     "attachments": {"media_keys": ["3_100"]}},
            "includes": {
                "users": [{"id": "10", "username": "Example", "protected": False}],
                "media": [{"media_key": "3_100", "type": "photo",
                           "url": "https://pbs.twimg.com/media/SyntheticPhoto.jpg"}],
            },
        }

    def parse(self, payload, page="https://twitter.com/example/status/123"):
        return {p["id"]: p for p in extract(json.dumps(payload), page, "20250101000000", "example")}

    def test_v2_single_resource_joins_author_media_and_date(self):
        p = self.parse(self.fixture())["123"]
        self.assertEqual(p["text"], "A synthetic post")
        self.assertEqual(p["text_source"], "json")
        self.assertEqual(p["created_at"], "2024-01-02T03:04:05Z")
        self.assertEqual(p["date_source"], "page")
        self.assertEqual(len(p["media"]), 1)
        self.assertEqual(p["content_status"], "recovered")
        self.assertEqual(p["captures"][0]["timestamp"], "20250101000000")

    def test_long_note_preferred_and_original_text_retained(self):
        payload = self.fixture("Short &amp; truncated")
        payload["data"]["note_tweet"] = {"text": "The complete &amp; synthetic note", "entities": {}}
        p = self.parse(payload)["123"]
        self.assertEqual(p["text"], "The complete & synthetic note")
        self.assertEqual(p["text_source"], "note_tweet")
        self.assertIn("Short & truncated", p["text_versions"])

    def test_media_only_post_is_recovered(self):
        p = self.parse(self.fixture(""))["123"]
        self.assertEqual(p["text"], "")
        self.assertEqual(len(p["media"]), 1)

    def test_video_keeps_preview_and_all_encodings(self):
        payload = self.fixture()
        payload["includes"]["media"] = [{
            "media_key": "3_100", "type": "video",
            "preview_image_url": "https://pbs.twimg.com/ext_tw_video_thumb/100/pu/img/Synthetic.jpg",
            "variants": [
                {"bit_rate": 100, "content_type": "video/mp4", "url": "https://video.twimg.com/ext_tw_video/100/pu/vid/320x180/low.mp4"},
                {"bit_rate": 900, "content_type": "video/mp4", "url": "https://video.twimg.com/ext_tw_video/100/pu/vid/1280x720/high.mp4"},
                {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/ext_tw_video/100/pu/pl/list.m3u8"},
            ],
        }]
        media = self.parse(payload)["123"]["media"]
        self.assertEqual([m["kind"] for m in media], ["image", "video", "video", "hls"])
        self.assertIn("high.mp4", media[1]["urls"][0])

    def test_animated_gif_variants_and_invalid_urls(self):
        payload = self.fixture()
        payload["includes"]["media"] = [{"media_key": "3_100", "type": "animated_gif", "variants": [
            None, {"bit_rate": {}, "url": "https://video.twimg.com/tweet_video/Synthetic.mp4"},
            {"url": "https://example.invalid/foreign.mp4"}, {"url": {"invalid": True}},
        ]}]
        media = self.parse(payload)["123"]["media"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["kind"], "video")

    def test_quote_and_reply_expansions_never_attach_other_author(self):
        payload = self.fixture()
        payload["data"]["referenced_tweets"] = [{"type": "quoted", "id": "999"}, {"type": "replied_to", "id": "998"}]
        payload["includes"]["users"].insert(0, {"id": "20", "username": "Other"})
        payload["includes"]["tweets"] = [{"id": "999", "author_id": "20", "text": "Other body", "attachments": {"media_keys": ["3_200"]}}]
        payload["includes"]["media"].insert(0, {"media_key": "3_200", "type": "photo", "url": "https://pbs.twimg.com/media/Foreign.jpg"})
        posts = self.parse(payload)
        self.assertEqual(set(posts), {"123"})
        self.assertEqual(len(posts["123"]["media"]), 1)
        self.assertIn("SyntheticPhoto", posts["123"]["media"][0]["key"])

    def test_same_author_included_tweet_remains_separate(self):
        payload = self.fixture()
        payload["includes"]["tweets"] = [{"id": "124", "author_id": "10", "text": "Separate earlier post"}]
        posts = self.parse(payload)
        self.assertEqual(set(posts), {"123", "124"})
        self.assertEqual(posts["124"]["text"], "Separate earlier post")
        self.assertFalse(posts["124"]["media"])

    def test_retweet_does_not_take_source_attachments(self):
        payload = self.fixture("RT @other: synthetic repost text")
        payload["data"]["referenced_tweets"] = [{"type": "retweeted", "id": "999"}]
        p = self.parse(payload)["123"]
        self.assertTrue(p["text"])
        self.assertFalse(p["media"])

    def test_borrowed_media_source_does_not_take_source_attachments(self):
        payload = self.fixture()
        payload["data"]["attachments"]["media_source_tweet"] = "999"
        self.assertFalse(self.parse(payload)["123"]["media"])

    def test_profile_list_requires_each_author_and_attachment_match(self):
        payload = self.fixture()
        other = {"id": "999", "author_id": "20", "text": "Other post"}
        payload["data"] = [payload["data"], other]
        payload["includes"]["users"].append({"id": "20", "username": "other"})
        posts = self.parse(payload, "https://twitter.com/example")
        self.assertEqual(set(posts), {"123"})

    def test_missing_or_wrong_author_never_falls_back_to_page_url(self):
        for user in ([], [{"id": "20", "username": "example"}], [{"id": "10", "username": "other"}]):
            with self.subTest(user=user):
                payload = self.fixture()
                payload["includes"]["users"] = user
                p = self.parse(payload)["123"]
                self.assertFalse(p["text"])
                self.assertFalse(p["media"])

    def test_protected_withheld_and_ambiguous_authors_are_skipped(self):
        for mode in ("protected", "withheld", "conflicting"):
            with self.subTest(mode=mode):
                payload = self.fixture()
                if mode == "conflicting":
                    payload["includes"]["users"].append({"id": "10", "username": "other"})
                else:
                    payload["includes"]["users"][0][mode] = True
                self.assertFalse(self.parse(payload)["123"]["text"])
        payload = self.fixture()
        payload["data"]["withheld"] = {"country_codes": ["XX"]}
        self.assertFalse(self.parse(payload)["123"]["text"])

    def test_media_requires_explicit_key_and_unambiguous_expansion(self):
        for mode in ("missing_key", "absent_expansion", "conflicting"):
            with self.subTest(mode=mode):
                payload = self.fixture()
                if mode == "missing_key":
                    payload["data"]["attachments"] = {}
                elif mode == "absent_expansion":
                    payload["includes"]["media"] = []
                else:
                    payload["includes"]["media"].append({"media_key": "3_100", "type": "photo", "url": "https://pbs.twimg.com/media/Different.jpg"})
                self.assertFalse(self.parse(payload)["123"]["media"])

    def test_separate_envelopes_do_not_share_author_expansions(self):
        first = self.fixture()
        first["includes"]["users"] = []
        second = copy.deepcopy(first)
        second["data"]["id"] = "456"
        second["data"]["author_id"] = "20"
        second["includes"]["users"] = [{"id": "10", "username": "example"}, {"id": "20", "username": "other"}]
        self.assertFalse(self.parse([first, second])["123"]["text"])


if __name__ == "__main__":
    unittest.main()
