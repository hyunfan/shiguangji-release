"""Retry unavailable sources without repeating confirmed empty results."""
import tempfile
import unittest
from unittest.mock import Mock
from archive import ArchiveError
from media_recovery import RecoveryEngine
from source_http import SourceError
from test_media_recovery import FixtureCollector, FixtureHTTP, FixtureCC, post_with, no_discovery, IMAGE_URL


class SourceRetryTests(unittest.TestCase):
    def test_duplicate_evidence_does_not_repeat_download_or_change_negative_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            http=FixtureHTTP(collector.stop)
            engine=RecoveryEngine(collector,http=http,discover=no_discovery,common_crawl=FixtureCC())
            post=collector.store.rows[0];item=post['media'][0]
            engine.direct(post,item,[(IMAGE_URL,'post'),(IMAGE_URL,'local-cache'),
                                    (IMAGE_URL.replace('https://','http://'),'old-cache')])
            self.assertEqual(http.requests,1)
            engine.direct(post,item,[(IMAGE_URL,'new-metadata-evidence')])
            self.assertEqual(http.requests,1)
            collector.options['force_recheck']=True
            engine.direct(post,item,[(IMAGE_URL,'new-metadata-evidence')])
            self.assertEqual(http.requests,2)

    def test_metadata_duplicate_of_failed_network_url_does_not_become_negative_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            http=FixtureHTTP(collector.stop,{IMAGE_URL:SourceError('network failure')})
            engine=RecoveryEngine(collector,http=http,discover=no_discovery,common_crawl=FixtureCC())
            post=collector.store.rows[0];item=post['media'][0];attempted={}
            engine.direct(post,item,[(IMAGE_URL,'post')],attempted)
            engine.direct(post,item,[(IMAGE_URL,'metadata'),(IMAGE_URL+'?name=small','metadata')],attempted)
            self.assertEqual(http.requests,2)
            report=next(row for row in item['recovery_checks'] if row['source']=='live_cdn')
            self.assertEqual(report['status'],'failed')
            self.assertEqual(item['recovery_error_kind'],'network')
            engine.direct(post,item,[(IMAGE_URL,'post')])
            self.assertEqual(http.requests,3)

    def test_cc_failure_retries_cc_but_reuses_known_cdn_and_wayback_absence(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            collector.client.cdx=Mock(return_value=[[]])
            first=FixtureHTTP(collector.stop)
            RecoveryEngine(collector,http=first,discover=no_discovery,common_crawl=FixtureCC(ArchiveError('503'))).run()
            self.assertGreater(first.requests,0)
            self.assertEqual(collector.client.cdx.call_count,1)
            second=FixtureHTTP(collector.stop)
            cc=FixtureCC(ArchiveError('503'))
            RecoveryEngine(collector,http=second,discover=no_discovery,common_crawl=cc).run()
            self.assertEqual(second.requests,0)
            self.assertEqual(cc.calls,1)
            self.assertEqual(collector.client.cdx.call_count,1)

    def test_new_candidate_or_force_recheck_invalidates_negative_source_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            collector=FixtureCollector(folder,post_with())
            http=FixtureHTTP(collector.stop)
            engine=RecoveryEngine(collector,http=http,discover=no_discovery,common_crawl=FixtureCC())
            post=collector.store.rows[0];item=post['media'][0]
            engine.direct(post,item,[(IMAGE_URL,post['url'])])
            self.assertEqual(http.requests,1)
            engine.direct(post,item,[(IMAGE_URL,post['url'])])
            self.assertEqual(http.requests,1)
            engine.direct(post,item,[(IMAGE_URL+'?name=small',post['url'])])
            self.assertEqual(http.requests,2)
            collector.options['force_recheck']=True
            engine.direct(post,item,[(IMAGE_URL+'?name=small',post['url'])])
            self.assertEqual(http.requests,3)


if __name__=='__main__':unittest.main()
