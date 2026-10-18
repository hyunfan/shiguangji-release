"""Historical embed discovery fixtures; no network or real collection writes."""
import json
import unittest
from urllib.parse import parse_qs, quote, urlsplit

from archive import Cancelled
from archived_sources import ArchivedSources, CDX, decoded_media_urls, endpoint_kind, query_plans

# Synthetic future snowflakes; never copy identifiers from real collections.
PID = "9000000000000000001"
OTHER = "9000000000000000002"
VIDEO = "https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/good.mp4?tag=1"
WRONG = VIDEO.replace("/123/", "/999/")
PLAYER = f"https://twitter.com/i/videos/{PID}"
POST = {"id": PID, "username": "alice", "content_reason": ""}
TARGET = {"key": "video.twimg.com/ext_tw_video/123/", "kind": "video", "reference_only": True}
SYNDICATION = f"https://cdn.syndication.twimg.com/tweet-result?id={PID}"
NITTER = f"https://nitter.net/alice/status/{PID}"


def tweet(**kw):
    return {"id_str": PID, "user": {"screen_name": "alice"}, "mediaDetails": [{"video_info": {"variants": [{"url": VIDEO, "bitrate": 500}]}}], **kw}


class Reader:
    def __init__(self, indexes=None, bodies=None, final_urls=None):
        self.indexes, self.bodies, self.final_urls = indexes or {}, bodies or {}, final_urls or {}
        self.calls = []

    def __call__(self, url, limit):
        self.calls.append((url, limit))
        if url.startswith(CDX + "?"):
            query = parse_qs(urlsplit(url).query)["url"][0]
            answer = self.indexes.get(query, "")
            if isinstance(answer, Exception):raise answer
            return answer, {"url": self.final_urls.get(url, url)}
        answer = self.bodies.get(url)
        if isinstance(answer, Exception):raise answer
        if answer is None:raise AssertionError("Unexpected replay read: " + url)
        return answer, {"url": self.final_urls.get(url, url)}


def row(url, day=1):return f"202105{day:02d}000000 {url} application/json 200 digest{day}"
def replay(url, day=1):return f"https://web.archive.org/web/202105{day:02d}000000id_/{url}"


