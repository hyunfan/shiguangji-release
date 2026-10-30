"""Route tables and helpers of the shiguangji.web package (without a running server)."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from shiguangji.tasks.task_service import TaskService
from shiguangji.web import files, routes_get, routes_post
from shiguangji.web.security import local_origins, tokens_equal, valid_host


class WebRouteTests(unittest.TestCase):
    def test_get_routes_prefer_exact_paths_and_fall_back_to_static(self):
        self.assertIs(routes_get.route('/api/state'), routes_get.state)
        self.assertIs(routes_get.route('/api/collections'), routes_get.collections)
        self.assertIs(routes_get.route('/collections/alice/a.jpg'), routes_get.collection_files)
        self.assertIs(routes_get.route('/api/collection/alice'), routes_get.collection)
        self.assertIs(routes_get.route('/index.html'), routes_get.static)
        self.assertIs(routes_get.route('/api/unknown'), routes_get.static)

    def test_post_dispatch_rejects_unknown_paths_and_queue_actions(self):
        tasks = SimpleNamespace(lock=threading.RLock(), stopping=threading.Event(), task_queue=lambda: object())
        env = SimpleNamespace(TASKS=tasks)
        self.assertEqual(routes_post.dispatch(env, '/api/nope', {}), ({'error': '未找到操作。'}, 404))
        self.assertEqual(routes_post.dispatch(env, '/api/queue/nope', {}), ({'error': '未找到队列操作。'}, 404))

    def test_queue_paths_get_the_larger_body_limit(self):
        self.assertEqual(routes_post.body_limit('/api/queue/add'), 65536)
        self.assertEqual(routes_post.body_limit('/api/start'), 8192)

    def test_security_helpers(self):
        self.assertTrue(tokens_equal('abc', 'abc'))
        self.assertFalse(tokens_equal('åbc', 'abc'))
        self.assertTrue(valid_host('127.0.0.1:8765', 8765))
        self.assertFalse(valid_host('evil.example:8765', 8765))
        self.assertIn('http://127.0.0.1:8765', local_origins(8765))

    def test_range_parsing(self):
        self.assertEqual(files.parse_range('', 10), (0, 9, False))
        self.assertEqual(files.parse_range('bytes=2-4', 10), (2, 4, True))
        self.assertEqual(files.parse_range('bytes=-3', 10), (7, 9, True))
        with self.assertRaises(files.RangeNotSatisfiable):
            files.parse_range('bytes=20-', 10)

    def test_collection_active_combines_running_job_and_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            service = TaskService(Path(folder), collector_for=lambda handle: None, options_for=lambda body: body)
            service.job.update(running=True, username='alice')
            self.assertTrue(service.collection_active('alice'))
            self.assertFalse(service.collection_active('bob'))
            service.collection_busy = lambda key: key == 'bob-key'
            self.assertTrue(service.collection_active('bob', 'bob-key'))


if __name__ == '__main__':
    unittest.main()
