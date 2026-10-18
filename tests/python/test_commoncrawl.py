"""Offline Common Crawl source tests; no real account data or network access."""
import gzip
import json
import unittest
import zlib
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from commoncrawl_source import (COLLECTIONS_URL, CommonCrawlError, CommonCrawlSource,
                                INDEX_LIMIT, _inflate, _media_valid)
from extractor import media_item


IMAGE_URL = "https://pbs.twimg.com/media/Sample?format=jpg&name=large"
from test_media_fixtures import PNG as IMAGE
FILENAME = "crawl-data/CC-MAIN-2020-24/segments/1591234567.12/warc/CC-MAIN-sample.warc.gz"


def box(kind, data=b""):
    return (8 + len(data)).to_bytes(4, "big") + kind + data


from test_media_fixtures import VIDEO


def record(data=IMAGE, target=IMAGE_URL, extra_warc="", extra_http="", chunked=False,
           http_encoding="", http_status="200 OK", warc_type="response"):
    if http_encoding == "gzip":
        data = gzip.compress(data)
    elif http_encoding == "deflate":
        data = zlib.compress(data)
    if chunked:
        data = f"{len(data):x};fixture=yes\r\n".encode() + data + b"\r\n0\r\n\r\n"
        length_header = "Transfer-Encoding: chunked\r\n"
    else:
        length_header = f"Content-Length: {len(data)}\r\n"
    encoding_header = f"Content-Encoding: {http_encoding}\r\n" if http_encoding else ""
    response = (f"HTTP/1.1 {http_status}\r\n" + length_header + encoding_header + extra_http + "\r\n").encode() + data
    header = (f"WARC/1.0\r\nWARC-Type: {warc_type}\r\nWARC-Target-URI: {target}\r\n"
              f"Content-Length: {len(response)}\r\n" + extra_warc + "\r\n").encode()
    return gzip.compress(header + response + b"\r\n\r\n")


class FakeSource:
    def __init__(self, content=None, url=IMAGE_URL):
        self.content = content if content is not None else record()
        self.row = {"url": url, "timestamp": "20200615120000", "status": "200", "mime": "image/jpeg",
                    "filename": FILENAME, "offset": "100", "length": str(len(self.content))}
        self.collections = [{"id": "CC-MAIN-2026-39"}, {"id": "CC-MAIN-2026-34"},
                            {"id": "CC-MAIN-2020-29"}, {"id": "CC-MAIN-2020-24"},
                            {"id": "CC-MAIN-2020-16"}, {"id": "CC-MAIN-2019-51"},
                            {"id": "CC-MAIN-2018-05"}]
        self.calls = []
        self.rows = [self.row]
        self.extra_metadata = {}
        self.query_error = None

    def fetch(self, url, limit, headers=None):
        self.calls.append((url, limit, headers))
        if url == COLLECTIONS_URL:
            return json.dumps(self.collections).encode(), {"url": url}
        if "index.commoncrawl.org" in url:
            if self.query_error:
                raise self.query_error
            return b"\n".join(json.dumps(row).encode() for row in self.rows), {"url": url}
        self.assert_range = headers["Range"]
        return self.content, {"url": url, **self.extra_metadata}

    def source(self, **options):
        return CommonCrawlSource(self.fetch, **options)


def image_post():
    item = media_item(IMAGE_URL)
    return item, {"created_at": "2020-06-10T00:00:00Z", "captures": [], "media": [item]}