class ArchivedSourceTests(unittest.TestCase):
    def test_only_tracking_parameters_are_removed_from_observed_player(self):
        observed = PLAYER + "?embed_source=twitter&lang=en&utm_source=fixture&start=30"
        plans = query_plans(POST, [(observed, "local-cache:fixture.txt")])
        self.assertEqual(plans[0]["url"], PLAYER + "?lang=en&start=30")
        self.assertEqual(plans[1]["url"], observed)
        self.assertEqual(plans[0]["observed_at"], "local-cache:fixture.txt")
        self.assertFalse(any(p["kind"] == "player" for p in query_plans(POST)))

    def test_unrelated_endpoints_are_never_queried(self):
        for url in [PLAYER.replace(PID, OTHER), "https://evil.test/i/videos/" + PID,
                    "https://nitter.net/bob/status/" + PID, SYNDICATION + "&id=" + OTHER,
                    "https://user@twitter.com/i/videos/" + PID]:
            with self.subTest(url=url):
                self.assertEqual(endpoint_kind(url, PID, "alice"), "")
                self.assertNotIn(url, [p["url"] for p in query_plans(POST, [url])])

    def test_current_sources_are_never_called_and_player_media_identity_matches(self):
        reader = Reader({PLAYER: row(PLAYER)}, {replay(PLAYER): f'<video src="{VIDEO}"></video><video src="{WRONG}"></video>'})
        source = ArchivedSources(reader)
        found = source.discover(POST, TARGET, [PLAYER + "?embed_source=twitter"])
        self.assertEqual([x["url"] for x in found], [VIDEO])
        self.assertEqual(found[0]["evidence_url"], replay(PLAYER))
        self.assertEqual(found[0]["captured_at"], "20210501000000")
        self.assertTrue(all(urlsplit(url).hostname == "web.archive.org" for url, limit in reader.calls))
        self.assertIsInstance(source.last_report, list)
        self.assertEqual(source.summary["status"], "found")

    def test_cdx_prefix_cannot_leak_longer_post_ids(self):
        wrong = SYNDICATION.replace(PID, OTHER)
        reader = Reader({SYNDICATION: row(wrong)}, {})
        source = ArchivedSources(reader)
        self.assertEqual(source.discover(POST, TARGET), [])
        self.assertEqual(source.summary["captures_checked"], 0)

    def test_archived_syndication_requires_post_and_author_and_excludes_quotes(self):
        actual = SYNDICATION + "&lang=en&token=fixture"
        data = tweet(quoted_tweet=tweet(mediaDetails=[{"video_info": {"variants": [{"url": WRONG}]}}]))
        reader = Reader({SYNDICATION: row(actual)}, {replay(actual): json.dumps(data)})
        self.assertEqual([x["url"] for x in ArchivedSources(reader).discover(POST, TARGET)], [VIDEO])
        for replacement in ({"id_str": OTHER}, {"user": {"screen_name": "bob"}}, {"user": None}):
            data = tweet(**replacement)
            reader = Reader({SYNDICATION: row(actual)}, {replay(actual): json.dumps(data)})
            self.assertEqual(ArchivedSources(reader).discover(POST, TARGET), [])

    def test_historical_tweets_json_jsonp_extracts_only_target_object(self):
        batch = f"https://syndication.twitter.com/tweets.json?ids={PID}"
        payload = {"body": [{"id_str": OTHER, "user": {"screen_name": "bob"}, "mediaDetails": [{"video_info": {"variants": [{"url": WRONG}]}}]}, tweet()]}
        reader = Reader({batch: row(batch)}, {replay(batch): "__twttr.callbacks.cb0(" + json.dumps(payload) + ");"})
        self.assertEqual([x["url"] for x in ArchivedSources(reader).discover(POST, TARGET)], [VIDEO])

    def test_old_keyed_tweet_object_and_legacy_media_fields(self):
        batch = f"https://syndication.twitter.com/tweets.json?ids={PID}"
        payload = {"body": {PID: {"user": {"screen_name": "alice"}, "extended_entities": {
            "media": [{"video_info": {"variants": [{"url": VIDEO}]}}]}}}}
        reader = Reader({batch: row(batch)}, {replay(batch): json.dumps(payload)})
        self.assertEqual([x["url"] for x in ArchivedSources(reader).discover(POST, TARGET)], [VIDEO])
        payload["body"][PID]["id_str"] = OTHER
        reader = Reader({batch: row(batch)}, {replay(batch): json.dumps(payload)})
        self.assertEqual(ArchivedSources(reader).discover(POST, TARGET), [])

    def test_jsonp_is_parsed_without_executing_trailing_script(self):
        reader = Reader({SYNDICATION: row(SYNDICATION)}, {replay(SYNDICATION): "cb(" + json.dumps(tweet()) + ");doSomething();"})
        source = ArchivedSources(reader)
        self.assertEqual(source.discover(POST, TARGET), [])
        self.assertEqual(source.last_report[0]["error"], "invalid_metadata")

    def test_percent_encoded_media_from_metadata_is_decoded(self):
        encoded = quote(quote(VIDEO, safe=""), safe="")
        data = tweet(mediaDetails=[{"video_info": {"variants": [{"url": encoded}]}}])
        reader = Reader({SYNDICATION: row(SYNDICATION)}, {replay(SYNDICATION): json.dumps(data)})
        self.assertEqual([x["url"] for x in ArchivedSources(reader).discover(POST, TARGET)], [VIDEO])

    def test_nitter_requires_main_post_link_and_does_not_take_quote(self):
        body = f'''<div class="main-tweet"><a class="tweet-link" href="/alice/status/{PID}#m">post</a>
          <video src="/pic/orig/{quote(VIDEO, safe='')}"></video>
          <div class="quote"><video src="/pic/orig/{quote(WRONG, safe='')}"></video></div></div>
          <div class="timeline-item"><a href="/bob/status/999">reply</a><video src="{WRONG}"></video></div>'''
        reader = Reader({NITTER: row(NITTER)}, {replay(NITTER): body})
        result = ArchivedSources(reader).discover(POST, TARGET)
        self.assertEqual([x["url"] for x in result], [VIDEO])
        reader = Reader({NITTER: row(NITTER)}, {replay(NITTER): body.replace(f"/alice/status/{PID}", f"/bob/status/{PID}")})
        self.assertEqual(ArchivedSources(reader).discover(POST, TARGET), [])

    def test_resource_record_player_uses_identical_identity_checks(self):
        resource = row(PLAYER).replace('application/json 200', 'application/json -')
        reader = Reader({PLAYER: resource}, {replay(PLAYER): f'<video src="{VIDEO}"></video>'})
        found = ArchivedSources(reader).discover(POST, TARGET, [PLAYER])
        self.assertEqual([item['url'] for item in found], [VIDEO])
        self.assertEqual(parse_qs(urlsplit(reader.calls[0][0]).query)['filter'], ['statuscode:(200|-)'])

    def test_nitter_quote_does_not_supply_main_tweet_media(self):
        body = f'<div class="main-tweet"><a href="/alice/status/{PID}#m">post</a><div class="quote"><video src="{VIDEO}"></video></div></div>'
        reader = Reader({NITTER: row(NITTER)}, {replay(NITTER): body})
        self.assertEqual(ArchivedSources(reader).discover(POST, TARGET), [])

    def test_known_restricted_post_does_not_make_any_request(self):
        reader = Reader();post = {**POST, "content_reason": "restricted"}
        source = ArchivedSources(reader)
        self.assertEqual(source.discover(post, TARGET, [PLAYER]), [])
        self.assertEqual(reader.calls, [])
        self.assertEqual(source.summary["status"], "restricted")

    def test_archived_gate_stops_further_endpoints(self):
        reader = Reader({PLAYER: row(PLAYER)}, {replay(PLAYER): f'<p>Log in to view age-restricted content</p><video src="{VIDEO}">'})
        source = ArchivedSources(reader)
        self.assertEqual(source.discover(POST, TARGET, [PLAYER]), [])
        self.assertEqual(source.summary["status"], "restricted")
        self.assertEqual(len(source.last_report), 1)

    def test_capture_redirect_to_other_post_is_rejected(self):
        wrong = PLAYER.replace(PID, OTHER)
        reader = Reader({PLAYER: row(PLAYER)}, {replay(PLAYER): f'<video src="{VIDEO}">'}, {replay(PLAYER): replay(wrong)})
        source = ArchivedSources(reader)
        self.assertEqual(source.discover(POST, TARGET, [PLAYER]), [])
        self.assertEqual(source.last_report[0]["status"], "failed")

    def test_nearest_capture_records_actual_timestamp_and_evidence(self):
        reader = Reader({PLAYER: row(PLAYER)}, {replay(PLAYER): f'<video src="{VIDEO}"></video>'},
                        {replay(PLAYER): replay(PLAYER, 2)})
        found = ArchivedSources(reader).discover(POST, TARGET, [PLAYER])
        self.assertEqual(found[0]["captured_at"], "20210502000000")
        self.assertEqual(found[0]["evidence_url"], replay(PLAYER, 2))

    def test_global_query_and_per_query_snapshot_limits(self):
        rows = "\n".join(row(PLAYER, day) for day in range(1, 8))
        bodies = {replay(PLAYER, day): "<html>no media</html>" for day in range(1, 8)}
        reader = Reader({PLAYER: rows}, bodies)
        source = ArchivedSources(reader, max_queries=1, max_captures=2)
        self.assertEqual(source.discover(POST, TARGET, [PLAYER]), [])
        self.assertEqual(source.summary["requests"], 3)
        self.assertEqual(source.summary["captures_checked"], 2)
        self.assertFalse(source.summary["complete"])

    def test_cancel_is_not_swallowed(self):
        calls = []
        def check():
            calls.append(1)
            if len(calls) > 1:raise Cancelled()
        with self.assertRaises(Cancelled):ArchivedSources(Reader(), check).discover(POST, TARGET)

    def test_oversized_or_non_cdx_response_is_failed(self):
        for value in ("<html>gateway failure</html>", "x" * (300 * 1024)):
            source = ArchivedSources(Reader({SYNDICATION: value}), max_queries=1)
            self.assertEqual(source.discover(POST, TARGET), [])
            self.assertEqual(source.last_report[0]["status"], "failed")

    def test_unknown_media_id_and_unrelated_download_host_are_rejected(self):
        raw = f'<video src="{WRONG}"></video><video src="https://evil.test/ext_tw_video/123/a.mp4"></video>'
        source = ArchivedSources(Reader({PLAYER: row(PLAYER)}, {replay(PLAYER): raw}))
        self.assertEqual(source.discover(POST, TARGET, [PLAYER]), [])
        self.assertEqual(decoded_media_urls("https://video.twimg.com.evil.test/ext_tw_video/123/a.mp4"), [])


if __name__ == "__main__":unittest.main()
