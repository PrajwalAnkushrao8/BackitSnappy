"""SQLite-backed metadata index for uploaded files and albums.

Single writer lock serializes writes (SQLite only allows one writer at a
time anyway); WAL mode lets concurrent readers (FastAPI GET routes) proceed
without blocking on the writer thread (folder watcher / upload handler).
"""
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

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

-- Audit trail for the Automatic Photos Backup feature: one row per
-- exported+confirmed-uploaded file, and one Photos item can be more than
-- one row -- a Live Photo's original is genuinely two files (the still and
-- its paired .mov), each uploaded and logged separately (see
-- photos_backup._process_item). photos_item_id is intentionally not
-- unique; db.get_processed_photos_item_ids() just needs *a* row to exist
-- for an item to treat it as already handled.
CREATE TABLE IF NOT EXISTS photos_backup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photos_item_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    uploaded_at REAL NOT NULL,
    deleted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_backup_log_item_id ON photos_backup_log(photos_item_id);

-- Single-row table (id is always 1) tracking when the poll loop last
-- actually ran a check -- distinct from any one item's uploaded_at, since a
-- poll cycle that finds nothing new still needs to move this forward for
-- Settings' "last checked" display to be honest.
CREATE TABLE IF NOT EXISTS photos_backup_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_checked_at REAL
);

