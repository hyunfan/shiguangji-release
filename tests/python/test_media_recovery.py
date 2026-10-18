"""Integration regressions for multi-source recovery using offline fixtures only."""
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from archive import ArchiveError, Cancelled, Client
from extractor import blank_post, media_item
from media_recovery import RecoveryEngine, image_variants, related
from source_http import SourceError
from public_sources import discover as public_discover
from run_budget import RunLimit
from test_archive import FakeResponse, PNG
from test_commoncrawl import VIDEO


IMAGE_URL = "https://pbs.twimg.com/media/Fixture.jpg"
PID = "1344795472600000000"


class MemoryStore:
    def __init__(self, posts):
        self.rows = copy.deepcopy(posts)

    def posts(self):
        return copy.deepcopy(self.rows)

    def save_post(self, post):
        for index, old in enumerate(self.rows):
            if old["id"] == post["id"]:
                self.rows[index] = copy.deepcopy(post)
                return
        self.rows.append(copy.deepcopy(post))


class NoWayback:
    def cdx(self, *args, **kwargs):
        yield []

    def text(self, url, limit=None):
        if '/cdx/search/cdx?' in url:return ''
        raise AssertionError("No fixture has a Wayback snapshot")

    def download(self, *args, **kwargs):
        raise ArchiveError("HTTP Error 404: NOT FOUND", status=404)


class PlayerWayback(NoWayback):
    def __init__(self, entries, source, config_error=False):
        self.entries, self.source, self.config_error = entries, source, config_error
        self.queries, self.reads = [], []

    def cdx(self, query, match_type="exact"):
        self.queries.append((query, match_type))
        if self.config_error and query.startswith("api.twitter.com/"):
            raise ArchiveError("fixture configuration endpoint unavailable")
        yield self.entries.get(query, [])

    def text(self, url, limit=None):
        if '/cdx/search/cdx?' in url:return ''
        self.reads.append(url)
        return self.source


class FixtureCollector:
    def __init__(self, folder, post, **options):
        self.folder = Path(folder)
        self.options = {"common_crawl": True, "public_mirrors": True, **options}
        self.stop = threading.Event()
        self.store = MemoryStore([post])
        self.client = NoWayback()
        self.logs = []
        self.run_info = {}
        self.progress_calls = []

    def log(self, message):
        self.logs.append(message)

    def progress(self, **values):
        self.progress_calls.append(values)

    def checkpoint(self, status):
        pass

    def saved_file_valid(self, item):
        file = self.folder / item.get("path", "")
        return item.get("status") == "saved" and file.is_file() and file.stat().st_size == item.get("bytes")


class FixtureHTTP:
    def __init__(self, stop, replies=None):
        self.stop = stop
        self.replies = replies or {}
        self.requests = 0
        self.urls = []

    def check(self):
        if self.stop.is_set():
            raise Cancelled()

    def fetch(self, *args, **kwargs):
        raise AssertionError("No unexpected public HTTP metadata requests")

    def open(self, url):
        self.check()
        self.requests += 1
        self.urls.append(url)
        answer = self.replies.get(url, SourceError("fixture 请求失败：HTTP 404", 404))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer()
        return FakeResponse(answer, url)

    def download(self, url, destination, max_bytes):
        return Client.download(self, url, destination, max_bytes)


class FixtureCC:
    last_scope = ["CC-MAIN-2020-24", "CC-MAIN-2026-39"]

    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def recover(self, item, post, max_bytes):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def post_with(item=None):
    post = blank_post(PID, "alice")
    item = item or media_item(IMAGE_URL)
    item.update(status="failed", error="Wayback 未恢复", error_kind="not_indexed")
    post["media"] = [item]
    return post


def cc_result(data=PNG):
    return {"data": data, "source": "common_crawl", "url": IMAGE_URL,
            "retrieved_url": "https://data.commoncrawl.org/crawl-data/CC-MAIN-2020-24/segments/123/warc/sample.warc.gz",
            "captured_at": "20200615120000", "evidence_url": "https://index.commoncrawl.org/CC-MAIN-2020-24-index?url=fixture"}


