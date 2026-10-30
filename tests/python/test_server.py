"""Local HTTP contract: static UI, collection export, range seeking, and request boundaries."""
import http.client
from contextlib import contextmanager
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import app
from shiguangji.collect.archive import Collector, make_zip
from test_archive import FixtureClient

class ServerTests(unittest.TestCase):
    def test_storage_settings_require_token_and_never_start_collection(self):
        from shiguangji.storage.storage_policy import settings,save_settings
        old=settings(self.folder)
        try:
            body=json.dumps({'zip_on_demand':False,'cache_gb':1})
            with patch.object(app.TASKS,'start') as start:
                self.assertEqual(self.request('/api/storage/settings','POST',{},body)[0],403)
                code,_,data=self.request('/api/storage/settings','POST',{'X-Local-Token':app.TOKEN},body)
                self.assertEqual(code,200);self.assertFalse(json.loads(data)['storage']['settings']['zip_on_demand'])
                start.assert_not_called()
            body=json.dumps({'operation':'cleanup','plan_id':'x'})
            self.assertEqual(self.request('/api/storage/action','POST',{'X-Local-Token':app.TOKEN},body)[0],400)
        finally:save_settings(self.folder,old)

    def test_cloud_configuration_is_authenticated_and_does_not_start_collection(self):
        from shiguangji.storage.cloud_backup import CloudBackup
        service = CloudBackup(self.folder)
        body = json.dumps({'enabled':True,'destination':str(self.folder.parent/'cloud-backup-test')})
        headers = {'X-Local-Token':app.TOKEN,'Content-Type':'application/json'}
        with patch.object(app,'CLOUD',service), patch.object(app.TASKS,'start') as start:
            self.assertEqual(self.request('/api/cloud-backup','POST',{},body)[0],403)
            self.assertEqual(self.request('/api/cloud-backup','POST',headers,body)[0],200)
            state = json.loads(self.request('/api/state')[2])
            self.assertTrue(state['cloud_backup']['enabled'])
            self.assertTrue(state['reliability']['backup_after_task'])
            start.assert_not_called()

    def test_first_batch_without_offline_export_disables_offline_link_and_invalidates_etag(self):
        folder = self.folder/'alice'
        page = folder/'index.html'
        content = page.read_bytes()
        try:
            page.unlink()
            status, headers, body = self.request('/api/collection/alice?view=summary')
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)['offline_ready'])
            page.write_bytes(content)
            status, _, body = self.request('/api/collection/alice?view=summary', headers={'If-None-Match':headers['ETag']})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['offline_ready'])
        finally:
            page.write_bytes(content)

    def test_paginated_detail_and_posts_contract(self):
        status,headers,raw=self.request('/api/collection/alice?view=summary')
        self.assertEqual(status,200)
        summary=json.loads(raw);self.assertEqual(summary['posts'],[])
        self.assertIn('content_revision',summary)
        self.assertEqual(self.request('/api/collection/alice?view=summary',headers={'If-None-Match':headers['ETag']})[0],304)
        page=json.loads(self.request('/api/posts/alice?limit=1&revision='+summary['content_revision'])[2])
        self.assertEqual(len(page['posts']),1);self.assertEqual(page['total'],summary['counts']['posts'])
        self.assertTrue(json.loads(self.request('/api/posts/alice?revision=outdated')[2])['stale'])
        self.assertEqual(self.request('/api/posts/alice?limit=bad')[0],400)
        self.assertEqual(self.request('/api/posts/nobody')[0],404)

    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.folder=Path(cls.temp.name)
        Collector('alice',base=cls.folder,client=FixtureClient()).run();make_zip(cls.folder/'alice')
        cls.data_patch=patch.object(app.TASKS, 'base',cls.folder);cls.data_patch.start()
        cls.server=app.LocalServer(('127.0.0.1',0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.port=cls.server.server_port
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.data_patch.stop();cls.temp.cleanup()
    def request(self,path,method='GET',headers=None,body=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        c.request(method,path,body=body,headers=headers or {});r=c.getresponse();status=r.status;h=dict(r.getheaders());data=r.read();c.close();return status,h,data
    def test_history_names_api_and_job_constraints(self):
        headers={'X-Local-Token':app.TOKEN,'Content-Type':'application/json'}
        with patch.dict(app.TASKS.job, running=False), patch.object(app.TASKS, 'collection_busy', return_value=False):
            state=json.loads(self.request('/api/state')[2])
            self.assertIn('account_identity', state['capabilities'])
            self.assertEqual(self.request('/api/identity/alice')[0],200)
            payload={'username':'alice','alias':'oldalice','state':'candidate'}
            self.assertEqual(self.request('/api/identity','POST',headers,json.dumps(payload))[0],200)
            result=json.loads(self.request('/api/identity/alice')[2])
            self.assertEqual(result['aliases'][0]['state'],'candidate')
            self.assertEqual(self.request('/api/identity','POST',{},json.dumps(payload))[0],403)
            for override in ({'alias':'../outside'},{'alias':'alice'},{'state':'accept_unverified'}):
                self.assertEqual(self.request('/api/identity','POST',headers,json.dumps({**payload,**override}))[0],400)
            with patch.dict(app.TASKS.job,running=True):
                self.assertEqual(self.request('/api/identity','POST',headers,json.dumps(payload))[0],409)
            with patch.object(app.TASKS, 'start') as start:
                self.assertEqual(self.request('/api/start','POST',headers,json.dumps({'username':'alice','mode':'identity','auto_continue':True}))[0],200)
                self.assertFalse(start.call_args.args[1]['auto_continue'])
            self.request('/api/identity','POST',headers,json.dumps({**payload,'state':'auto'}))

    def test_ui_and_manifest(self):
        status,h,body=self.request('/');self.assertEqual(status,200);self.assertIn('时光集',body.decode());self.assertIn('Content-Security-Policy',h)
        status,_,body=self.request('/api/collection/alice');self.assertEqual(status,200);self.assertTrue(json.loads(body)['zip_ready'])
        for name in ('style.css','gallery.js','data.js','index.html'):
            self.assertEqual(self.request('/collections/alice/'+name)[0],200)
    def test_zip_and_video_range(self):
        status,h,body=self.request('/download/alice');self.assertEqual(status,200);self.assertTrue(body.startswith(b'PK'));self.assertIn('attachment',h['Content-Disposition'])
        manifest=json.loads((self.folder/'alice/manifest.json').read_text(encoding='utf-8'))
        path='/collections/alice/'+manifest['posts'][0]['media'][0]['path']
        status,h,body=self.request(path,headers={'Range':'bytes=0-3'});self.assertEqual(status,206);self.assertEqual(len(body),4);self.assertIn('bytes 0-3/',h['Content-Range'])
        self.assertEqual(self.request(path,headers={'Range':'bytes=999999-1000000'})[0],416)

    def test_cloud_cache_route_preserves_mime_range_and_read_lease(self):
        cache=self.folder/'test-cloud.blob';cache.write_bytes(b'0123456789')
        leased=[]
        @contextmanager
        def media(*args):
            leased.append(True)
            try:yield cache,{'mime':'video/mp4'},True
            finally:leased.clear()
        try:
            with patch.object(app,'open_media',side_effect=media):
                status,headers,data=self.request('/api/cloud-media/alice/media/example.mp4',headers={'Range':'bytes=2-5'})
            self.assertEqual(status,206);self.assertEqual(data,b'2345')
            self.assertEqual(headers['Content-Type'],'video/mp4')
            self.assertEqual(headers['Content-Range'],'bytes 2-5/10')
            # The handler releases the read lease right after writing the body, so the
            # client can finish reading first; wait (bounded) for the release.
            deadline=time.monotonic()+5
            while leased and time.monotonic()<deadline:time.sleep(0.01)
            self.assertFalse(leased)
            with patch.object(app,'open_media',side_effect=TimeoutError('cloud read timeout')):
                self.assertEqual(self.request('/api/cloud-media/alice/media/example.mp4')[0],503)
        finally:cache.unlink()
    def test_external_origins_and_paths_rejected(self):
        self.assertEqual(self.request('/api/state',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('/api/start','POST',{'Content-Type':'application/json'},'{}')[0],403)
        self.assertEqual(self.request('/api/start','POST',{'X-Local-Token':app.TOKEN,'Origin':'https://evil.test','Content-Type':'application/json'},'{}')[0],403)
        self.assertEqual(self.request('/collections/../app.py')[0],403)
        self.assertEqual(self.request('/collections/alice/archive.sqlite')[0],403)
    def test_invalid_username_rejected(self):
        status,_,body=self.request('/api/start','POST',{'X-Local-Token':app.TOKEN,'Content-Type':'application/json'},json.dumps({'username':'../oops'}))
        self.assertEqual(status,400)
    def test_only_one_server_can_own_port(self):
        with self.assertRaises(OSError):
            server=app.LocalServer(('127.0.0.1',self.port),app.Handler)
            server.server_close()
        self.assertEqual(app.running_instance(self.port)['app'],'x-wayback-library')

    def test_desktop_launcher_advertised_only_when_installed_for_this_root(self):
        for installed in (False, True):
            with self.subTest(installed=installed), patch.object(app, 'protocol_available', return_value=installed) as available:
                state = json.loads(self.request('/api/state')[2])
                self.assertEqual('desktop_launcher' in state['capabilities'], installed)
                available.assert_called_once_with(app.ROOT)

    def test_external_cli_progress_replaces_stale_job_counts(self):
        manifest = json.loads((self.folder/'alice'/'manifest.json').read_text(encoding='utf-8'))
        with patch.dict(app.TASKS.job, username='alice', running=False, stage='done', status='complete', counts={'posts': 0}), \
             patch.object(app.TASKS, 'collection_busy', return_value=True):
            app.TASKS.refresh_external_job()
            self.assertEqual(app.TASKS.job['stage'], 'external')
            self.assertEqual(app.TASKS.job['status'], 'running')
            self.assertEqual(app.TASKS.job['counts'], manifest['counts'])

    def test_multi_source_contract_requires_existing_collection_and_boolean_options(self):
        headers={'X-Local-Token':app.TOKEN,'Content-Type':'application/json'}
        with patch.object(app.TASKS, 'start') as start:
            payload={'username':'alice','mode':'rescue','common_crawl':True,'public_mirrors':False,'cc_indexes':4}
            self.assertEqual(self.request('/api/start','POST',headers,json.dumps(payload))[0],200)
            handle,options,_=start.call_args.args
            self.assertEqual(handle,'alice')
            self.assertEqual(options['mode'],'rescue')
            self.assertFalse(options['public_mirrors'])
            self.assertEqual(options['cc_indexes'],4)
            start.reset_mock()
            self.assertEqual(options['max_minutes'], 10)
            self.assertEqual(options['max_requests'], 200)
            for override in ({'username':'nonexistent'},{'common_crawl':'false'},{'multi_source':1},{'cc_indexes':13},
                             {'max_minutes':0},{'max_minutes':31},{'max_requests':0},{'max_requests':651}):
                with self.subTest(override=override):
                    self.assertEqual(self.request('/api/start','POST',headers,json.dumps({**payload,**override}))[0],400)
            start.assert_not_called()

    def test_discover_supports_new_accounts_and_advertises_capability(self):
        self.assertIn('post_discovery', json.loads(self.request('/api/state')[2])['capabilities'])
        with patch.object(app.TASKS, 'start') as start:
            status, _, _ = self.request('/api/start', 'POST', {'X-Local-Token': app.TOKEN, 'Content-Type': 'application/json'},
                                         json.dumps({'username': 'newaccount', 'mode': 'discover'}))
            self.assertEqual(status, 200)
            self.assertEqual(start.call_args.args[1]['mode'], 'discover')

    def test_manifest_conditional_get_changes_with_download_availability(self):
        _, headers, _ = self.request('/api/collection/alice')
        conditional = {'If-None-Match': headers['ETag']}
        status, _, body = self.request('/api/collection/alice', headers=conditional)
        self.assertEqual((status, body), (304, b''))
        with patch.dict(app.TASKS.job, running=True, username='alice'):
            status, h, body = self.request('/api/collection/alice', headers=conditional)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)['zip_ready'])
            self.assertNotEqual(h['ETag'], headers['ETag'])

    def test_stale_zip_is_not_served_as_latest(self):
        path = self.folder/'alice/manifest.json'
        previous = path.read_bytes()
        try:
            path.write_bytes(previous + b'\n')
            self.assertEqual(self.request('/download/alice')[0], 409)
            self.assertFalse(json.loads(self.request('/api/collection/alice')[2])['zip_ready'])
        finally:
            path.write_bytes(previous)

    def test_collection_identity_is_live_even_when_manifest_was_exported_before_alias_evidence(self):
        from shiguangji.platforms.account_identity import Catalog
        from shiguangji.collect.archive import Store, export_collection
        from shiguangji.platforms.extractor import blank_post
        with tempfile.TemporaryDirectory(dir=self.folder) as temp, patch.object(app.TASKS, 'base',Path(temp)):
            base=Path(temp)
            store=Store(base/'newname','newname')
            store.put_post(blank_post('100','newname'))
            export_collection(store,'partial',[])
            store.close()
            manifest=(base/'newname/manifest.json').read_bytes()
            _,old_headers,body=self.request('/api/collection/newname')
            self.assertEqual(json.loads(body)['identity']['aliases'],[])
            catalog=Catalog(base)
            try:
                for name in ('newname','oldname'):
                    catalog.record('oldname',name,f'<i data-screen-name="{name}" data-user-id="123"></i>',
                                   '20200101','https://twitter.com/'+name)
                status,headers,body=self.request('/api/collection/newname',headers={'If-None-Match':old_headers['ETag']})
                self.assertEqual(status,200)
                self.assertNotEqual(headers['ETag'],old_headers['ETag'])
                self.assertEqual(json.loads(body)['identity']['aliases'][0]['state'],'verified')
                self.assertEqual(json.loads(self.request('/api/collections')[2])[0]['identity']['aliases'][0]['username'],'oldname')
                self.assertEqual(self.request('/api/collection/newname',headers={'If-None-Match':headers['ETag']})[0],304)
                catalog.choice('newname','oldname','excluded')
                status,_,body=self.request('/api/collection/newname',headers={'If-None-Match':headers['ETag']})
                self.assertEqual(status,200)
                self.assertEqual(json.loads(body)['identity']['aliases'][0]['state'],'excluded')
                self.assertEqual((base/'newname/manifest.json').read_bytes(),manifest)
            finally:
                catalog.close()

    def test_pause_is_idempotent_and_survives_poll(self):
        headers = {'X-Local-Token': app.TOKEN, 'Content-Type': 'application/json'}
        with patch.dict(app.TASKS.job, running=True, username='alice', stage='media', pause_requested=False, logs=[]), patch.object(app.TASKS, 'stop', threading.Event()):
            self.assertEqual(self.request('/api/pause', 'POST', headers, '{}')[0], 200)
            self.assertTrue(json.loads(self.request('/api/state')[2])['job']['pause_requested'])
            self.assertTrue(app.TASKS.stop.is_set())
            self.assertEqual(self.request('/api/pause', 'POST', headers, '{}')[0], 200)
            self.assertEqual(len(app.TASKS.job['logs']), 1)
            app.TASKS.job['stage'] = 'zip'
            self.assertEqual(self.request('/api/pause', 'POST', headers, '{}')[0], 409)
        self.assertEqual(self.request('/api/pause', 'POST', headers, '{}')[0], 400)

    def test_zip_only_preserves_partial_recovery_status(self):
        path = self.folder/'alice/manifest.json'
        previous = path.read_bytes()
        try:
            manifest = json.loads(previous)
            manifest['status'] = 'partial'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            with patch.dict(app.TASKS.job, logs=[], running=True):
                app.TASKS.worker('alice', {}, zip_only=True)
                self.assertEqual(app.TASKS.job['status'], 'partial')
                self.assertFalse(app.TASKS.job['running'])
        finally:
            path.write_bytes(previous)
            make_zip(self.folder/'alice')

    def test_non_ascii_token_is_rejected_with_403(self):
        body=json.dumps({'username':'alice'})
        for token in ('\u00e9'*8, app.TOKEN[:-1]+'\u00e9', '\u00ff'):
            with self.subTest(token=token):
                status,_,data=self.request('/api/start','POST',{'X-Local-Token':token,'Content-Type':'application/json'},body)
                self.assertEqual(status,403)
                self.assertIn('error',json.loads(data))
        self.assertFalse(app.token_matches('\u00e9'))
        self.assertFalse(app.token_matches('\ud800'))
        self.assertFalse(app.token_matches(None))
        self.assertTrue(app.token_matches(app.TOKEN))

    def test_unexpected_post_error_returns_generic_500_and_logs(self):
        headers={'X-Local-Token':app.TOKEN,'Content-Type':'application/json'}
        with patch.object(app,'save_settings',side_effect=RuntimeError('secret internal detail')), \
                self.assertLogs('shiguangji.app','ERROR') as logged:
            status,_,data=self.request('/api/reliability','POST',headers,json.dumps({}))
        self.assertEqual(status,500)
        payload=json.loads(data)
        self.assertNotIn('secret internal detail',payload['error'])
        self.assertIn('secret internal detail','\n'.join(logged.output))
        self.assertEqual(self.request('/api/state')[0],200)

if __name__=='__main__':unittest.main()