-- Every Telegram api_id/api_hash pair this app has ever been set up with,
-- bound to the phone number it was entered for -- one number, one api_id,
-- permanently. Enforces that a given api_id can never be reused for a
-- second, different phone number (see client_manager.set_credentials):
-- without that, someone could unknowingly log into a second account using
-- credentials still "remembered" for a first, with no re-confirmation --
-- the same silent-carryover risk the account-switch index wipe already
-- guards against, just one layer up. Survives logout and even
-- wipe_local_index() (a genuine account switch) -- this registry is
-- permanent app-identity history, not part of any one account's index.
-- Deliberately holds NO api_hash: that half is the actual secret and lives
-- in the Keychain (secrets_store.set_api_hash_for_phone), since this file
-- is plain, unencrypted SQLite under Application Support. An earlier
-- version of this table did store api_hash here -- see
-- _migrate_api_hash_to_keychain for the one-time move and cleanup.
CREATE TABLE IF NOT EXISTS api_credentials (
    phone_number TEXT PRIMARY KEY,
    api_id INTEGER NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

-- One-time cleanup: the iCloud Offload Folder feature this replaced kept
-- its own audit table with a local quarantine/purge/restore lifecycle that
-- no longer applies to anything -- this is unreleased, local-only dev
-- data, so dropping it beats carrying dead schema forward.
DROP TABLE IF EXISTS offload_log;
"""


def _migrate_api_hash_to_keychain(conn: sqlite3.Connection) -> None:
    """One-time move of any api_hash still sitting in this file over to the
    Keychain, then removal of the column.

    A previous version of api_credentials stored api_hash in plaintext
    here. Creating the table without the column only helps fresh installs
    -- an existing database keeps the old column (and its contents) until
    something actively migrates it, which is what this does. VACUUM at the
    end matters as much as the DROP: without it the old value survives in
    pages the delete merely freed, still readable with a hex editor."""
    from . import secrets_store  # local import: secrets_store must not import db

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(api_credentials)")}
    if "api_hash" not in columns:
        return
    rows = conn.execute("SELECT phone_number, api_hash FROM api_credentials").fetchall()
    for row in rows:
        if row["api_hash"]:
            secrets_store.set_api_hash_for_phone(row["phone_number"], row["api_hash"])
    conn.execute("ALTER TABLE api_credentials DROP COLUMN api_hash")
    conn.commit()
    conn.execute("VACUUM")
    conn.commit()
    logger.info("Migrated %d api_hash value(s) out of the database into the Keychain", len(rows))


def _restrict_db_permissions() -> None:
    """0600, not the default 0644 -- this file holds the full index of what
    the user has stored, plus their phone number. No other account on the
    machine has any reason to read it."""
    try:
        os.chmod(config.DB_PATH, 0o600)
    except OSError:
        logger.warning("Could not tighten permissions on %s", config.DB_PATH)


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
            _migrate_api_hash_to_keychain(_conn)
        _restrict_db_permissions()
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
    Used on an account switch -- a different account would otherwise see
    stale rows pointing at channels it can't resolve. Rebuilt automatically
    on next login by discovering the account's own pre-existing BackitSnappy
    channels, if any.

    Deliberately doesn't touch media_cache/thumbnails on disk -- this is a
    pure-SQL module with no filesystem side effects by design. The caller
    is responsible for also pruning now-orphaned cache files (see
    media.prune_orphaned_cache, using get_all_known_hashes() before and
    after) or they'll silently linger forever, since nothing else will
    ever reference their now-deleted hash again."""
    conn = get_connection()
    with _write_lock:
        conn.execute("DELETE FROM upload_queue")
        conn.execute("DELETE FROM album_members")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM albums")
        conn.commit()


def get_all_known_hashes() -> set[str]:
    """Every sha256_hash currently referenced by an indexed file -- used to
    identify orphaned media_cache/thumbnail files (ones whose hash isn't in
    this set point at content nothing local references anymore)."""
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT sha256_hash FROM files").fetchall()
    return {row["sha256_hash"] for row in rows}


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


# --- Photos backup audit log --------------------------------------------

def insert_photos_backup_log(
    photos_item_id: str,
    filename: str,
    size: int,
    telegram_message_id: int,
    channel_id: int,
    uploaded_at: float,
    deleted_at: float,
) -> int:
    conn = get_connection()
    with _write_lock:
        cur = conn.execute(
            """INSERT INTO photos_backup_log
               (photos_item_id, filename, size, telegram_message_id, channel_id, uploaded_at, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (photos_item_id, filename, size, telegram_message_id, channel_id, uploaded_at, deleted_at),
        )
        conn.commit()
        return cur.lastrowid


def get_processed_photos_item_ids() -> set[str]:
    """Every Photos item id already backed up -- the poll loop diffs the
    live library against this set to find what's new."""
    conn = get_connection()
    rows = conn.execute("SELECT photos_item_id FROM photos_backup_log").fetchall()
    return {row["photos_item_id"] for row in rows}


def count_photos_backup_log() -> int:
    conn = get_connection()
    return conn.execute("SELECT COUNT(*) AS n FROM photos_backup_log").fetchone()["n"]


def list_photos_backup_log(limit: int = 50) -> list[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM photos_backup_log ORDER BY uploaded_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_photos_backup_last_checked() -> float | None:
    conn = get_connection()
    row = conn.execute("SELECT last_checked_at FROM photos_backup_state WHERE id = 1").fetchone()
    return row["last_checked_at"] if row else None


def set_photos_backup_last_checked(ts: float) -> None:
    conn = get_connection()
    with _write_lock:
        conn.execute(
            """INSERT INTO photos_backup_state (id, last_checked_at) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET last_checked_at = excluded.last_checked_at""",
            (ts,),
        )
        conn.commit()


# --- api credentials (api_id/api_hash <-> phone binding) -----------------

def get_api_credentials_for_phone(phone_key: str) -> tuple[int, str] | None:
    """Reassembles a phone number's credentials from both halves: api_id
    from this index, api_hash from the Keychain (see
    secrets_store.get_api_hash_for_phone for why they're split). Returns
    None unless both halves are present -- a row whose Keychain entry is
    missing (deleted by the user, or a database restored onto a different
    Mac) is unusable, and the caller should treat it as a number that
    needs credentials entered again rather than half-build a client."""
    from . import secrets_store  # local import: secrets_store must not import db

    conn = get_connection()
    row = conn.execute(
        "SELECT api_id FROM api_credentials WHERE phone_number = ?", (phone_key,)
    ).fetchone()
    if row is None:
        return None
    api_hash = secrets_store.get_api_hash_for_phone(phone_key)
    if not api_hash:
        return None
    return row["api_id"], api_hash


def get_phone_for_api_id(api_id: int) -> str | None:
    """Which phone number (if any) an api_id is already bound to -- used to
    reject reusing it for a *different* number (see
    client_manager.set_credentials) before ever calling bind_api_credentials."""
    conn = get_connection()
    row = conn.execute(
        "SELECT phone_number FROM api_credentials WHERE api_id = ?", (api_id,)
    ).fetchone()
    return row["phone_number"] if row else None


def bind_api_credentials(phone_key: str, api_id: int, api_hash: str) -> None:
    """Records this phone number's credentials permanently, splitting them:
    api_id into this index, api_hash into the Keychain. Callers MUST check
    get_phone_for_api_id first and refuse to call this if the api_id
    already belongs to a different phone_key -- this function itself
    doesn't re-check, so a caller that skips that guard would silently
    reassign the api_id's UNIQUE constraint to a new number, which is
    exactly what the guard exists to prevent."""
    from . import secrets_store  # local import: secrets_store must not import db

    conn = get_connection()
    with _write_lock:
        conn.execute(
            """INSERT INTO api_credentials (phone_number, api_id, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(phone_number) DO UPDATE SET api_id = excluded.api_id""",
            (phone_key, api_id, time.time()),
        )
        conn.commit()
    secrets_store.set_api_hash_for_phone(phone_key, api_hash)
