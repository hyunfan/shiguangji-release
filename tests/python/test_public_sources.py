import json
import unittest
from urllib.error import HTTPError

from public_sources import discover, safe_media, syndication_token

PID = "1467033705218666496"
IMAGE = "https://pbs.twimg.com/media/test.jpg"
VIDEO = "https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/test.mp4"


class PublicSourcesTests(unittest.TestCase):
    def test_renamed_author_matches_numeric_id_and_never_only_name_similarity(self):
        post={**self.post(), 'author_ids':['123']}
        for author, expected in [({'screen_name':'newalice','id_str':'123'},True),
                                 ({'screen_name':'alice','id_str':'999'},False),
                                 ({'screen_name':'newalice'},False)]:
            with self.subTest(author=author):
                payload=self.tweet(user=author,photos=[{'url':IMAGE}])
                items,_,_,_=self.run_discovery(payload,post=post)
                self.assertEqual(bool(items),expected)

    def test_vx_mirror_recovers_exact_post_media_without_quote_tree(self):
        def payload(url):
            if 'api.vxtwitter.com' not in url:return {}
            return {'tweetID':PID,'user_screen_name':'Alice','tweetURL':f'https://twitter.com/alice/status/{PID}',
                    'media_extended':[{'url':VIDEO,'type':'video','thumbnail_url':IMAGE}],
                    'mediaURLs':[VIDEO],'qrt':{'mediaURLs':['https://pbs.twimg.com/media/quote.jpg']}}
        result,post,calls,_=self.run_discovery(payload,mirror=True)
        self.assertEqual({m['urls'][0] for m in result},{VIDEO,IMAGE})
        self.assertTrue(all(m['source']=='vxtwitter' for m in result))
        self.assertEqual(post['source_checks']['vxtwitter']['status'],'found')
        self.assertEqual(len(calls),3)

    def test_vx_response_cannot_inherit_identity_from_request_or_quote(self):
        for handle,ident in [('bob',PID),('alice','999'),('',PID)]:
            def payload(url):
                return {'tweetID':ident,'user_screen_name':handle,'mediaURLs':[VIDEO]} if 'api.vxtwitter.com' in url else {}
            result,post,_,_=self.run_discovery(payload,mirror=True)
            self.assertFalse(result)
            self.assertEqual(post['source_checks']['vxtwitter']['status'],'identity_mismatch')

    def post(self):
        return {"id": PID, "username": "alice", "content_reason": ""}

    def run_discovery(self, data, *, mirror=False, post=None):
        calls, logs = [], []
        def fetch(url, limit, headers=None):
            calls.append(url)
            value = data(url) if callable(data) else data
            return json.dumps(value).encode(), {"url": url, "content_type": "application/json"}
        p = post or self.post()
        result = discover(p, fetch, lambda: None, logs.append, mirror)
        return result, p, calls, logs

    def tweet(self, **values):
        return {"id_str": PID, "user": {"screen_name": "Alice"}, **values}

    def test_tokens_match_javascript_embed_calculation(self):
        for pid, token in [(PID, "3ktlpqim2i"), ("1492791879452094465", "3m9qs85db1p"),
                           ("20", "6dq1a2xwd93"), ("2057610994122166373", "4zk6bk5el")]:
            self.assertEqual(syndication_token(pid), token)

    def test_exact_owner_media_with_provenance_no_quote_or_parent(self):
        data = self.tweet(photos=[{"url": IMAGE}], quoted_tweet={"photos": [{"url": "https://pbs.twimg.com/media/quoted.jpg"}]},
                          parent={"photos": [{"url": "https://pbs.twimg.com/media/parent.jpg"}]})
        result, post, calls, logs = self.run_discovery(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["key"], "pbs.twimg.com/media/test.jpg")
        self.assertEqual(result[0]["source"], "syndication")
        self.assertEqual(result[0]["evidence_url"], calls[0])
        self.assertTrue(result[0]["discovered_at"])
        self.assertEqual(post["source_checks"]["syndication"]["status"], "found")

    def test_identity_is_mandatory_not_inferred_from_requested_url(self):
        for data in (self.tweet(id_str="999"), self.tweet(user={"screen_name": "bob"}), self.tweet(user={}), {"photos": [{"url": IMAGE}]}):
            with self.subTest(data=data):
                result, p, _, _ = self.run_discovery(data)
                self.assertEqual(result, [])
                self.assertEqual(p["source_checks"]["syndication"]["status"], "identity_mismatch")

    def test_duplicate_encodings_are_fallbacks_for_one_video(self):
        low = VIDEO.replace("640x360", "320x180")
        result, _, _, _ = self.run_discovery(self.tweet(mediaDetails=[{"media_url_https": IMAGE,
            "video_info": {"variants": [{"url": low, "bitrate": 100}, {"url": VIDEO, "bitrate": 500},
                {"url": VIDEO.replace(".mp4", ".m3u8"), "content_type": "application/x-mpegURL"}]}}]))
        self.assertEqual(len(result), 2)
        video = next(x for x in result if x["kind"] == "video")
        self.assertEqual(video["urls"], [VIDEO, low])

    def test_partial_media_details_do_not_hide_top_level_video(self):
        result, _, _, _ = self.run_discovery(self.tweet(
            mediaDetails=[{"type": "photo", "media_url_https": IMAGE}],
            video={"poster": IMAGE, "variants": [{"type": "video/mp4", "src": VIDEO}]}))
        self.assertEqual({m["kind"] for m in result}, {"image", "video"})
        self.assertEqual(len(result), 2)

    def test_overlapping_video_representations_merge_encodings_not_other_ids(self):
        low = VIDEO.replace("640x360", "320x180")
        other = VIDEO.replace("/123/", "/456/")
        result, _, _, _ = self.run_discovery(self.tweet(mediaDetails=[
            {"video_info": {"variants": [{"url": VIDEO, "bitrate": 500}]}},
            {"video_info": {"variants": [{"url": other, "bitrate": 500}]}}],
            video={"variants": [{"src": low}]}))
        videos = [m for m in result if m["kind"] == "video"]
        self.assertEqual(len(videos), 2)
        self.assertEqual(next(m for m in videos if "/123/" in m["key"])["urls"], [VIDEO, low])
        self.assertEqual(next(m for m in videos if "/456/" in m["key"])["urls"], [other])

    def test_stale_url_handle_is_accepted_only_after_exact_response_author_match(self):
        values = {"url": f"https://twitter.com/old_handle/status/{PID}",
                  "mediaDetails": [{"media_url_https": IMAGE, "expanded_url": f"https://x.com/old_handle/status/{PID}/photo/1"}]}
        result, _, _, _ = self.run_discovery(self.tweet(**values))
        self.assertEqual(len(result), 1)
        self.assertEqual(self.run_discovery(self.tweet(user={"screen_name": "other"}, **values))[0], [])
        values["url"] = "https://x.com/alice/status/999"
        self.assertEqual(self.run_discovery(self.tweet(**values))[0], [])

    def test_foreign_media_and_unavailable_media_are_rejected(self):
        entries = [{"url": IMAGE, "source_status_id_str": "9"},
                   {"url": IMAGE, "expanded_url": "https://x.com/bob/status/9/photo/1"},
                   {"url": IMAGE, "ext_media_availability": {"status": "unavailable"}}]
        self.assertEqual(self.run_discovery(self.tweet(mediaDetails=entries))[0], [])

    def test_mirror_is_opt_in_and_requires_author_and_id(self):
        mirror = {"code": 200, "tweet": {"id": PID, "author": {"screen_name": "alice"},
                  "media": {"videos": [{"url": VIDEO, "thumbnail_url": IMAGE}]},
                  "quote": {"media": {"photos": [{"url": "https://pbs.twimg.com/media/other.jpg"}]}}}}
        data = lambda url: {} if "syndication" in url else mirror
        self.assertEqual(len(self.run_discovery(data)[2]), 1)
        result, _, calls, _ = self.run_discovery(data, mirror=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(m["source"] == "fxtwitter" for m in result))
        mirror["tweet"]["author"]["screen_name"] = "bob"
        self.assertEqual(self.run_discovery(data, mirror=True)[0], [])

    def test_restricted_posts_skip_all_sources(self):
        p = self.post();p["content_reason"] = "restricted"
        result, p, calls, _ = self.run_discovery({}, mirror=True, post=p)
        self.assertEqual((result, calls), ([], []))
        self.assertTrue(all(r["status"] == "restricted" for r in p["source_checks"].values()))

    def test_returned_age_gate_does_not_fall_through_to_mirror(self):
        data = {"__typename": "TweetTombstone", "tombstone": {"text": {"text": "Log in to view age-restricted content"}}}
        result, p, calls, _ = self.run_discovery(data, mirror=True)
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(p["source_checks"]["fxtwitter"]["status"], "restricted")

    def test_deleted_tombstone_is_recorded_and_mirror_can_be_checked(self):
        result, p, calls, _ = self.run_discovery({"__typename": "TweetTombstone", "tombstone": {"text": "Deleted"}}, mirror=True)
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(p["source_checks"]["syndication"]["status"], "not_found")

    def test_safe_media_rejects_external_or_malformed_links(self):
        for url in ["http://pbs.twimg.com/media/a.jpg", "https://evil.test/media/a.jpg", "https://pbs.twimg.com.evil.test/media/a.jpg",
                    "https://user@pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com:999/media/a.jpg", "https://pbs.twimg.com/foo/media/a.jpg",
                    "https://pbs.twimg.com/media/../private/a.jpg", "https://pbs.twimg.com/media/%2e%2e/a.jpg", "https://pbs.twimg.com/media/a.jpg#x",
                    "https://pbs.twimg.com/profile_images/a.jpg", "https://pbs.twimg.com/media/a.jpg\n"]:
            with self.subTest(url=url):self.assertIsNone(safe_media(url))
        self.assertIsNotNone(safe_media(IMAGE))

    def test_network_status_is_recorded_not_fabricated_as_recovered(self):
        def fetch(*args, **kwargs):raise HTTPError(args[0], 429, "rate limit", {}, None)
        p = self.post()
        self.assertEqual(discover(p, fetch, lambda: None, lambda x: None), [])
        self.assertEqual(p["source_checks"]["syndication"]["status"], "rate_limited")

    def test_unexpected_redirect_and_non_json_are_rejected(self):
        for meta in ({"url": "https://evil.test/tweet-result"}, {"content_type": "text/html"}):
            p = self.post()
            fetch = lambda *a, **k: (json.dumps(self.tweet(photos=[{"url": IMAGE}])).encode(), meta)
            self.assertEqual(discover(p, fetch, lambda: None, lambda x: None), [])
            self.assertEqual(p["source_checks"]["syndication"]["status"], "unavailable")

    def test_cancel_not_swallowed_by_source_error_handler(self):
        class Cancelled(Exception):pass
        def fetch(*a, **k):raise Cancelled()
        with self.assertRaises(Cancelled):discover(self.post(), fetch, lambda: None, lambda x: None)

    def test_corrupt_metadata_and_missing_ids_cannot_add_media(self):
        for body in (b"<html>error</html>", b"[]", b"{}", b"x" * (2 * 1024 * 1024 + 1)):
            self.assertEqual(discover(self.post(), lambda *a, **k: (body, {}), lambda: None, lambda x: None), [])


if __name__ == "__main__":
    unittest.main()