def no_discovery(*args, **kwargs):
    return []


class MediaRecoveryTests(unittest.TestCase):
    def test_known_mp4_saves_before_any_metadata_lookup(self):
        url='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/good.mp4'
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with(media_item(url)))
            collector.client.cdx=Mock(side_effect=AssertionError('No Wayback lookup needed'))
            discover=Mock(side_effect=AssertionError('No public metadata needed'))
            historical=Mock()
            http=FixtureHTTP(collector.stop,{url:VIDEO})
            cc=FixtureCC()
            engine=RecoveryEngine(collector,http=http,discover=discover,common_crawl=cc,archived_sources=historical)
            engine.run()
            item=collector.store.rows[0]['media'][0]
            self.assertEqual(http.urls,[url])
            self.assertEqual(item['source'],'live_cdn')
            self.assertEqual(item['source_evidence'],collector.store.rows[0]['url'])
            self.assertEqual((Path(folder)/item['path']).read_bytes(),VIDEO)
            discover.assert_not_called()
            historical.discover.assert_not_called()
            collector.client.cdx.assert_not_called()
            self.assertEqual(cc.calls,0)

    def test_known_files_in_post_are_saved_before_missing_reference_metadata(self):
        video='https://video.twimg.com/ext_tw_video/456/pu/vid/640x360/good.mp4'
        reference={'key':'video.twimg.com/ext_tw_video/123/','kind':'video','reference_only':True,'urls':[],'path':''}
        post=post_with(reference)
        post['media'] += [media_item(video),media_item(IMAGE_URL)]
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post)
            http=FixtureHTTP(collector.stop,{video:VIDEO,IMAGE_URL:PNG})
            def discover(*args,**kwargs):
                self.assertEqual(http.urls,[video,IMAGE_URL])
                self.assertEqual([m['status'] for m in collector.store.rows[0]['media'][1:]],['saved','saved'])
                raise RunLimit('metadata budget exhausted')
            engine=RecoveryEngine(collector,http=http,discover=discover,common_crawl=FixtureCC())
            with self.assertRaises(RunLimit):engine.run()
            result=collector.store.rows[0]
            self.assertEqual(engine.stats['recovered'],2)
            self.assertFalse(engine.check_cached(result,result['media'][0]))
            self.assertTrue(all(collector.saved_file_valid(m) for m in result['media'][1:]))

    def test_metadata_after_failed_direct_is_shared_and_duplicate_urls_are_not_retried(self):
        bad='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/bad.mp4'
        good=bad.replace('/bad.mp4','/good.mp4')
        other='https://video.twimg.com/ext_tw_video/456/pu/vid/640x360/other.mp4'
        post=post_with(media_item(bad))
        post['media'].append({'key':'video.twimg.com/ext_tw_video/456/','kind':'video','reference_only':True,'urls':[],'path':'','status':'pending'})
        evidence='https://api.fxtwitter.com/alice/status/'+PID
        discoveries=[media_item(url) for url in (bad,good,other)]
        for item in discoveries:item['evidence_url']=evidence
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post)
            http=FixtureHTTP(collector.stop,{good:VIDEO,other:VIDEO})
            def discover(p,*args,**kwargs):
                self.assertEqual(http.urls,[bad])
                p['source_checks']={'fxtwitter':{'status':'found','evidence_url':evidence}}
                return discoveries
            discovery=Mock(side_effect=discover)
            engine=RecoveryEngine(collector,http=http,discover=discovery,common_crawl=FixtureCC())
            engine.run()
            self.assertEqual(http.urls,[bad,good,other])
            discovery.assert_called_once()
            self.assertTrue(all(item['source_evidence']==evidence for item in collector.store.rows[0]['media']))
            self.assertEqual(engine.stats['recovered'],2)

    def test_same_failed_candidate_from_all_metadata_sources_is_requested_once(self):
        video='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/missing.mp4'
        discovered=media_item(video);discovered['evidence_url']='public-fixture'
        with tempfile.TemporaryDirectory() as folder:
            raw=Path(folder)/'raw';raw.mkdir()
            (raw/'fixture.txt').write_text(video,encoding='utf-8')
            collector=FixtureCollector(folder,post_with(media_item(video)))
            http=FixtureHTTP(collector.stop)
            engine=RecoveryEngine(collector,http=http,discover=lambda *a,**k:[discovered],common_crawl=FixtureCC())
            engine.archived_config=Mock(return_value=[(video,'config-fixture')])
            engine.archived_metadata=Mock(return_value=[(video,'historical-fixture')])
            engine.run()
            self.assertEqual(http.urls,[video])
            item=collector.store.rows[0]['media'][0]
            self.assertNotEqual(item['status'],'saved')
            self.assertTrue(engine.check_cached(collector.store.rows[0],item))

    def test_restricted_post_does_not_expand_metadata_after_direct_failure(self):
        video='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/missing.mp4'
        post=post_with(media_item(video));post['content_reason']='restricted'
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post)
            discover=Mock(side_effect=AssertionError('restricted metadata lookup'))
            historical=Mock()
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=discover,
                                  common_crawl=FixtureCC(),archived_sources=historical)
            collector.client.cdx=Mock(return_value=[[]])
            engine.run()
            discover.assert_not_called()
            historical.discover.assert_not_called()
            self.assertTrue(all('config/' not in str(call) for call in collector.client.cdx.call_args_list))

    def test_cancelled_metadata_propagates_without_caching_unfinished_item(self):
        item={'key':'video.twimg.com/ext_tw_video/123/','kind':'video','reference_only':True,'urls':[],'path':''}
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with(item))
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=Mock(side_effect=Cancelled()),common_crawl=FixtureCC())
            with self.assertRaises(Cancelled):engine.run()
            result=collector.store.rows[0]
            self.assertFalse(engine.check_cached(result,result['media'][0]))
            self.assertEqual(engine.stats['recovered'],0)

    def test_wayback_failed_capture_keeps_download_error(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            collector.client.cdx=Mock(return_value=[[{'original':IMAGE_URL,'timestamp':'20210101000000'}]])
            collector.client.download=Mock(side_effect=ArchiveError('HTTP 404: archived media missing'))
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,common_crawl=FixtureCC())
            post=collector.store.rows[0];item=post['media'][0]
            self.assertFalse(engine.archive_media(post,item))
            report=next(row for row in item['recovery_checks'] if row['source']=='wayback')
            self.assertEqual(report['status'],'failed')
            self.assertEqual(report['captures_found'],1)
            self.assertIn('HTTP 404',report['error'])

    def test_wayback_all_captures_confirmed_404_or_410_are_cached_as_absent(self):
        errors=[]
        for status in (404,410):
            cause=HTTPError('https://web.archive.org/fixture',status,'missing',{},None)
            error=ArchiveError(f'Internet Archive 请求失败：{cause}')
            error.__cause__=cause
            cause.close()
            errors.append(error)
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            rows=[{'original':IMAGE_URL,'timestamp':f'2021010{day}000000'} for day in (1,2)]
            collector.client.cdx=Mock(return_value=[rows])
            collector.client.download=Mock(side_effect=errors)
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,common_crawl=FixtureCC())
            post=collector.store.rows[0];item=post['media'][0]
            self.assertFalse(engine.archive_media(post,item))
            report=next(row for row in item['recovery_checks'] if row['source']=='wayback')
            self.assertEqual(report['status'],'not_found')
            self.assertEqual(report['captures_checked'],2)
            self.assertIn('HTTP Error 410',report['error'])
            self.assertNotEqual(item.get('recovery_error_kind'),'network')
            self.assertFalse(engine.archive_media(post,item))
            self.assertEqual(collector.client.download.call_count,2)
            collector.options['force_recheck']=True
            collector.client.download.side_effect=errors
            self.assertFalse(engine.archive_media(post,item))
            self.assertEqual(collector.client.download.call_count,4)

    def test_wayback_mixed_absence_and_transient_error_remains_retryable(self):
        for transient in (ArchiveError('download timeout'),SourceError('HTTP 503',503),
                          ArchiveError('下载媒体失败：timed out after HTTP 404 candidate')):
            with self.subTest(error=str(transient)),tempfile.TemporaryDirectory() as folder:
                collector=FixtureCollector(folder,post_with())
                rows=[{'original':IMAGE_URL,'timestamp':f'2021010{day}000000'} for day in (1,2)]
                collector.client.cdx=Mock(return_value=[rows])
                collector.client.download=Mock(side_effect=[SourceError('HTTP 404',404),transient])
                engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,common_crawl=FixtureCC())
                post=collector.store.rows[0];item=post['media'][0]
                self.assertFalse(engine.archive_media(post,item))
                report=next(row for row in item['recovery_checks'] if row['source']=='wayback')
                self.assertEqual(report['status'],'failed')
                self.assertEqual(report['error'],str(transient))
                self.assertEqual(item['recovery_error_kind'],'network')
                collector.client.download.side_effect=SourceError('HTTP 404',404)
                self.assertFalse(engine.archive_media(post,item))
                self.assertEqual(collector.client.download.call_count,4)

    def test_budget_exhaustion_during_direct_does_not_fall_back_or_cache_absence(self):
        url='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/good.mp4'
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with(media_item(url)))
            discovery=Mock()
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop,{url:RunLimit('budget')}),
                                  discover=discovery,common_crawl=FixtureCC())
            with self.assertRaises(RunLimit):engine.run()
            post=collector.store.rows[0];item=post['media'][0]
            self.assertFalse(engine.check_cached(post,item))
            self.assertEqual(item.get('recovery_checks',[]),[])
            discovery.assert_not_called()

    def test_historical_transport_preserves_final_replay_url(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            final='https://web.archive.org/web/20210102000000id_/https://cdn.syndication.twimg.com/tweet-result?id='+PID
            collector.client.text_with_meta=lambda url,limit: ('{}',{'url':final})
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,common_crawl=FixtureCC())
            self.assertEqual(engine.archived_read('fixture',200),('{}',{'url':final}))

    def test_old_disabled_source_failure_does_not_become_new_failure(self):
        post=post_with()
        post['source_checks']={'fxtwitter':{'status':'unavailable','checked_at':'2020-01-01T00:00:00Z'}}
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post,public_mirrors=False)
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,common_crawl=FixtureCC())
            engine.run()
            post=collector.store.rows[0];item=post['media'][0]
            self.assertNotEqual(item.get('recovery_error_kind'),'network')
            self.assertNotIn('fxtwitter',[report['source'] for report in item['recovery_checks']])
            self.assertTrue(engine.check_cached(post,item))

    def test_alternate_bitrate_stays_with_exact_numeric_video_identity(self):
        first=media_item('https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/a.mp4')
        self.assertTrue(related(first,media_item('https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/b.mp4')))
        self.assertFalse(related(first,media_item('https://video.twimg.com/ext_tw_video/1234/pu/vid/640x360/b.mp4')))
        self.assertFalse(related(first,media_item('https://video.twimg.com/amplify_video/123/vid/640x360/b.mp4')))

    def test_unfinished_cc_progress_bypasses_whole_item_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,common_crawl=FixtureCC())
            engine.run()
            post=collector.store.rows[0];item=post['media'][0]
            self.assertTrue(engine.check_cached(post,item))
            item.update(cc_more=True,cc_pending_indexes=[])
            self.assertFalse(engine.check_cached(post,item))
            item.update(cc_more=False,cc_pending_indexes=['CC-MAIN-2020-24'])
            self.assertFalse(engine.check_cached(post,item))
            collector.options['common_crawl']=False
            self.assertTrue(engine.check_cached(post,item))

    def test_historical_candidate_saved_with_real_file_provenance(self):
        video='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/good.mp4'
        evidence=f'https://web.archive.org/web/20210101000000id_/https://nitter.net/alice/status/{PID}'
        class Historical:
            last_report=[{'status':'found','endpoint':evidence}]
            def discover(self,*args,**kwargs):return [{'url':video,'evidence_url':evidence,'captured_at':'20210101000000'}]
        item={'key':'video.twimg.com/ext_tw_video/123/','kind':'video','reference_only':True,'urls':[],'path':''}
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with(item))
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop,{video:VIDEO}),discover=no_discovery,
                                  common_crawl=FixtureCC(),archived_sources=Historical())
            engine.run()
            result=collector.store.rows[0]['media'][0]
            self.assertEqual(result['source'],'live_cdn')
            self.assertEqual(result['source_evidence'],evidence)
            self.assertEqual(result['status'],'saved')
            self.assertEqual((Path(folder)/result['path']).read_bytes(),VIDEO)

    def test_historical_link_without_file_remains_missing_and_is_persisted(self):
        video='https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/good.mp4'
        class Historical:
            last_report=[{'status':'found'}]
            def discover(self,*args,**kwargs):return [{'url':video,'evidence_url':'fixture'}]
        item={'key':'video.twimg.com/ext_tw_video/123/','kind':'video','reference_only':True,'urls':[],'path':''}
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with(item))
            engine=RecoveryEngine(collector,http=FixtureHTTP(collector.stop),discover=no_discovery,
                                  common_crawl=FixtureCC(),archived_sources=Historical())
            engine.run()
            result=collector.store.rows[0]['media'][0]
            self.assertNotEqual(result['status'],'saved')
            self.assertIn(video,result['urls'])
            self.assertEqual(engine.stats['recovered'],0)

    def test_cached_player_endpoint_recovers_only_same_post_and_media(self):
        prefix = "video.twimg.com/ext_tw_video/123/"
        video = "https://" + prefix + "pu/vid/640x360/good.mp4"
        player = f"https://twitter.com/i/videos/{PID}"
        unrelated_player = "https://twitter.com/i/videos/999"
        item = {"key": prefix, "kind": "video", "reference_only": True, "urls": ["https://" + prefix], "path": ""}
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "raw";raw.mkdir()
            (raw / "fixture.txt").write_text(f'<a href="{player}">player</a><a href="{unrelated_player}">other</a><a href="https://evil.test/i/videos/{PID}">external</a>', encoding="utf-8")
            collector = FixtureCollector(folder, post_with(item))
            collector.client = PlayerWayback({player: [{"timestamp": "20210101000000", "original": player}]},
                                            json.dumps({"video": video, "other": "https://video.twimg.com/ext_tw_video/999/pu/vid/wrong.mp4"}))
            http = FixtureHTTP(collector.stop, {video: VIDEO})
            engine = RecoveryEngine(collector, http=http, discover=no_discovery, common_crawl=FixtureCC())
            engine.run()
            media = collector.store.rows[0]["media"][0]
            self.assertEqual(media["status"], "saved")
            self.assertEqual(http.urls, [video])
            self.assertEqual(collector.client.queries, [(f"api.twitter.com/1.1/videos/tweet/config/{PID}.json", "exact"), (player, "exact")])
            self.assertIn(player, media["source_evidence"])
            check = next(c for c in media["recovery_checks"] if c["source"] == "wayback_video_config")
            self.assertEqual(check["captures_checked"], 1)
            entry = check["entry_points"][1]
            self.assertEqual(entry["observed_at"], "local-cache:fixture.txt")
            self.assertEqual(entry["status"], "found")
            self.assertEqual(check["evidence_url"], media["source_evidence"])

    def test_player_endpoint_is_never_guessed_without_raw_evidence(self):
        item = {"key": "video.twimg.com/ext_tw_video/123/", "kind": "video", "reference_only": True, "urls": [], "path": ""}
        with tempfile.TemporaryDirectory() as folder:
            post = post_with(item);collector = FixtureCollector(folder, post)
            collector.client = PlayerWayback({}, "")
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=FixtureCC())
            engine.cached_links([post])
            self.assertEqual(engine.archived_config(post, post["media"][0]), [])
            self.assertEqual(collector.client.queries, [(f"api.twitter.com/1.1/videos/tweet/config/{PID}.json", "exact")])

    def test_player_rows_for_other_post_are_not_read(self):
        player = f"https://twitter.com/i/videos/{PID}"
        item = {"key": "video.twimg.com/ext_tw_video/123/", "kind": "video", "reference_only": True, "urls": [], "path": ""}
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "raw";raw.mkdir()
            (raw / "fixture.txt").write_text(player, encoding="utf-8")
            post = post_with(item);collector = FixtureCollector(folder, post)
            collector.client = PlayerWayback({player: [{"timestamp": "20210101000000", "original": "https://twitter.com/i/videos/999"}]}, "unused")
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=FixtureCC())
            engine.cached_links([post])
            self.assertEqual(engine.archived_config(post, post["media"][0]), [])
            self.assertEqual(collector.client.reads, [])

    def test_player_snapshots_are_bounded_and_reused_for_separate_video_ids(self):
        player = f"https://x.com/i/videos/{PID}"
        video = "https://video.twimg.com/ext_tw_video/123/pu/vid/good.mp4"
        other = video.replace("/123/", "/456/")
        def item(number):return {"key": f"video.twimg.com/ext_tw_video/{number}/", "kind": "video", "reference_only": True, "urls": [], "path": ""}
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "raw";raw.mkdir()
            (raw / "fixture.txt").write_text(player, encoding="utf-8")
            post = post_with(item(123));collector = FixtureCollector(folder, post)
            rows = [{"timestamp": f"202101{day:02d}000000", "original": player} for day in range(1, 11)]
            collector.client = PlayerWayback({player: rows}, json.dumps({"videos": [video, other]}))
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=FixtureCC())
            engine.cached_links([post])
            first = engine.archived_config(post, post["media"][0])
            second = engine.archived_config(post, item(456))
            self.assertEqual({url for url, evidence in first}, {video})
            self.assertEqual({url for url, evidence in second}, {other})
            self.assertEqual(len(collector.client.reads), 3)
            self.assertEqual(len(collector.client.queries), 2)

    def test_config_failure_still_checks_observed_player(self):
        player = f"https://twitter.com/i/videos/{PID}"
        video = "https://video.twimg.com/ext_tw_video/123/pu/vid/good.mp4"
        item = {"key": "video.twimg.com/ext_tw_video/123/", "kind": "video", "reference_only": True, "urls": [], "path": ""}
        with tempfile.TemporaryDirectory() as folder:
            raw = Path(folder) / "raw";raw.mkdir()
            (raw / "fixture.txt").write_text(player, encoding="utf-8")
            post = post_with(item);collector = FixtureCollector(folder, post)
            collector.client = PlayerWayback({player: [{"timestamp": "20210101000000", "original": player}]}, json.dumps({"url": video}), config_error=True)
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=FixtureCC())
            engine.cached_links([post])
            found = engine.archived_config(post, post["media"][0])
            self.assertEqual([url for url, evidence in found], [video])
            report = post["media"][0]["recovery_checks"][0]
            self.assertEqual(report["status"], "found")
            self.assertEqual([entry["status"] for entry in report["entry_points"]], ["failed", "found"])

    def test_file_reference_downloads_known_gif_video_without_metadata(self):
        url='https://video.twimg.com/tweet_video/Token.mp4'
        item={'key':'video.twimg.com/tweet_video/Token.mp4','kind':'video','reference_only':True,'urls':[url],'path':''}
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with(item))
            http=FixtureHTTP(collector.stop,{url:VIDEO})
            discovery=Mock(side_effect=AssertionError('Known file needs no metadata'))
            engine=RecoveryEngine(collector,http=http,discover=discovery,common_crawl=FixtureCC())
            engine.run()
            self.assertEqual(collector.store.rows[0]['media'][0]['status'],'saved')
            self.assertEqual(http.urls,[url])
            discovery.assert_not_called()

    def test_live_cdn_success_saves_file_hash_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            http = FixtureHTTP(collector.stop, {IMAGE_URL: PNG})
            cc = FixtureCC()
            engine = RecoveryEngine(collector, http=http, discover=no_discovery, common_crawl=cc)
            engine.run()
            item = collector.store.rows[0]["media"][0]
            self.assertEqual(item["status"], "saved")
            self.assertEqual(item["source"], "live_cdn")
            self.assertEqual(item["source_url"], IMAGE_URL)
            self.assertEqual(item["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertEqual((Path(folder) / item["path"]).read_bytes(), PNG)
            self.assertEqual(cc.calls, 0)
            self.assertEqual(engine.stats["recovered"], 1)

    def test_common_crawl_success_keeps_actual_warc_and_query_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            cc = FixtureCC(cc_result())
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=cc)
            engine.run()
            item = collector.store.rows[0]["media"][0]
            self.assertEqual(item["source"], "common_crawl")
            self.assertEqual(item["source_captured_at"], "20200615120000")
            self.assertEqual(item["source_evidence"], cc_result()["evidence_url"])
            self.assertEqual((Path(folder) / item["path"]).read_bytes(), PNG)
            diagnostics = json.dumps(item)
            self.assertIn(cc_result()["retrieved_url"], diagnostics)
            for index in cc.last_scope:
                self.assertIn(index, diagnostics)

    def test_missing_results_are_not_success_and_show_scoped_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            cc = FixtureCC()
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=cc)
            engine.run()
            item = collector.store.rows[0]["media"][0]
            self.assertNotEqual(item["status"], "saved")
            self.assertEqual(engine.stats["recovered"], 0)
            checks = {check["source"]: check for check in item["recovery_checks"]}
            self.assertEqual(checks["live_cdn"]["status"], "not_found")
            self.assertEqual(checks["common_crawl"]["status"], "not_found")
            self.assertIn("CC-MAIN-2020-24", json.dumps(checks["common_crawl"]))
            self.assertEqual(list(Path(folder).rglob("*.part")), [])

    def test_failed_new_download_preserves_existing_original_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "media" / "existing.jpg"
            original.parent.mkdir()
            original.write_bytes(b"original-existing-file")
            post = post_with()
            post["media"][0]["path"] = "media/existing.jpg"
            collector = FixtureCollector(folder, post)
            http = FixtureHTTP(collector.stop, {IMAGE_URL: b"<html>error page</html>"})
            engine = RecoveryEngine(collector, http=http, discover=no_discovery, common_crawl=FixtureCC())
            engine.run()
            self.assertEqual(original.read_bytes(), b"original-existing-file")
            self.assertNotEqual(collector.store.rows[0]["media"][0]["status"], "saved")
            self.assertEqual(list(Path(folder).rglob("*.part")), [])

    def test_cancelled_download_propagates_and_does_not_create_negative_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            http = FixtureHTTP(collector.stop, {IMAGE_URL: Cancelled()})
            engine = RecoveryEngine(collector, http=http, discover=no_discovery, common_crawl=FixtureCC())
            with self.assertRaises(Cancelled):
                engine.run()
            post = collector.store.rows[0]
            item = post["media"][0]
            self.assertFalse(engine.check_cached(post, item))
            self.assertNotEqual(item["status"], "saved")
            self.assertEqual(list(Path(folder).rglob("*.part")), [])

    def test_cancelled_common_crawl_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=FixtureCC(Cancelled()))
            with self.assertRaises(Cancelled):
                engine.run()
            post = collector.store.rows[0]
            self.assertFalse(engine.check_cached(post, post["media"][0]))

    def test_image_format_variants_remain_on_same_media_identifier(self):
        item = media_item("https://pbs.twimg.com/media/Fixture.jpg:large")
        variants = image_variants(item)
        self.assertIn("https://pbs.twimg.com/media/Fixture?format=png&name=orig", variants)
        self.assertTrue(all(related(item, media_item(url)) for url in variants))
        self.assertTrue(related(item, media_item("https://pbs.twimg.com/media/Fixture.png")))
        self.assertFalse(related(item, media_item("https://pbs.twimg.com/media/FixtureOther.jpg")))
        self.assertFalse(related(item, media_item("https://pbs.twimg.com/ext_tw_video_thumb/12/Fixture.jpg")))

    def test_file_reference_matches_exact_token_not_arbitrary_suffix(self):
        item = {"key": "video.twimg.com/tweet_video/Token.mp4", "kind": "video", "reference_only": True}
        self.assertTrue(related(item, media_item("https://video.twimg.com/tweet_video/Token.mp4")))
        self.assertFalse(related(item, media_item("https://video.twimg.com/tweet_video/Token.mp4Other.mp4")))

    def test_current_metadata_only_downloads_related_video(self):
        prefix = "video.twimg.com/ext_tw_video/123/"
        reference = {"key": prefix, "kind": "video", "reference_only": True, "urls": ["https://" + prefix], "path": ""}
        post = post_with(reference)
        video = media_item("https://" + prefix + "pu/vid/640x480/good.mp4")
        video["evidence_url"] = "https://api.fxtwitter.com/alice/status/" + PID
        unrelated = media_item("https://video.twimg.com/ext_tw_video/456/pu/vid/bad.mp4")
        def discover(p, *args, **kwargs):
            p["source_checks"] = {"fxtwitter": {"status": "found", "evidence_url": video["evidence_url"]}}
            return [unrelated, video]
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post)
            http = FixtureHTTP(collector.stop, {video["urls"][0]: VIDEO})
            engine = RecoveryEngine(collector, http=http, discover=discover, common_crawl=FixtureCC())
            engine.run()
            result = collector.store.rows[0]
            item = result["media"][0]
            self.assertEqual(item["source"], "live_cdn")
            self.assertEqual(item["source_evidence"], video["evidence_url"])
            self.assertEqual(item["recovered_video_url"], video["urls"][0])
            self.assertNotIn(unrelated["urls"][0], http.urls)
            self.assertEqual(result["source_checks"]["fxtwitter"]["status"], "found")

    def test_common_crawl_html_is_rejected_before_file_write(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery,
                                    common_crawl=FixtureCC(cc_result(b"<html>404</html>")))
            engine.run()
            item = collector.store.rows[0]["media"][0]
            self.assertNotEqual(item["status"], "saved")
            self.assertEqual(item["recovery_error_kind"], "network")
            self.assertEqual(list(Path(folder).rglob("*.part")), [])

    def test_image_download_cannot_be_marked_saved_with_video_data(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop, {IMAGE_URL: VIDEO}),
                                    discover=no_discovery, common_crawl=FixtureCC(cc_result(VIDEO)))
            engine.run()
            self.assertNotEqual(collector.store.rows[0]["media"][0]["status"], "saved")
            self.assertEqual(engine.stats["recovered"], 0)

    def test_source_http_404_is_reported_as_absent_public_metadata(self):
        post = post_with()
        def not_found(*args, **kwargs):
            raise SourceError("cdn.syndication.twimg.com 请求失败：HTTP 404", 404)
        result = public_discover(post, not_found, lambda: None, lambda message: None, enable_mirrors=True)
        self.assertEqual(result, [])
        self.assertEqual(set(post["source_checks"]), {"syndication", "fxtwitter", "vxtwitter"})
        self.assertTrue(all(check["status"] == "not_found" for check in post["source_checks"].values()))

    def test_failed_network_source_is_retryable_even_if_other_sources_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FixtureCollector(folder, post_with())
            http = FixtureHTTP(collector.stop, {IMAGE_URL: SourceError("fixture 网络中断")})
            engine = RecoveryEngine(collector, http=http, discover=no_discovery, common_crawl=FixtureCC())
            engine.run()
            post = collector.store.rows[0]
            self.assertEqual(post["media"][0]["recovery_error_kind"], "network")
            self.assertFalse(engine.check_cached(post, post["media"][0]))

    def test_missing_previously_saved_file_does_not_remain_saved_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            post = post_with()
            post["media"][0].update(status="saved", path="media/deleted.jpg", bytes=1000)
            collector = FixtureCollector(folder, post)
            engine = RecoveryEngine(collector, http=FixtureHTTP(collector.stop), discover=no_discovery, common_crawl=FixtureCC())
            engine.run()
            self.assertNotEqual(collector.store.rows[0]["media"][0]["status"], "saved")
            self.assertEqual(engine.stats["recovered"], 0)


if __name__ == "__main__":
    unittest.main()
