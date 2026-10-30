"""SQLite collection repository; legacy schema and directories are preserved."""
import json
import sqlite3
from pathlib import Path
from shiguangji.core.archive_support import INDEX_POLICY_VERSION, now, replay
from shiguangji.platforms.extractor import belongs, blank_post, merge_post, post_id

class Store:
    def __init__(self, folder, handle):
        self.folder, self.handle = Path(folder), handle
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder / "archive.sqlite")
        self.db.row_factory = sqlite3.Row
        self._batch = False
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS captures(timestamp TEXT, original TEXT, mimetype TEXT, digest TEXT,
                status TEXT DEFAULT 'pending', error TEXT DEFAULT '', PRIMARY KEY(timestamp,original));
            CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS queries(url TEXT PRIMARY KEY, updated TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS index_progress(url TEXT PRIMARY KEY, resume_key TEXT, updated TEXT, completed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS raw_checks(name TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, parser_version INTEGER);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS captures_status ON captures(status);
            CREATE INDEX IF NOT EXISTS captures_original_digest ON captures(original,digest,status);
        """)
        if 'completed' not in {r[1] for r in self.db.execute('PRAGMA table_info(index_progress)')}:
            self.db.execute('ALTER TABLE index_progress ADD COLUMN completed INTEGER DEFAULT 0')
            self.db.commit()
        # A cursor belongs to its query filters. Never resume the old 200-only
        # scan after adding WARC resource records (whose HTTP status is '-').
        policy = self.db.execute("SELECT value FROM settings WHERE key='index_policy'").fetchone()
        if not policy or policy[0] != INDEX_POLICY_VERSION:
            with self.db:
                self.db.execute('DELETE FROM index_progress')
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('index_policy',?)", (INDEX_POLICY_VERSION,))
    def add_captures(self, records, index_url=None, next_key="", handles=None):
        # One transaction for the records AND cursor: interruption cannot skip an unsaved page.
        with self.db:
            self._batch = True
            try:
                for r in records:
                    if any(belongs(r["original"], h) for h in (handles or [self.handle])):
                        self.db.execute("INSERT OR IGNORE INTO captures(timestamp,original,mimetype,digest) VALUES(?,?,?,?)", tuple(r[k] for k in ("timestamp", "original", "mimetype", "digest")))
                        pid = post_id(r["original"], self.handle)
                        if pid:
                            p = blank_post(pid, self.handle)
                            p["captures"] = [{"timestamp": r["timestamp"], "url": r["original"], "archive_url": replay(r["timestamp"], r["original"], raw=False)}]
                            self.put_post(p)
                if index_url is not None:
                    self.db.execute("INSERT OR REPLACE INTO index_progress VALUES(?,?,?,?)", (index_url, next_key, now(), int(not next_key)))
            finally:
                self._batch = False
    def put_post(self, p):
        row = self.db.execute("SELECT data FROM posts WHERE id=?", (p["id"],)).fetchone()
        initial = blank_post(p["id"], p.get("username", self.handle))
        if p.get("platform") == "instagram":
            initial.update(url=p["url"], platform="instagram", shortcode=p.get("shortcode", ""), created_at="", date_source="unknown")
        merged = merge_post(json.loads(row[0]), p) if row else merge_post(initial, p)
        self.save_post(merged)
    def put_posts(self, posts):
        with self.db:
            self._batch = True
            try:
                for p in posts:
                    self.put_post(p)
            finally:
                self._batch = False
    def save_post(self, p):
        self.db.execute("INSERT INTO posts VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data WHERE posts.data!=excluded.data", (p["id"], json.dumps(p, ensure_ascii=False)))
        if not self._batch:
            self.db.commit()
    def posts(self):
        result = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM posts")]
        return sorted(result, key=lambda p: (p["created_at"] or "9999", int(p["id"])))
    def close(self):
        self.db.close()
        if hasattr(self, "identity_catalog"):
            self.identity_catalog.close()
