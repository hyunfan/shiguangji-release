"""Offline transport regression tests: no real requests or collection mutations."""
import gzip
import hashlib
import io
import json
import tempfile
import threading
import unittest
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

from archive import ArchiveError, Cancelled
from source_http import SourceClient, SourceError, SourceRedirect, validate_source_url


IMAGE = "https://pbs.twimg.com/media/fixture.jpg"
VIDEO = "https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/movie.mp4"
API = "https://cdn.syndication.twimg.com/tweet-result?id=123&token=public"
from test_media_fixtures import png
PNG = png(700000)


class Response(io.BytesIO):
    def __init__(self, data, url=IMAGE, content_type="image/png", headers=None, after_read=None):
        super().__init__(data)
        self.url, self.status = url, 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(data))
        for key, value in (headers or {}).items():
            if key in self.headers:del self.headers[key]
            self.headers[key] = str(value)
        self.read_sizes = []
        self.after_read = after_read

    def read(self, size=-1):
        self.read_sizes.append(size)
        value = super().read(size)
        if self.after_read:self.after_read()
        return value

    def read1(self, size=-1):
        # Exercise the transport's interruptible read path, including subclasses
        # that inject a failure or cancellation through read().
        return self.read(size)


class SourceURLTests(unittest.TestCase):
    def test_supported_urls(self):
        for url in [IMAGE, VIDEO, API, "https://api.fxtwitter.com/alice/status/123",
                    "https://api.fxtwitter.com/2/profile/alice", "https://api.fxtwitter.com/2/profile/id:123",
                    "https://index.commoncrawl.org/collinfo.json",
                    "https://index.commoncrawl.org/CC-MAIN-2026-38-index?url=twitter.com%2Falice&output=json",
                    "https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-38/segments/123/warc/a.warc.gz"]:
            with self.subTest(url=url):self.assertTrue(validate_source_url(url))

    def test_external_hosts_credentials_ports_and_non_media_are_rejected(self):
        for url in ["http://pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com.evil.test/media/a.jpg",
                    "https://127.0.0.1/media/a.jpg", "https://user:pass@pbs.twimg.com/media/a.jpg",
                    "https://pbs.twimg.com:8443/media/a.jpg", "https://pbs.twimg.com/profile_images/a.jpg",
                    "https://api.fxtwitter.com/status/123", "https://index.commoncrawl.org/robots.txt",
                    "https://api.fxtwitter.com/2/profile/../private", "https://api.fxtwitter.com/2/profile/alice?token=secret",
                    "https://api.vxtwitter.com/2/profile/alice",
                    "https://data.commoncrawl.org/crawl-data/../private.warc.gz"]:
            with self.subTest(url=url), self.assertRaises(SourceError):validate_source_url(url)

    def test_media_paths_cannot_escape_allowed_prefix_or_include_credentials(self):
        for url in ["https://pbs.twimg.com/foo/media/a.jpg", "https://pbs.twimg.com/media/../profile_images/a.jpg",
                    "https://pbs.twimg.com/media/%2e%2e/a.jpg", "https://pbs.twimg.com/media/a.jpg#fragment",
                    "https://@pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com/media/a.jpg\n"]:
            with self.subTest(url=url), self.assertRaises(SourceError):validate_source_url(url)

    def test_redirect_to_untrusted_host_is_rejected_before_request(self):
        handler = SourceRedirect()
        with self.assertRaises(SourceError):
            handler.redirect_request(Request(IMAGE), None, 302, "Found", {}, "https://evil.test/media/fixture.jpg")

    def test_redirect_cannot_change_resource_or_source_role(self):
        handler = SourceRedirect()
        for requested, final in [(IMAGE, IMAGE.replace("fixture", "other")), (API, IMAGE),
                                 (API, API.replace("id=123", "id=456"))]:
            with self.subTest(requested=requested, final=final), self.assertRaises(SourceError):
                handler.redirect_request(Request(requested), None, 302, "Found", {}, final)

    def test_media_size_variant_redirect_is_allowed(self):
        request = SourceRedirect().redirect_request(Request(IMAGE), None, 302, "Found", {},
                                                    "https://pbs.twimg.com/media/fixture?format=jpg&name=orig")
        self.assertIsNotNone(request)


