"""SQLite access. One connection per thread, WAL mode, autocommit."""
import os
import sqlite3
import threading

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id        INTEGER PRIMARY KEY,
    relpath   TEXT NOT NULL UNIQUE,
    sender    TEXT NOT NULL,
    kind      TEXT NOT NULL,            -- image | video
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    fp        TEXT NOT NULL,            -- quick content fingerprint
    taken_at  TEXT,                     -- naive local ISO timestamp
    duration  REAL,
    state     TEXT NOT NULL DEFAULT 'new',   -- new | ready | error
    error     TEXT,
    hidden    TEXT,                     -- NULL | duplicate | live | missing
    dup_of    INTEGER,
    vprev     INTEGER NOT NULL DEFAULT 0,    -- 0 pending, 1 transcoded, 2 play original, 3 failed
    sharp     REAL,                     -- sharpness of the sharpest region (higher = sharper)
    lum       REAL,                     -- mean brightness 0-255
    contrast  REAL,
    phash     TEXT,                     -- 64-bit perceptual hash, hex
    burst     INTEGER                   -- id shared by look-alike shots taken together
);
CREATE INDEX IF NOT EXISTS media_fp ON media(fp);
CREATE TABLE IF NOT EXISTS ratings (
    media_id   INTEGER NOT NULL,
    user       TEXT NOT NULL,
    score      INTEGER,
    star       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (media_id, user)
);
CREATE TABLE IF NOT EXISTS exports (
    media_id    INTEGER NOT NULL,
    target      TEXT NOT NULL,          -- immich | folder
    ref         TEXT,
    exported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (media_id, target)
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=60, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
        _local.path = config.DB_PATH
    return c


ADDED_COLUMNS = {"sharp": "REAL", "lum": "REAL", "contrast": "REAL", "phash": "TEXT", "burst": "INTEGER"}


def init() -> None:
    c = conn()
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("PRAGMA table_info(media)")}
    for name, kind in ADDED_COLUMNS.items():  # databases created by an older version
        if name not in have:
            c.execute(f"ALTER TABLE media ADD COLUMN {name} {kind}")


def bulk_rate_unrated(ids: list[int], user: str, score: int) -> int:
    """Give `score` to every listed item this user has not rated yet."""
    c = conn()
    before = c.total_changes
    c.execute("BEGIN")
    c.executemany("INSERT OR IGNORE INTO ratings (media_id, user, score, star) VALUES (?,?,?,0)",
                  [(i, user, score) for i in ids])
    c.execute("COMMIT")
    return c.total_changes - before


def set_rating(media_id: int, user: str, score: int | None, star: bool) -> None:
    c = conn()
    if score is None and not star:
        c.execute("DELETE FROM ratings WHERE media_id=? AND user=?", (media_id, user))
        return
    c.execute(
        """INSERT INTO ratings (media_id, user, score, star) VALUES (?,?,?,?)
           ON CONFLICT(media_id, user) DO UPDATE SET
             score=excluded.score, star=excluded.star, updated_at=CURRENT_TIMESTAMP""",
        (media_id, user, score, 1 if star else 0),
    )