class CommonCrawlTests(unittest.TestCase):
    def test_recovers_exact_media_and_provenance_with_bounded_range(self):
        fake = FakeSource()
        item, post = image_post()
        source = fake.source()
        result = source.recover(item, post, 10000)
        self.assertEqual(result["data"], IMAGE)
        self.assertEqual(result["source"], "common_crawl")
        self.assertEqual(result["url"], IMAGE_URL)
        self.assertEqual(result["captured_at"], "20200615120000")
        self.assertTrue(result["retrieved_url"].startswith("https://data.commoncrawl.org/crawl-data/"))
        query = parse_qs(urlsplit(result["evidence_url"]).query)
        self.assertEqual(query["url"], ["pbs.twimg.com/media/Sample"])
        self.assertEqual(query["limit"], ["20"])
        self.assertEqual(fake.assert_range, f"bytes=100-{100 + len(fake.content) - 1}")
        self.assertEqual(fake.calls[-1][1], len(fake.content))
        self.assertEqual(source.last_scope, ["CC-MAIN-2020-24"])
        self.assertIsNone(source.recover(item, {"media": []}, 10000))
        self.assertEqual(source.last_scope, [])

    def test_missing_index_404_is_empty_and_cached_per_instance(self):
        fake = FakeSource()
        fake.query_error = HTTPError("https://index.commoncrawl.org/test", 404, "No captures", {}, None)
        item, post = image_post()
        source = fake.source()
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertEqual(len(fake.calls), 7)
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertEqual(len(fake.calls), 8)
        self.assertEqual(len(item["cc_completed_indexes"]), 7)
        self.assertFalse(item["cc_more"])

    def test_wrapped_404_empty_but_network_errors_propagate(self):
        fake = FakeSource()
        item, post = image_post()
        fake.query_error = RuntimeError("HTTP Error 404: NOT FOUND")
        self.assertIsNone(fake.source().recover(item, post, 10000))
        fake.query_error = RuntimeError("HTTP Error 503: unavailable")
        with self.assertRaisesRegex(RuntimeError, "503"):
            fake.source().recover(item, post, 10000)

    def test_scope_uses_nearby_and_latest_collections_not_entire_catalog(self):
        fake = FakeSource()
        fake.rows = []
        item, post = image_post()
        logs = []
        source = fake.source(log=logs.append)
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertEqual(len(source.last_scope), 6)
        self.assertEqual(source.last_scope[0], "CC-MAIN-2020-24")
        self.assertIn("CC-MAIN-2026-39", source.last_scope)
        self.assertIn("CC-MAIN-2026-34", source.last_scope)
        self.assertNotIn("CC-MAIN-2018-05", source.last_scope)
        self.assertIn("不代表全部历史存档", logs[0])

    def test_catalog_cannot_supply_external_endpoint_or_duplicate_ids(self):
        fake = FakeSource()
        fake.rows = []
        fake.collections = [{"id": "CC-MAIN-2020-24", "cdx-api": "https://evil.invalid/"},
                            {"id": "CC-MAIN-2020-24"}, {"id": "../../evil"}]
        item, post = image_post()
        fake.source().recover(item, post, 10000)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(fake.calls[-1][0].startswith("https://index.commoncrawl.org/CC-MAIN-2020-24-index?"))

    def test_unknown_item_or_unrelated_index_urls_are_never_downloaded(self):
        fake = FakeSource()
        item, post = image_post()
        self.assertIsNone(fake.source().recover(item, {"media": []}, 10000))
        self.assertEqual(fake.calls, [])
        fake.row["url"] = "https://pbs.twimg.com/media/SampleOther.jpg"
        self.assertIsNone(fake.source().recover(item, post, 10000))
        self.assertFalse(any("data.commoncrawl.org" in u for u, _, _ in fake.calls))

    def test_warc_target_mismatch_or_http_error_cannot_restore_media(self):
        for options in ({"target": "https://pbs.twimg.com/media/Other.jpg"}, {"http_status": "403 Forbidden"},
                        {"extra_warc": "WARC-Truncated: length\r\n"}, {"warc_type": "revisit"},
                        {"extra_http": "Content-Range: bytes 0-9/100\r\n"}):
            with self.subTest(options=options):
                fake = FakeSource(record(**options))
                item, post = image_post()
                with self.assertRaises(CommonCrawlError):
                    fake.source().recover(item, post, 10000)
                self.assertEqual(item["cc_completed_indexes"], [])

    def test_gzip_and_deflate_http_content_and_chunked_body(self):
        for encoding in ("", "gzip", "deflate"):
            for chunked in (False, True):
                with self.subTest(encoding=encoding, chunked=chunked):
                    fake = FakeSource(record(http_encoding=encoding, chunked=chunked))
                    item, post = image_post()
                    self.assertEqual(fake.source().recover(item, post, 10000)["data"], IMAGE)

    def test_missing_chunk_terminator_is_rejected(self):
        raw = gzip.decompress(record(chunked=True))
        broken = raw.replace(b"\r\n0\r\n\r\n", b"\r\nX\r\n\r\n")
        fake = FakeSource(gzip.compress(broken))
        item, post = image_post()
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)

    def test_decompression_and_response_bounds(self):
        for encoding, compressed in (("gzip", gzip.compress(b"x" * 10000)), ("deflate", zlib.compress(b"x" * 10000))):
            with self.assertRaises(CommonCrawlError):
                _inflate(compressed, 100, encoding)
        fake = FakeSource()
        fake.row["length"] = str(len(fake.content) - 1)
        item, post = image_post()
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)
        fake = FakeSource()
        fake.extra_metadata["content_range"] = "bytes 1-20/300"
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)

    def test_unsafe_paths_huge_offsets_and_huge_records_are_skipped(self):
        item, post = image_post()
        for fields in ({"filename": "https://evil.invalid/warc.gz"}, {"filename": FILENAME.replace("/warc/", "/../warc/")},
                       {"offset": "-1"}, {"offset": str(2**64)}, {"length": str(100_000_000)}):
            with self.subTest(fields=fields):
                fake = FakeSource()
                fake.row.update(fields)
                with self.assertRaises(CommonCrawlError):
                    fake.source().recover(item, post, 10000)
                self.assertFalse(any("data.commoncrawl.org" in u for u, _, _ in fake.calls))
                self.assertEqual(item["cc_completed_indexes"], [])

    def test_reference_video_requires_post_thumbnail_and_complete_container(self):
        prefix = "video.twimg.com/ext_tw_video/123/"
        item = {"key": prefix, "kind": "video", "reference_only": True, "urls": ["https://" + prefix]}
        thumb = media_item("https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/abc.jpg")
        video_url = "https://" + prefix + "pu/vid/640x480/abc.mp4"
        post = {"media": [item, thumb]}
        fake = FakeSource(record(data=VIDEO, target=video_url), url=video_url)
        self.assertEqual(fake.source().recover(item, post, 10000)["data"], VIDEO)
        self.assertIsNone(fake.source().recover(item, {"media": [item]}, 10000))
        fake = FakeSource(record(data=VIDEO[:-4], target=video_url), url=video_url)
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)
        fake = FakeSource(record(data=box(b"ftyp", b"isom0000") + box(b"mdat", b"head only"), target=video_url), url=video_url)
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)

    def test_prefix_does_not_match_another_video_or_hls_fragment(self):
        prefix = "video.twimg.com/ext_tw_video/123/"
        item = {"key": prefix, "kind": "video", "reference_only": True, "urls": ["https://" + prefix]}
        post = {"media": [item, media_item("https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/a.jpg")]}
        for url in ("https://video.twimg.com/ext_tw_video/1234/vid/a.mp4", "https://" + prefix + "pu/pl/a.m3u8"):
            fake = FakeSource(url=url)
            self.assertIsNone(fake.source().recover(item, post, 10000))
            self.assertFalse(any("data.commoncrawl.org" in u for u, _, _ in fake.calls))

    def test_cancellation_propagates_before_network(self):
        class Cancelled(Exception):
            pass
        def cancel():
            raise Cancelled()
        fake = FakeSource()
        item, post = image_post()
        with self.assertRaises(Cancelled):
            fake.source(check=cancel).recover(item, post, 10000)
        self.assertEqual(fake.calls, [])

    def test_media_completion_checks_reject_cutoff_images_and_containers(self):
        self.assertTrue(_media_valid(IMAGE, "image"))
        self.assertFalse(_media_valid(IMAGE[:-2], "image"))
        self.assertFalse(_media_valid(b"<html>not media</html>", "image"))
        self.assertTrue(_media_valid(VIDEO, "video"))
        self.assertFalse(_media_valid(VIDEO[:-2], "video"))
        avif = box(b"ftyp", b"avif0000") + box(b"meta") + box(b"mdat", b"x")
        self.assertTrue(_media_valid(avif, "image"))
        self.assertFalse(_media_valid(avif[:-1], "image"))

    def test_warc_attempt_budget_prevents_unbounded_downloads(self):
        fake = FakeSource(record(data=b"not media"))
        fake.rows = [{**fake.row, "offset": str(100 + index * 1000)} for index in range(20)]
        item, post = image_post()
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)
        self.assertEqual(sum("data.commoncrawl.org" in u for u, _, _ in fake.calls), 6)

    def test_index_result_body_bound_and_redirect_validation(self):
        item, post = image_post()
        source = CommonCrawlSource(lambda u, n, headers=None: (b"x" * (INDEX_LIMIT + 1), {"url": u}))
        with self.assertRaises(CommonCrawlError):
            source.recover(item, post, 10000)
        source = CommonCrawlSource(lambda u, n, headers=None: (b"[]", {"url": "https://evil.invalid/"}))
        with self.assertRaises(CommonCrawlError):
            source.recover(item, post, 10000)

    def test_range_ignored_by_server_is_not_accepted(self):
        fake = FakeSource()
        fake.extra_metadata["status"] = 200
        item, post = image_post()
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)

    def test_corrupt_gzip_and_incorrect_content_length_are_not_recovered(self):
        compressed = record()
        bad_length = gzip.decompress(compressed).replace(f"Content-Length: {len(IMAGE)}\r\n".encode(), b"Content-Length: 99\r\n")
        for damaged in (compressed[:-5], gzip.compress(bad_length), gzip.compress(b"\r\n\r\n")):
            fake = FakeSource(damaged)
            item, post = image_post()
            with self.assertRaises(CommonCrawlError):
                fake.source().recover(item, post, 10000)

    def test_gif_video_reference_uses_only_exact_associated_token(self):
        url = "https://video.twimg.com/tweet_video/Token.mp4"
        item = {"key": "video.twimg.com/tweet_video/Token.mp4", "kind": "video", "reference_only": True, "urls": [url]}
        post = {"media": [item, media_item("https://pbs.twimg.com/tweet_video_thumb/Token.jpg")]}
        fake = FakeSource(record(data=VIDEO, target=url), url=url)
        result = fake.source().recover(item, post, 10000)
        self.assertEqual(result["data"], VIDEO)
        self.assertEqual(parse_qs(urlsplit(result["evidence_url"]).query)["matchType"], ["exact"])

    def test_zero_media_limit_keeps_source_safety_cap_but_does_not_disable_recovery(self):
        fake = FakeSource()
        item, post = image_post()
        self.assertEqual(fake.source().recover(item, post, 0)["data"], IMAGE)

    def test_circuit_breaker_does_not_claim_unrequested_index_scope(self):
        class NotSentError(Exception):
            request_sent = False
        fake = FakeSource()
        fake.query_error = NotSentError("source circuit breaker is open")
        item, post = image_post()
        source = fake.source()
        with self.assertRaises(NotSentError):
            source.recover(item, post, 10000)
        self.assertEqual(source.last_scope, [])

    def test_sent_http_failure_keeps_attempted_scope_but_propagates_error(self):
        class SentError(Exception):
            request_sent = True
        fake = FakeSource()
        fake.query_error = SentError("HTTP 504")
        item, post = image_post()
        source = fake.source()
        with self.assertRaises(SentError):
            source.recover(item, post, 10000)
        self.assertEqual(source.last_scope[0], "CC-MAIN-2020-24")
        self.assertEqual(len(source.last_scope), 6)
        self.assertEqual(item["cc_pending_indexes"], source.last_scope)
        self.assertEqual(item["cc_completed_indexes"], [])

    def test_cancelled_query_does_not_claim_index_scope(self):
        class Cancelled(Exception):
            pass
        fake = FakeSource()
        fake.query_error = Cancelled()
        item, post = image_post()
        source = fake.source()
        with self.assertRaises(Cancelled):
            source.recover(item, post, 10000)
        self.assertEqual(source.last_scope, [])

    def test_catalog_failure_resets_previous_scope(self):
        def fail_catalog(*args, **kwargs):
            raise RuntimeError("catalog is unavailable")
        source = CommonCrawlSource(fail_catalog)
        source.last_scope = ["CC-MAIN-2020-24"]
        item, post = image_post()
        with self.assertRaises(RuntimeError):
            source.recover(item, post, 10000)
        self.assertEqual(source.last_scope, [])

    def test_cached_empty_indexes_remain_verified_scope_without_new_requests(self):
        fake = FakeSource()
        fake.rows = []
        item, post = image_post()
        source = fake.source()
        self.assertIsNone(source.recover(item, post, 10000))
        original_scope = list(source.last_scope)
        original_calls = len(fake.calls)
        self.assertEqual(len(original_scope), 6)
        item, post = image_post()
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertEqual(source.last_scope, original_scope)
        self.assertEqual(len(fake.calls), original_calls)

    def test_persisted_progress_advances_beyond_first_six_until_catalog_exhausted(self):
        item, post = image_post()
        catalog = [{"id": f"CC-MAIN-2020-{week:02d}"} for week in range(5, 50, 3)]
        queried = []
        for expected_total in (6, 12, 15):
            fake = FakeSource()
            fake.collections = catalog
            fake.rows = []
            source = fake.source(max_indexes=6)
            self.assertIsNone(source.recover(item, post, 10000))
            self.assertLessEqual(len(source.last_scope), 6)
            self.assertFalse(set(queried).intersection(source.last_scope))
            queried.extend(source.last_scope)
            self.assertEqual(len(item["cc_completed_indexes"]), expected_total)
            self.assertEqual(item["cc_catalog_size"], 15)
            self.assertEqual(item["cc_more"], expected_total < 15)
            # Recreate the item/post as the real SQLite JSON round trip does.
            item = json.loads(json.dumps(item))
            post["media"] = [item]
        final = FakeSource()
        final.collections = catalog
        final.rows = []
        source = final.source()
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertEqual(source.last_scope, [])
        self.assertEqual(len(final.calls), 1)  # Refresh catalog, no old CDX lookups.
        self.assertEqual(item["cc_remaining_indexes"], [])

    def test_new_catalog_index_is_included_after_previous_exhaustion(self):
        fake = FakeSource()
        fake.collections = [{"id": "CC-MAIN-2020-24"}]
        fake.rows = []
        item, post = image_post()
        self.assertIsNone(fake.source().recover(item, post, 10000))
        self.assertFalse(item["cc_more"])
        newer = FakeSource()
        newer.collections = fake.collections + [{"id": "CC-MAIN-2027-05"}]
        newer.rows = []
        source = newer.source()
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertEqual(source.last_scope, ["CC-MAIN-2027-05"])
        self.assertEqual(len(item["cc_completed_indexes"]), 2)
        self.assertFalse(item["cc_more"])

    def test_failed_index_is_retained_while_other_empty_indexes_progress(self):
        class TemporaryFailure(Exception):
            request_sent = True
        fake = FakeSource()
        fake.rows = []
        ordinary = fake.fetch
        def fetch(url, limit, headers=None):
            if "CC-MAIN-2020-29-index" in url:
                raise TemporaryFailure("HTTP 504")
            return ordinary(url, limit, headers)
        item, post = image_post()
        source = CommonCrawlSource(fetch, max_indexes=3)
        with self.assertRaises(TemporaryFailure):
            source.recover(item, post, 10000)
        self.assertEqual(item["cc_completed_indexes"], ["CC-MAIN-2020-24", "CC-MAIN-2026-39"])
        self.assertEqual(item["cc_pending_indexes"], ["CC-MAIN-2020-29"])
        self.assertIn("HTTP 504", item["cc_index_errors"]["CC-MAIN-2020-29"]["error"])
        followup = fake.source(max_indexes=3)
        self.assertIsNone(followup.recover(item, post, 10000))
        self.assertEqual(followup.last_scope[0], "CC-MAIN-2020-29")
        self.assertEqual(item["cc_pending_indexes"], [])
        self.assertEqual(item["cc_index_errors"], {})
        self.assertEqual(len(item["cc_completed_indexes"]), 5)

    def test_cancellation_keeps_preceding_completed_indexes_without_completing_current(self):
        class Cancelled(Exception):
            pass
        fake = FakeSource()
        fake.rows = []
        ordinary = fake.fetch
        def fetch(url, limit, headers=None):
            if "CC-MAIN-2020-29-index" in url:
                raise Cancelled()
            return ordinary(url, limit, headers)
        item, post = image_post()
        source = CommonCrawlSource(fetch)
        with self.assertRaises(Cancelled):
            source.recover(item, post, 10000)
        self.assertEqual(item["cc_completed_indexes"], ["CC-MAIN-2020-24"])
        self.assertEqual(item["cc_pending_indexes"], ["CC-MAIN-2020-29"])
        self.assertEqual(source.last_scope, ["CC-MAIN-2020-24"])
        self.assertFalse(item["cc_index_errors"]["CC-MAIN-2020-29"]["request_sent"])

    def test_same_image_identifier_can_restore_another_image_format(self):
        png_url = "https://pbs.twimg.com/media/Sample?format=png&name=orig"
        png = __import__("test_media_fixtures").PNG
        fake = FakeSource(record(png, target=png_url), url=png_url)
        item, post = image_post()
        result = fake.source().recover(item, post, 10000)
        self.assertEqual(result["data"], png)
        self.assertEqual(result["url"], png_url)
        self.assertFalse(item["cc_more"])
        self.assertEqual(item["cc_recovered_index"], "CC-MAIN-2020-24")

    def test_known_video_can_restore_different_bitrate_for_same_numeric_id(self):
        high = "https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/high.mp4"
        low = "https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/low.mp4"
        item = media_item(high)
        post = {"media": [item]}
        fake = FakeSource(record(VIDEO, target=low), url=low)
        result = fake.source().recover(item, post, 10000)
        self.assertEqual(result["url"], low)
        self.assertEqual(result["data"], VIDEO)
        query = parse_qs(urlsplit(result["evidence_url"]).query)
        self.assertEqual(query["url"], ["video.twimg.com/ext_tw_video/123/"])

    def test_format_and_bitrate_fallbacks_do_not_accept_neighbor_identity(self):
        cases = [
            (IMAGE_URL, "https://pbs.twimg.com/media/SampleNeighbor.png"),
            ("https://video.twimg.com/ext_tw_video/123/pu/vid/high.mp4", "https://video.twimg.com/ext_tw_video/1234/pu/vid/low.mp4"),
            ("https://video.twimg.com/ext_tw_video/123/pu/vid/high.mp4", "https://video.twimg.com/amplify_video/123/pu/vid/low.mp4"),
        ]
        for expected, unrelated in cases:
            with self.subTest(unrelated=unrelated):
                item = media_item(expected)
                fake = FakeSource(url=unrelated)
                self.assertIsNone(fake.source().recover(item, {"media": [item]}, 10000))
                self.assertFalse(any("data.commoncrawl.org" in u for u, _, _ in fake.calls))

    def test_full_result_limit_is_not_marked_completed_or_empty(self):
        fake = FakeSource()
        fake.rows = [{**fake.row, "url": f"https://pbs.twimg.com/media/SampleOther{number}.jpg"} for number in range(20)]
        item, post = image_post()
        with self.assertRaisesRegex(CommonCrawlError, "20"):
            fake.source().recover(item, post, 10000)
        self.assertEqual(item["cc_completed_indexes"], [])
        self.assertTrue(item["cc_pending_indexes"])
        self.assertTrue(item["cc_more"])

    def test_bad_json_schema_does_not_complete_a_collection(self):
        fake = FakeSource()
        fake.rows = [{"error": "service unavailable"}]
        item, post = image_post()
        with self.assertRaises(CommonCrawlError):
            fake.source().recover(item, post, 10000)
        self.assertEqual(item["cc_completed_indexes"], [])
        self.assertTrue(item["cc_pending_indexes"])

    def test_changing_asset_identity_invalidates_old_completed_scans(self):
        fake = FakeSource()
        fake.rows = []
        item, post = image_post()
        source = fake.source(max_indexes=1)
        self.assertIsNone(source.recover(item, post, 10000))
        old_identity = item["cc_scan_identity"]
        replacement = media_item("https://pbs.twimg.com/media/Another.jpg")
        item.update(replacement)
        self.assertIsNone(source.recover(item, post, 10000))
        self.assertNotEqual(old_identity, item["cc_scan_identity"])
        self.assertEqual(len(item["cc_completed_indexes"]), 1)


if __name__ == "__main__":
    unittest.main()
