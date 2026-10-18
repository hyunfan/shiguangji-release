import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from public_sources import discover
from extractor import blank_post,merge_post
from recovery_report import report


class PostRecoveryTests(unittest.TestCase):
    def test_media_checks_do_not_refresh_expired_text_lookup(self):
        from post_recovery import recent_check
        from datetime import datetime,timezone
        current=1800000000
        stamp=lambda seconds:datetime.fromtimestamp(seconds,timezone.utc).isoformat()
        p={'public_text_checked_at':stamp(current-86401),
           'source_checks':{'public':{'status':'not_found','checked_at':stamp(current)}}}
        with patch('post_recovery.time.time',return_value=current):
            self.assertFalse(recent_check(p))
            p['public_text_checked_at']=stamp(current-60)
            self.assertTrue(recent_check(p))
            self.assertFalse(recent_check(p,True))
            p['source_checks']['public']['status']='unavailable'
            self.assertFalse(recent_check(p))

    def test_more_than_one_batch_keeps_unvisited_text_pending(self):
        from archive import Collector
        from post_recovery import recover_posts
        from collection_export import export_collection
        with tempfile.TemporaryDirectory() as d:
            c=Collector('alice',{'multi_source':True},base=Path(d))
            try:
                for i in range(13):c.store.save_post(blank_post(str(100+i),'alice'))
                def found(post,*a,**k):post.update(text='verified',text_source='public_metadata');return []
                with patch('post_recovery.discover',side_effect=found):
                    recover_posts(c)
                    result=export_collection(c.store,'partial',[],full=False,run_info=c.run_info)
                    self.assertEqual(result['counts']['text'],12)
                    self.assertEqual(result['counts']['text_recovery_pending'],1)
                    recover_posts(c)
                    self.assertEqual(c.run_info['post_recovery']['pending'],0)
                    self.assertEqual(sum(bool(p['text']) for p in c.store.posts()),13)
            finally:c.store.close()

    def test_public_text_requires_exact_author_and_preserves_historical_priority(self):
        for author,expected in [('alice',True),('bob',False)]:
            p=blank_post('123','alice')
            payload={'id_str':'123','user':{'screen_name':author},'text':'verified text'}
            discover(p,lambda url,limit,headers:(json.dumps(payload).encode(),{'url':url,'content_type':'application/json'}),lambda:None,lambda _:None,recover_text=True)
            self.assertEqual(bool(p['text']),expected)
            if expected:
                self.assertEqual(p['text_provenance']['scope'],'current_public')
                historical=blank_post('123','alice');historical.update(text='original historical text',text_source='json')
                merge_post(p,historical)
                self.assertEqual(p['text'],'original historical text');self.assertNotIn('text_provenance',p)

    def test_restricted_and_existing_text_not_replaced(self):
        p=blank_post('123','alice');p['content_reason']='restricted'
        def fail(*a,**k):raise AssertionError('Restricted post fetched')
        discover(p,fail,lambda:None,lambda _:None,recover_text=True)
        self.assertFalse(p['text'])

    def test_known_numeric_author_never_falls_back_to_same_handle(self):
        for author in ({'screen_name':'alice'},{'screen_name':'alice','id_str':'999'}):
            p=blank_post('123','alice');p['author_ids']=['888']
            payload={'id_str':'123','user':author,'text':'unverified text'}
            discover(p,lambda url,limit,headers:(json.dumps(payload).encode(),{'url':url,'content_type':'application/json'}),lambda:None,lambda _:None,recover_text=True)
            self.assertFalse(p['text'])

    def test_missing_text_categories_distinguish_unread_from_unparsed(self):
        posts=[blank_post(str(n),'alice') for n in range(1,5)];posts[-1]['content_reason']='restricted'
        result=report(posts,[{'original':posts[0]['url'],'status':'failed'},{'original':posts[1]['url'],'status':'done'}])
        self.assertEqual(result['text'],{'page_not_read':1,'needs_parsing_or_other_snapshot':1,'no_capture':1,'restricted':1})
