"""SQLite-backed metadata index for uploaded files and albums.

Single writer lock serializes writes (SQLite only allows one writer at a
time anyway); WAL mode lets concurrent readers (FastAPI GET routes) proceed
without blocking on the writer thread (folder watcher / upload handler).
"""
import sqlite3
import threading
import time
from pathlib import Path

from . import config

_write_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    telegram_channel_id INTEGER NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime_type TEXT,
    telegram_message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    album_id INTEGER REFERENCES albums(id),
    source TEXT NOT NULL,
    uploaded_at REAL NOT NULL
);
-- A given file (by hash) may exist once in the storage channel (album_id IS
-- NULL) and additionally once per album it's been forwarded into — but never
-- twice in the *same* place. COALESCE folds NULL album_id into a single slot
-- for the uniqueness check.
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_hash_album
    ON files(sha256_hash, COALESCE(album_id, -1));
CREATE INDEX IF NOT EXISTS idx_files_album ON files(album_id);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(sha256_hash);

CREATE TABLE IF NOT EXISTS album_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    telegram_username TEXT NOT NULL,
    invited_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_album_members_album ON album_members(album_id);

-- A row exists here from the moment an upload is accepted (temp file
-- saved) until it reaches a terminal state (done or error) -- if the app
-- is closed or crashes in between, rows still present on next launch mean
-- "resume this," since their temp file is durable on disk regardless of
-- process lifetime.
CREATE TABLE IF NOT EXISTS upload_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temp_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    album_id INTEGER REFERENCES albums(id),
    source TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        with _write_lock:
            _conn.executescript(SCHEMA)
            _conn.commit()
    return _conn


def init_db(db_path: Path | None = None) -> None:
    """Force (re)initialization, optionally against a custom path (for tests)."""
    global _conn
    if db_path is not None:
        config.DB_PATH = db_path
    _conn = None
    get_connection()


# --- files -------------------------------------------------------------

def insert_file(
    filename: str,
    sha256_hash: str,
    size: int,
    mime_type: str | None,
    telegram_message_id: int,
    channel_id: int,
    album_id: int | None,
    source: str,
) -> int:
    conn = get_connection()
    with _write_lock:
        cur = conn.execute(
            """INSERT INTO files
               (filename, sha256_hash, size, mime_type, telegram_message_id,
                channel_id, album_id, source, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                filename,
                sha256_hash,
                size,
                mime_type,
                telegram_message_id,
                channel_id,
                album_id,
                source,
                time.time(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_file_by_hash_and_album(
    sha256_hash: str, album_id: int | None
) -> sqlite3.Row | None:
    """Look up a file in one specific place: the storage channel
    (album_id=None) or a specific album."""
    conn = get_connection()
    if album_id is None:
        return conn.execute(
            "SELECT * FROM files WHERE sha256_hash = ? AND album_id IS NULL",
            (sha256_hash,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM files WHERE sha256_hash = ? AND album_id = ?",
        (sha256_hash, album_id),
    ).fetchone()


def get_storage_copy_by_hash(sha256_hash: str) -> sqlite3.Row | None:
    """The canonical storage-channel copy of a file, if one exists — used as
    the forward source when adding an already-backed-up file to an album."""
    return get_file_by_hash_and_album(sha256_hash, None)


def get_file(file_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def delete_file(file_id: int) -> None:
    conn = get_connection()
    with _write_lock:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()


def get_files_by_channel(channel_id: int) -> list[sqlite3.Row]:
    """All indexed files claiming to live in a given Telegram channel --
    used to reconcile the local index against what's actually still there."""
    conn = get_connection()
    return conn.execute(
        "SELECT id, telegram_message_id FROM files WHERE channel_id = ?", (channel_id,)
    ).fetchall()


def list_files(album_id: int | None = None) -> list[sqlite3.Row]:
    """Files in one place: the storage channel (album_id=None) or a specific
    album — mirrors get_file_by_hash_and_album's semantics."""
    conn = get_connection()
    if album_id is None:
        return conn.execute(
            "SELECT * FROM files WHERE album_id IS NULL ORDER BY uploaded_at DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM files WHERE album_id = ? ORDER BY uploaded_at DESC",
        (album_id,),
    ).fetchall()


# --- albums --------------------------------------------------------------

def insert_album(name: str, telegram_channel_id: int) -> int:
    conn = get_connection()
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO albums (name, telegram_channel_id, created_at) VALUES (?, ?, ?)",
            (name, telegram_channel_id, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def wipe_local_index() -> None:
    """Clears every locally-indexed record (files, albums, membership, and
    any in-flight upload queue rows) without touching Telegram itself.
    Used on logout -- a different (or freshly reconnected) account would
    otherwise see stale rows pointing at channels it can't resolve.
    Rebuilt automatically on next login by discovering the account's own
    pre-existing BackitSnappy channels, if any."""
    conn = get_connection()
    with _write_lock:
        conn.execute("DELETE FROM upload_queue")
        conn.execute("DELETE FROM album_members")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM albums")
        conn.commit()


def get_album(album_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    return conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()


def get_album_by_channel(channel_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM albums WHERE telegram_channel_id = ?", (channel_id,)
    ).fetchone()


def list_albums() -> list[sqlite3.Row]:
    conn = get_connection()
    return conn.execute("SELECT * FROM albums ORDER BY created_at DESC").fetchall()


def delete_album(album_id: int) -> None:
    """Cascade-delete an album's files and members, then the album itself.
    SQLite here has no ON DELETE CASCADE configured, so this is done
    explicitly, atomically (one locked block, one commit)."""
    conn = get_connection()
    with _write_lock:
        conn.execute("DELETE FROM files WHERE album_id = ?", (album_id,))
        conn.execute("DELETE FROM album_members WHERE album_id = ?", (album_id,))
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        conn.commit()


# --- album members ---------------------------------------------------------

def insert_album_member(album_id: int, telegram_username: str) -> int:
    conn = get_connection()
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO album_members (album_id, telegram_username, invited_at) VALUES (?, ?, ?)",
            (album_id, telegram_username, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def list_album_members(album_id: int) -> list[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM album_members WHERE album_id = ? ORDER BY invited_at",
        (album_id,),
    ).fetchall()


# --- upload queue (crash/restart resume) ------------------------------------

def enqueue_upload(temp_path: str, original_filename: str, album_id: int | None, source: str) -> int:
    conn = get_connection()
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO upload_queue (temp_path, original_filename, album_id, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (temp_path, original_filename, album_id, source, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def dequeue_upload(queue_id: int) -> None:
    conn = get_connection()
    with _write_lock:
        conn.execute("DELETE FROM upload_queue WHERE id = ?", (queue_id,))
        conn.commit()


def get_pending_uploads() -> list[sqlite3.Row]:
    conn = get_connection()
    return conn.execute("SELECT * FROM upload_queue ORDER BY created_at").fetchall()
