"""Local SQLite persistence; raw account responses never leave the backend."""
import json
import base64
import hashlib
import os
import sqlite3
import time
import zlib
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


class Store:
    def __init__(self, directory):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path / "monitor.sqlite3", check_same_thread=False)
        os.chmod(path / "monitor.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS history (timestamp INTEGER PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS raw (id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, exchange TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rule_documents (url TEXT PRIMARY KEY, timestamp INTEGER, digest TEXT, body TEXT);
        """)
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, encode(value)))
        self.db.commit()

    def event(self, kind, value, timestamp=None):
        ts = timestamp or int(time.time() * 1000)
        cursor = self.db.execute("INSERT INTO events(timestamp,kind,value) VALUES (?,?,?)", (ts, kind, encode(value)))
        self.db.commit()
        return {**value, "id": cursor.lastrowid, "kind": kind, "timestamp": ts}

    def events(self, limit=150):
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 500),)).fetchall()
        return [{**json.loads(r["value"]), "id": r["id"], "kind": r["kind"], "timestamp": r["timestamp"]} for r in rows]

    def history(self, hours=24):
        if hours is None:
            rows = self.db.execute("SELECT timestamp,value FROM history ORDER BY timestamp").fetchall()
        else:
            cutoff = int(time.time() * 1000) - hours * 3600000
            rows = self.db.execute("SELECT timestamp,value FROM history WHERE timestamp>=? ORDER BY timestamp", (cutoff,)).fetchall()
        stride = max(1, (len(rows) + 1199) // 1200)
        return [{"timestamp": r["timestamp"], **json.loads(r["value"])} for r in rows[::stride]]

    def sample(self, timestamp, value):
        self.db.execute("INSERT OR REPLACE INTO history VALUES (?,?)", (timestamp, encode(value)))
        self.db.commit()

    def raw(self, timestamp, exchange, value):
        # Adapter payloads contain cached large metadata and ledger responses.
        # Store each changed endpoint losslessly, rather than copying the full cache every poll.
        key = "raw_digests:" + exchange
        previous = self.get(key, {})
        digests, changed = dict(previous), {}
        for endpoint, payload in value.items():
            digest = hashlib.sha256(encode(payload).encode()).hexdigest()
            if previous.get(endpoint) != digest:
                changed[endpoint] = payload
                digests[endpoint] = digest
        if not changed:
            return
        encoded = encode(changed)
        if len(encoded) > 2048:
            encoded = encode({"encoding": "zlib+base64", "payload": base64.b64encode(zlib.compress(encoded.encode(), 6)).decode()})
        with self.db:
            self.db.execute("INSERT INTO raw(timestamp,exchange,value) VALUES (?,?,?)", (timestamp, exchange, encoded))
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, encode(digests)))

    @staticmethod
    def decode_raw(value):
        payload = json.loads(value)
        if payload.get("encoding") == "zlib+base64":
            return json.loads(zlib.decompress(base64.b64decode(payload["payload"])))
        return payload

    def prune(self):
        now = int(time.time() * 1000)
        self.db.execute("DELETE FROM raw WHERE timestamp<?", (now - 7 * 86400000,))
        self.db.commit()

    def close(self):
        self.db.close()
