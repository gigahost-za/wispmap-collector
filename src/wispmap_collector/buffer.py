"""Disk buffer: a SQLite queue of gzipped batches (spec doc 03 §8).

Every batch is written here *before* it is sent, and deleted only after the server accepted it.
So a server outage, a network cut or a collector restart never loses data: the batches wait on disk.
"""
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 200 * 1024 * 1024           # 200 MB ...
MAX_AGE_S = 48 * 3600                   # ... or 48 h, whichever comes first


@dataclass(frozen=True)
class Item:
    id: int                             # queue position (SQLite rowid): lower = older
    batch_id: str
    created: float                      # wall clock (time.time) when the batch was made
    body: bytes = b""                   # gzipped JSON


class Buffer:
    def __init__(self, path: str | Path, max_bytes: int = MAX_BYTES, max_age_s: float = MAX_AGE_S,
                 clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_age_s, self.clock = max_bytes, max_age_s, clock
        # isolation_level=None -> autocommit: every statement is on disk when it returns
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")       # crash-safe and cheap on SD cards / flash
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS batches (
                               id       INTEGER PRIMARY KEY AUTOINCREMENT,
                               batch_id TEXT    NOT NULL UNIQUE,
                               created  REAL    NOT NULL,
                               body     BLOB    NOT NULL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")

    # ----- queue -----
    def put(self, batch_id: str, body: bytes, created: float | None = None) -> None:
        self.db.execute("INSERT INTO batches (batch_id, created, body) VALUES (?, ?, ?)",
                        (batch_id, self.clock() if created is None else created, body))

    def oldest(self) -> Item | None:
        return self._one("ORDER BY id ASC")

    def newest(self) -> Item | None:
        return self._one("ORDER BY id DESC")

    def _one(self, order: str) -> Item | None:
        row = self.db.execute(f"SELECT id, batch_id, created, body FROM batches {order} LIMIT 1").fetchone()
        return Item(*row) if row else None

    def delete(self, item_id: int) -> None:
        self.db.execute("DELETE FROM batches WHERE id = ?", (item_id,))

    def depth(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]

    def size_bytes(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM batches").fetchone()[0]

    # ----- caps -----
    def enforce_caps(self) -> int:
        """Drop the oldest batches beyond 48 h / 200 MB. Returns how many were dropped."""
        dropped = self.db.execute("DELETE FROM batches WHERE created < ?",
                                  (self.clock() - self.max_age_s,)).rowcount
        while self.size_bytes() > self.max_bytes:
            oldest = self.oldest()
            if oldest is None:
                break
            self.delete(oldest.id)
            dropped += 1
        if dropped:
            self.db.execute("INSERT INTO meta (key, value) VALUES ('dropped', ?) "
                            "ON CONFLICT (key) DO UPDATE SET value = value + excluded.value", (dropped,))
        return dropped

    @property
    def dropped(self) -> int:
        """Batches lost to the caps since this buffer file was created (reported to the server)."""
        row = self.db.execute("SELECT value FROM meta WHERE key = 'dropped'").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self.db.close()
