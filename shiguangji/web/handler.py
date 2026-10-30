"""HTTP server and request handler for the loopback-only interface.

The handler validates Host, token and Origin, parses bodies, maps errors to status codes
and dispatches to the route tables. Application state is read from ``env`` (the
``app.py`` module) at request time.
"""
import json
import logging
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from shiguangji.core.app_version import VERSION
from shiguangji.storage.collection_lock import CollectionBusy
from shiguangji.storage.storage_guard import StorageLow
from shiguangji.web import routes_get, routes_post
from shiguangji.web.files import send_file
from shiguangji.web.security import local_origins, valid_host

LOG = logging.getLogger("shiguangji.app")
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
DISCONNECTED = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two servers bind the same port. Require one owner.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "XWaybackLibrary/" + VERSION
    env = None  # the application module, set by the entry script's subclass

    def log_message(self, fmt, *args):
        pass

    def valid_host(self):
        return valid_host(self.headers.get("Host", ""), self.server.server_port)

    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)

    def json(self, value, code=200, etag=""):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "private, no-cache" if etag else "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def not_modified(self, etag):
        self.send_response(304)
        self.headers_common()
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "private, no-cache")
        self.end_headers()

    def file(self, path, attachment="", mime=None):
        return send_file(self, path, attachment, mime)

    def do_GET(self):
        try:
            self.get()
        except DISCONNECTED:
            pass
        except Exception as e:
            self.json({"error": str(e)}, 500)

    def get(self):
        if not self.valid_host():
            return self.json({"error": "仅允许本机访问。"}, 403)
        path = unquote(urlsplit(self.path).path)
        return routes_get.route(path)(self, self.env, path)

    def do_POST(self):
        try:
            self.post()
        except DISCONNECTED:
            pass
        except Exception:
            LOG.exception("Unhandled error in POST %s", urlsplit(self.path).path)
            try:
                self.json({"error": "本地服务内部错误，请稍后重试。"}, 500)
            except OSError:
                pass

    def post(self):
        env = self.env
        if not self.valid_host() or not env.token_matches(self.headers.get("X-Local-Token", "")):
            return self.json({"error": "请刷新本地页面后重试。"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in local_origins(self.server.server_port):
            return self.json({"error": "请求来源不受支持。"}, 403)
        try:
            body = self.read_body()
            response = routes_post.dispatch(env, self.path, body)
            payload, code = response if response is not None else ({"ok": True}, 200)
            return self.json(payload, code)
        except StorageLow as e:
            return self.json({'error':str(e)}, 507)
        except CollectionBusy as e:
            return self.json({"error": str(e)}, 409)
        except OSError as e:
            if self.path.startswith(routes_post.QUEUE_PREFIX):
                with env.TASKS.lock:
                    env.TASKS.task_queue().data.update(state='paused', reason='storage_error')
            return self.json({'error':'本地记录暂时无法保存，请检查磁盘和文件占用后重试：'+str(e)[:200]}, 503)
        except (ValueError, TypeError) as e:
            return self.json({"error": str(e)}, 400)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= routes_post.body_limit(self.path):
            raise ValueError("请求大小无效。")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("请求内容应为对象。")
        return body