class SourceClientTests(unittest.TestCase):
    def client(self, *args, **kwargs):
        client = SourceClient(*args, delay=0, **kwargs)
        client.wait = Mock(side_effect=lambda seconds: client.check())
        client.opener = Mock()
        return client

    def test_anonymous_headers_and_unknown_headers_rejected(self):
        client = self.client()
        for headers in ({"Cookie": "session=private"}, {"Authorization": "Bearer private"}, {"Host": "evil.test"}):
            with self.assertRaises(SourceError):client.open(IMAGE, headers)
        self.assertEqual(client.opener.open.call_count, 0)
        response = Response(PNG)
        client.opener.open.return_value = response
        client.open(IMAGE).close()
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)

    def test_public_discovery_integrates_with_anonymous_transport(self):
        from public_sources import discover
        client = self.client()
        def respond(request, timeout):
            body = json.dumps({"id_str": "123", "user": {"screen_name": "alice"}, "photos": [{"url": IMAGE}]}).encode()
            return Response(body, request.full_url, "application/json")
        client.opener.open.side_effect = respond
        post = {"id": "123", "username": "alice"}
        result = discover(post, client.fetch, client.check, lambda message: None)
        self.assertEqual(len(result), 1)
        self.assertEqual(client.requests, 1)
        self.assertEqual(post["source_checks"]["syndication"]["status"], "found")

    def test_public_discovery_preserves_transport_http_status(self):
        from public_sources import discover
        client = self.client()
        client.opener.open.side_effect = HTTPError(API, 404, "Not found", Message(), io.BytesIO())
        post = {"id": "123", "username": "alice"}
        self.assertEqual(discover(post, client.fetch, client.check, lambda message: None), [])
        self.assertEqual(post["source_checks"]["syndication"]["status"], "not_found")

    def test_final_media_identity_mismatch_closes_response(self):
        client = self.client()
        response = Response(PNG, IMAGE.replace("fixture", "different"))
        client.opener.open.return_value = response
        with self.assertRaises(SourceError):client.open(IMAGE)
        self.assertTrue(response.closed)

    def test_final_metadata_identity_is_checked_even_with_custom_opener(self):
        client = self.client()
        response = Response(b"{}", API.replace("id=123", "id=456"), "application/json")
        client.opener.open.return_value = response
        with self.assertRaises(SourceError):client.fetch(API, 100)
        self.assertTrue(response.closed)

    def test_streamed_download_is_atomic_hashed_and_bounded_reads(self):
        client = self.client()
        response = Response(PNG)
        client.opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "a.bin"
            result = client.download(IMAGE, destination, len(PNG))
            self.assertEqual(result["file"].read_bytes(), PNG)
            self.assertEqual(result["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(result["bytes"], len(PNG))
            self.assertFalse(destination.with_suffix(".bin.part").exists())
            self.assertTrue(response.closed)
            self.assertGreater(len(response.read_sizes), 2)
            self.assertTrue(all(0 < n <= 256 * 1024 for n in response.read_sizes))

    def test_oversize_or_html_download_is_not_saved(self):
        for body, limit in [(PNG, 100), (b"<html>not a video</html>", 1000)]:
            client = self.client();client.opener.open.return_value = Response(body)
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "a.bin"
                with self.assertRaises(ArchiveError):client.download(IMAGE, destination, limit)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_truncated_content_length_is_not_saved(self):
        client = self.client()
        client.opener.open.return_value = Response(PNG, headers={"Content-Length": len(PNG) + 1})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArchiveError):client.download(IMAGE, Path(directory) / "a.bin", len(PNG) + 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancel_before_open_does_not_connect(self):
        stop = threading.Event();stop.set()
        client = self.client(stop=stop)
        with self.assertRaises(Cancelled):client.open(IMAGE)
        client.opener.open.assert_not_called()

    def test_cancel_mid_download_removes_partial(self):
        stop = threading.Event();client = self.client(stop=stop)
        response = Response(PNG, after_read=stop.set)
        client.opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Cancelled):client.download(IMAGE, Path(directory) / "a.bin", len(PNG))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertTrue(response.closed)

    def test_cancel_during_fetch_is_observed_before_returning_data(self):
        stop = threading.Event();client = self.client(stop=stop)
        client.opener.open.return_value = Response(b"{}", API, "application/json", after_read=stop.set)
        with self.assertRaises(Cancelled):client.fetch(API, 100)

    def test_fetch_network_read_errors_are_wrapped_and_response_closed(self):
        for error in (TimeoutError("fixture timeout"), OSError("fixture disconnected"), IncompleteRead(b"partial", 20)):
            with self.subTest(error=type(error).__name__):
                client = self.client()
                response = Response(b"{}", API, "application/json")
                response.read = Mock(side_effect=error)
                client.opener.open.return_value = response
                with self.assertRaises(SourceError) as caught:client.fetch(API, 100)
                self.assertIs(caught.exception.__cause__, error)
                self.assertTrue(response.closed)

    def test_cancel_takes_priority_over_read_timeout(self):
        stop = threading.Event();client = self.client(stop=stop)
        response = Response(b"{}", API, "application/json")
        def interrupted_read(size):
            stop.set()
            raise TimeoutError("fixture timeout")
        response.read = interrupted_read
        client.opener.open.return_value = response
        with self.assertRaises(Cancelled):client.fetch(API, 100)
        self.assertTrue(response.closed)

    def test_download_read_failure_is_source_error_and_removes_temp_file(self):
        client = self.client()
        response = Response(PNG)
        response.read = Mock(side_effect=[PNG[:8192], TimeoutError("fixture disconnected")])
        client.opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SourceError):client.download(IMAGE, Path(directory) / "fixture.bin", len(PNG))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(response.closed)

    def test_404_is_not_retried_and_does_not_trip_circuit(self):
        client = self.client()
        error = HTTPError(IMAGE, 404, "Not found", Message(), io.BytesIO(b""))
        client.opener.open.side_effect = error
        with self.assertRaises(SourceError) as caught:client.open(IMAGE)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertFalse(client.blocked)
        self.assertTrue(error.closed)

    def test_transient_failure_retries_then_blocks_only_its_host(self):
        client = self.client()
        client.opener.open.side_effect = URLError("fixture network failure")
        with self.assertRaises(SourceError):client.open(IMAGE)
        self.assertEqual(client.requests, 2)
        with self.assertRaises(SourceError):client.open(IMAGE)
        self.assertEqual(client.requests, 3)
        with self.assertRaises(SourceError):client.open(IMAGE)
        self.assertEqual(client.requests, 3)
        self.assertIn("pbs.twimg.com", client.blocked)
        client.opener.open.side_effect = None
        client.opener.open.return_value = Response(b"{}", API, "application/json")
        self.assertEqual(client.fetch(API, 100)[0], b"{}")

    def test_expected_404_resets_transient_streak_before_next_query(self):
        client=self.client()
        client.opener.open.side_effect=[URLError('timeout'),HTTPError(IMAGE,404,'No captures',Message(),io.BytesIO(b'')),
                                      URLError('timeout'),HTTPError(IMAGE,404,'No captures',Message(),io.BytesIO(b''))]
        for _ in range(2):
            with self.assertRaises(SourceError) as caught:client.open(IMAGE)
            self.assertEqual(caught.exception.status,404)
        self.assertFalse(client.blocked)
        self.assertEqual(client.requests,4)

    def test_success_resets_consecutive_failure_count(self):
        client = self.client()
        client.opener.open.side_effect = [URLError("one failure"), Response(PNG)]
        client.open(IMAGE).close()
        self.assertEqual(client.requests, 2)
        self.assertEqual(client.failures["pbs.twimg.com"], 0)

    def test_request_budget_stops_retries(self):
        client = self.client(max_requests=1)
        client.opener.open.side_effect = URLError("fixture failure")
        with self.assertRaises(SourceError):client.open(IMAGE)
        self.assertEqual(client.requests, 1)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_unattempted_and_retried_requests_report_actual_attempt_status(self):
        client=self.client(max_requests=0)
        with self.assertRaises(SourceError) as caught:client.open(IMAGE)
        self.assertFalse(caught.exception.request_sent)
        client=self.client()
        client.blocked.add('pbs.twimg.com')
        with self.assertRaises(SourceError) as caught:client.open(IMAGE)
        self.assertFalse(caught.exception.request_sent)
        client=self.client(max_requests=1)
        client.opener.open.side_effect=HTTPError(IMAGE,504,'Gateway Timeout',Message(),io.BytesIO(b''))
        with self.assertRaises(SourceError) as caught:client.open(IMAGE)
        self.assertTrue(caught.exception.request_sent)
        self.assertEqual(caught.exception.status,504)

    def test_default_budget_and_timeout(self):
        client = self.client()
        self.assertEqual(client.max_requests, 650)
        client.opener.open.return_value = Response(PNG)
        client.open(IMAGE).close()
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 20)

    def test_fetch_requires_positive_byte_limit_without_connecting(self):
        client = self.client()
        for limit in (0, -1, 1.5, None):
            with self.subTest(limit=limit), self.assertRaises(SourceError):client.fetch(API, limit)
        client.opener.open.assert_not_called()

    def test_fetch_streams_in_bounded_chunks(self):
        client = self.client()
        body = b"x" * (1024 * 1024)
        response = Response(body, API, "application/json")
        client.opener.open.return_value = response
        self.assertEqual(client.fetch(API, len(body))[0], body)
        self.assertGreater(len(response.read_sizes), 1)
        self.assertTrue(all(0 < n <= 256 * 1024 for n in response.read_sizes))

    def test_gzip_metadata_decodes_with_both_compressed_and_decoded_limits(self):
        body = b'{"data":"' + b"a" * 5000 + b'"}'
        client = self.client()
        client.opener.open.return_value = Response(gzip.compress(body), API, "application/json", {"Content-Encoding": "gzip"})
        decoded, meta = client.fetch(API, len(body))
        self.assertEqual(decoded, body)
        self.assertEqual(meta["content_encoding"], "")
        client.opener.open.return_value = Response(gzip.compress(body), API, "application/json", {"Content-Encoding": "gzip"})
        with self.assertRaises(SourceError):client.fetch(API, 1000)
        client.opener.open.return_value = Response(b"x" * 1001, API)
        with self.assertRaises(SourceError):client.fetch(API, 1000)

    def test_range_bytes_are_not_decompressed(self):
        url = "https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-38/segments/123/warc/a.warc.gz"
        payload = gzip.compress(b"fixture")
        client = self.client()
        response = Response(payload, url, "application/octet-stream", {"Content-Encoding": "gzip", "Content-Range": f"bytes 0-{len(payload)-1}/{len(payload)}"})
        response.status = 206
        client.opener.open.return_value = response
        data, meta = client.fetch(url, len(payload), {"Range": f"bytes=0-{len(payload)-1}"})
        self.assertEqual(data, payload)
        self.assertEqual(meta["status"], 206)


if __name__ == "__main__":
    unittest.main()
