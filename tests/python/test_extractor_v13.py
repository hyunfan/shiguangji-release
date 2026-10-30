"""Parser regressions use invented tweets and never contact X or Wayback."""
import json
import unittest

from shiguangji.platforms.extractor import blank_post, extract, media_item, merge_post


PID = "1346889436626259968"
URL = f"https://twitter.com/alice/status/{PID}"
TS = "20210201000000"
IMAGE = "https://pbs.twimg.com/media/fixture.jpg"


def parse(source, url=URL):
    posts = extract(source if isinstance(source, str) else json.dumps(source), url, TS, "alice")
    return next(post for post in posts if post["id"] == PID)


class ExtractorV13Tests(unittest.TestCase):
    def test_extended_tweet_preserves_full_text_and_its_media(self):
        p = parse({"id_str": PID, "user": {"screen_name": "alice"},
                   "text": "A &amp; B…", "entities": {"media": []},
                   "extended_tweet": {"full_text": "A &amp; B: the complete long tweet.",
                                      "extended_entities": {"media": [{"media_url_https": IMAGE}]}}})
        self.assertEqual(p["text"], "A & B: the complete long tweet.")
        self.assertEqual(p["text_source"], "extended_tweet")
        self.assertEqual(p["text_versions"], ["A & B…"])
        self.assertEqual(len(p["media"]), 1)

    def test_note_tweet_without_legacy_preview_is_supported(self):
        p = parse({"rest_id": PID, "legacy": {},
                   "core": {"user_results": {"result": {"legacy": {"screen_name": "alice"}}}},
                   "note_tweet": {"note_tweet_results": {"result": {"text": "Long note &amp; more"}}}})
        self.assertEqual(p["text"], "Long note & more")
        self.assertEqual(p["text_source"], "note_tweet")

    def test_note_tweet_stays_scoped_to_verified_author(self):
        p = parse({"rest_id": PID, "legacy": {"full_text": "Bob's preview"},
                   "core": {"user_results": {"result": {"legacy": {"screen_name": "bob"}}}},
                   "note_tweet": {"note_tweet_results": {"result": {"text": "Bob's long note"}}}})
        self.assertEqual(p["text"], "")

    def test_embedded_versions_survive_database_style_merge_and_deduplicate(self):
        old = blank_post(PID, "alice")
        old.update(text="old", text_versions=["very old", "old"])
        page = blank_post(PID, "alice")
        page.update(text="new full text", text_versions=["preview", "very old", "new full text"])
        p = merge_post(old, page)
        self.assertEqual(p["text"], "new full text")
        self.assertEqual(set(p["text_versions"]), {"old", "very old", "preview"})
        self.assertEqual(merge_post(p, page)["text_versions"], p["text_versions"])

    def test_metadata_does_not_replace_actual_text_even_when_longer(self):
        source = f'''<meta property="og:description" content="Alice on Twitter: &quot;Real post&quot; with added site description">
        <article><a href="{URL}">time</a><div data-testid="tweetText">Real post</div></article>'''
        p = parse(source)
        self.assertEqual(p["text"], "Real post")
        self.assertEqual(p["text_source"], "html")
        self.assertIn('Alice on Twitter: "Real post" with added site description', p["text_versions"])

    def test_reliable_short_text_replaces_previous_metadata_with_version_kept(self):
        old = parse('<meta property="og:description" content="Alice on Twitter: Real post">')
        actual = parse(f'<article><a href="{URL}">time</a><div data-testid="tweetText">Real post</div></article>')
        p = merge_post(old, actual)
        self.assertEqual(p["text"], "Real post")
        self.assertIn("Alice on Twitter: Real post", p["text_versions"])

    def test_legacy_exact_quote_wrapper_migrates_without_losing_original(self):
        for prior, body, source in (("\"Complete body\"", "Complete body", "html"),
                                    ("  “完整正文”\n", "完整正文", "json"),
                                    ("\"Quoted \\\"word\\\" inside\"", "Quoted \\\"word\\\" inside", "html")):
            with self.subTest(prior=prior, source=source):
                old = blank_post(PID, "alice")
                old.pop("text_source")
                old["text"] = prior
                actual = blank_post(PID, "alice")
                actual.update(text=body, text_source=source)
                result = merge_post(old, actual)
                self.assertEqual(result["text"], body)
                self.assertEqual(result["text_source"], source)
                self.assertIn(prior, result["text_versions"])

    def test_legacy_migration_does_not_generalize_or_override_known_text(self):
        cases = (("Alice said: \"body\"", "body", "", "html"),
                 ("\"body plus more\"", "body", "", "html"),
                 ("\"body\u201d", "body", "", "html"),
                 ("'body'", "body", "", "html"),
                 ("\" body \"", "body", "", "html"),
                 ("\"body\"", "body", "html", "html"),
                 ("\"body\"", "body", "", "metadata"),
                 ("\"body\"", "body", "", ""))
        for prior, body, old_source, new_source in cases:
            with self.subTest(prior=prior, old_source=old_source, new_source=new_source):
                old = blank_post(PID, "alice")
                old.update(text=prior, text_source=old_source)
                actual = blank_post(PID, "alice")
                actual.update(text=body, text_source=new_source)
                result = merge_post(old, actual)
                self.assertEqual(result["text"], prior)
                self.assertEqual(result["text_source"], old_source)
                self.assertIn(body, result["text_versions"])

    def test_foreign_status_metadata_is_not_attached_to_requested_post(self):
        p = parse(f'''<meta property="og:url" content="https://twitter.com/bob/status/999">
        <meta property="og:description" content="Foreign text"><meta property="og:image" content="{IMAGE}">''')
        self.assertEqual(p["text"], "")
        self.assertEqual(p["media"], [])

    def test_duplicate_image_metadata_is_preserved(self):
        p = parse(f'''<meta property="og:image" content="{IMAGE}">
        <meta property="og:image" content="https://pbs.twimg.com/media/second.jpg">''')
        self.assertEqual(len(p["media"]), 2)

    def test_unavailable_or_age_gate_metadata_is_not_recovered_as_a_tweet(self):
        for notice, reason in (("This Tweet is unavailable.", "unavailable"),
                               ("Age-restricted adult content", "restricted")):
            with self.subTest(notice=notice):
                p = parse(f'<meta property="og:description" content="{notice}"><body>{notice}</body>')
                self.assertEqual(p["text"], "")
                self.assertEqual(p["content_reason"], reason)

    def test_lazy_srcset_and_linked_original_media_with_quote_excluded(self):
        p = parse(f'''<article><a href="{URL}">time</a><div data-testid="tweetText">Photos</div>
        <img data-srcset="{IMAGE} 1x, https://pbs.twimg.com/media/second.jpg 2x">
        <a href="https://pbs.twimg.com/media/third.png">original</a>
        <div class="QuoteTweet"><img srcset="https://pbs.twimg.com/media/quoted.jpg 1x"></div></article>''')
        self.assertEqual({m["key"] for m in p["media"]}, {"pbs.twimg.com/media/fixture.jpg", "pbs.twimg.com/media/second.jpg", "pbs.twimg.com/media/third.png"})

    def test_quote_permalink_before_own_permalink_does_not_reassign_article(self):
        p = parse(f'''<article><div class="QuoteTweet"><a href="https://twitter.com/bob/status/99">quote</a>
        <div data-testid="tweetText">Bob's text</div></div>
        <a href="{URL}">Alice time</a><div data-testid="tweetText">Alice's text</div></article>''')
        self.assertEqual(p["text"], "Alice's text")

    def test_nested_article_container_attributes_are_not_parent_media(self):
        p = parse(f'''<article><a href="{URL}">Alice time</a><div data-testid="tweetText">Alice's text</div>
        <article data-tweet-id="99" data-screen-name="bob" data-image-url="{IMAGE}">
        <a href="https://twitter.com/bob/status/99">Bob time</a></article></article>''')
        self.assertEqual(p["media"], [])

    def test_malformed_urls_do_not_discard_other_valid_content(self):
        for value in ("https://pbs.twimg.com:bad/media/a.jpg", "https://[broken/media/a.jpg"):
            self.assertIsNone(media_item(value))
        p = parse(f'''<article data-tweet-id="{PID}" data-screen-name="alice">
        <a href="https://[broken/status/99">bad link</a><p class="tweet-text">Still readable</p>
        <img src="https://pbs.twimg.com:bad/media/bad.jpg"><img src="{IMAGE}"></article>''')
        self.assertEqual(p["text"], "Still readable")
        self.assertEqual(len(p["media"]), 1)

    def test_mixed_bitrate_values_do_not_discard_video_or_photo(self):
        variants = [{"url": f"https://video.twimg.com/ext_tw_video/123/pu/vid/{i}.mp4", "bitrate": rate}
                    for i, rate in enumerate((None, "832000", 256000, "invalid"))]
        p = parse({"id_str": PID, "user": {"screen_name": "alice"}, "text": "video",
                   "entities": {"media": [{"media_url_https": IMAGE}]},
                   "extended_entities": {"media": [{"video_info": {"variants": variants}}]}})
        self.assertEqual(len(p["media"]), 5)
        self.assertEqual([m["urls"][0] for m in p["media"] if m["kind"] == "video"],
                         [variants[i]["url"] for i in (1, 2, 0, 3)])


if __name__ == "__main__":
    unittest.main()
