"""SQLite storage boundary for persistent targeted-discussion sync (M3).

This module owns the database connection lifecycle, schema versioning,
migrations, transactions, constraints, and the stable read-only query API.
It is deliberately free of platform protocol knowledge: adapters never open
connections here, and no credential, cookie, header, pagination cursor, or
request payload ever reaches this layer.

Design invariants
-----------------

* Every persistence time is UTC ISO-8601 with a fixed microsecond field, so
  lexicographic ordering matches chronological ordering across runs.
* Platform IDs are stored as SQLite INTEGER values and surfaced through the
  query API as Python ``int`` objects, so IDs beyond 2**53 never lose
  precision.
* Writers are serialized with ``BEGIN IMMEDIATE`` plus a bounded busy
  timeout; lock acquisition failure is a sanitized ``PersistenceError``.
* A sync run, its facts, observations, baseline and diff ledger commit
  together in one transaction or not at all.
* ``current_visibility`` may only be ``visible`` or
  ``not_currently_visible``; ``unavailable`` exists only as a
  ``reply_events.target_availability`` value.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Comment, Viewer
from .reference import DiscussionReference

SCHEMA_VERSION = 1

_BUSY_TIMEOUT_MS = 5000

_FAULT_POINTS = frozenset({"transaction_start", "before_commit"})
_fault_hooks: dict[str, Callable[[], None]] = {}


class PersistenceError(RuntimeError):
    """Sanitized, actionable persistence failure.

    ``category`` is one of a small fixed set of stable labels. The message is
    a fixed template and never echoes SQL text, comment bodies, credentials,
    headers, or server payloads.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


_ERROR_MESSAGES: dict[str, str] = {
    "unsupported_discussion": "持久化仅支持用户选择的定向讨论；legacy 整视频结果不能写入数据库。",
    "invalid_document": "输出文档缺少持久化所需字段或字段类型无效。",
    "open": "无法打开或创建 SQLite 数据库。",
    "not_found": "SQLite 数据库文件不存在。",
    "schema_too_new": "SQLite 数据库 schema 版本高于当前程序，已拒绝访问以防破坏数据。",
    "schema_unknown": "SQLite 数据库 schema 版本或结构不被当前程序识别，已拒绝访问。",
    "migration": "SQLite 数据库迁移失败，数据库保持原状。",
    "lock_timeout": "SQLite 数据库正被其他进程占用，等待写锁超时。",
    "constraint": "SQLite 约束校验失败，本轮同步已整体回滚。",
    "relationship_conflict": (
        "同一评论出现冲突的 root/parent 关系；既有关系未被覆盖，本轮标记为不完整。"
    ),
    "transaction": "SQLite 事务执行失败，本轮同步已整体回滚。",
    "commit": "SQLite 事务提交失败，本轮同步已整体回滚。",
    "query": "SQLite 查询失败。",
}


def _error(category: str) -> PersistenceError:
    return PersistenceError(category, _ERROR_MESSAGES[category])


def _path_error(category: str, path: Path) -> PersistenceError:
    """Return a fixed, sanitized message for ``category``.

    The ``path`` argument is accepted for caller symmetry but deliberately not
    echoed: absolute paths can contain the local username and other private
    data, and errors must never leak them.
    """

    return PersistenceError(category, _ERROR_MESSAGES[category])


def _now_utc() -> datetime:
    """Return the current UTC wall clock (module-level for test injection)."""

    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    """Format as fixed-width, sortable UTC text: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def inject_fault(point: str, hook: Callable[[], None]) -> Iterator[None]:
    """Test-only: run ``hook`` when the transaction reaches ``point``.

    Production persistence never registers hooks. Offline tests use this to
    prove rollback and crash recovery deterministically.
    """

    if point not in _FAULT_POINTS:
        raise ValueError(f"未知故障注入点：{point}")
    previous = _fault_hooks.get(point)
    _fault_hooks[point] = hook
    try:
        yield
    finally:
        if previous is None:
            _fault_hooks.pop(point, None)
        else:
            _fault_hooks[point] = previous


def clear_fault_hooks() -> None:
    """Test-only: remove every installed fault hook."""

    _fault_hooks.clear()


def _run_fault(point: str) -> None:
    hook = _fault_hooks.get(point)
    if hook is not None:
        hook()


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_id_list(raw: str) -> list[int]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise _error("schema_unknown") from error
    if not isinstance(value, list):
        raise _error("schema_unknown")
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise _error("schema_unknown")
        ids.append(item)
    return ids


def _decode_diagnostics(raw: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise _error("schema_unknown") from error
    if not isinstance(value, list):
        raise _error("schema_unknown")
    return value


def _decode_visible_ids(raw: str | None) -> list[int]:
    if raw is None:
        return []
    return _decode_id_list(raw)


def _readonly_uri(path: Path) -> str:
    posix = path.expanduser().resolve().as_posix()
    return f"file:{quote(posix, safe='/:')}?mode=ro"


def _is_busy_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _execute_retrying_busy(
    connection: sqlite3.Connection, statement: str, *, attempts: int = 10
) -> sqlite3.Cursor:
    """Run a connection pragma, retrying the brief first-open WAL race.

    Two writers opening the same brand-new database can transiently contend
    while the first connection initializes the header and WAL index; SQLite
    reports that as ``database is locked`` without honoring the configured
    busy timeout for that initialization window. A short bounded retry makes
    both writers converge instead of one failing on first open.
    """

    for attempt in range(attempts):
        try:
            return connection.execute(statement)
        except sqlite3.OperationalError as error:
            if attempt == attempts - 1 or not _is_busy_error(error):
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def _connect_read_write(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        # Use a short per-attempt timeout while probing/converting the journal
        # mode, then restore the configured timeout for real transactions.
        connection.execute(f"PRAGMA busy_timeout={min(_BUSY_TIMEOUT_MS, 250)}")
        mode_row = _execute_retrying_busy(connection, "PRAGMA journal_mode").fetchone()
        if mode_row is not None and str(mode_row[0]).lower() != "wal":
            _execute_retrying_busy(connection, "PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        _readonly_uri(path),
        uri=True,
        timeout=_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        connection.close()
        raise
    return connection


_SCHEMA_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE schema_version (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL CHECK (version >= 1)
    )
    """,
    """
    CREATE TABLE viewers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL CHECK (length(platform) > 0),
        authenticated INTEGER NOT NULL CHECK (authenticated IN (0, 1)),
        platform_user_id INTEGER,
        username TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (authenticated = 1 AND platform_user_id IS NOT NULL AND platform_user_id > 0)
            OR (authenticated = 0 AND platform_user_id IS NULL AND username IS NULL)
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_viewers_anonymous ON viewers (platform) WHERE authenticated = 0",
    """
    CREATE UNIQUE INDEX uq_viewers_authenticated
        ON viewers (platform, platform_user_id) WHERE authenticated = 1
    """,
    """
    CREATE TABLE discussions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL CHECK (length(platform) > 0),
        object_type TEXT NOT NULL CHECK (length(object_type) > 0),
        oid INTEGER NOT NULL CHECK (oid > 0),
        root_comment_id INTEGER NOT NULL CHECK (root_comment_id > 0),
        aid INTEGER NOT NULL CHECK (aid > 0),
        bvid TEXT,
        focus_comment_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (platform, object_type, oid, root_comment_id)
    )
    """,
    """
    CREATE TABLE comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discussion_id INTEGER NOT NULL REFERENCES discussions (id) ON DELETE RESTRICT,
        comment_id INTEGER NOT NULL CHECK (comment_id > 0),
        author_id INTEGER NOT NULL CHECK (author_id >= 0),
        username TEXT NOT NULL,
        content TEXT NOT NULL,
        root_id INTEGER NOT NULL CHECK (root_id >= 0),
        parent_id INTEGER NOT NULL CHECK (parent_id >= 0),
        created_at INTEGER NOT NULL CHECK (created_at >= 0),
        video_id TEXT NOT NULL,
        reply_count INTEGER NOT NULL CHECK (reply_count >= 0),
        created_utc TEXT NOT NULL,
        updated_utc TEXT NOT NULL,
        UNIQUE (discussion_id, comment_id),
        UNIQUE (discussion_id, id)
    )
    """,
    "CREATE INDEX ix_comments_discussion ON comments (discussion_id)",
    """
    CREATE TABLE sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discussion_id INTEGER NOT NULL REFERENCES discussions (id) ON DELETE RESTRICT,
        viewer_id INTEGER NOT NULL REFERENCES viewers (id) ON DELETE RESTRICT,
        started_at TEXT NOT NULL CHECK (length(started_at) > 0),
        finished_at TEXT NOT NULL CHECK (length(finished_at) > 0),
        generated_at TEXT NOT NULL CHECK (length(generated_at) > 0),
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
        observed_ids TEXT NOT NULL,
        newly_observed_ids TEXT NOT NULL,
        not_currently_visible_ids TEXT NOT NULL,
        previous_visible_ids TEXT,
        diagnostics TEXT NOT NULL,
        CHECK (started_at <= finished_at)
    )
    """,
    """
    CREATE INDEX ix_sync_runs_scope
        ON sync_runs (discussion_id, viewer_id, finished_at, id)
    """,
    """
    CREATE UNIQUE INDEX uq_sync_runs_scope_run
        ON sync_runs (discussion_id, viewer_id, id)
    """,
    """
    CREATE TABLE viewer_state (
        discussion_id INTEGER NOT NULL REFERENCES discussions (id) ON DELETE CASCADE,
        viewer_id INTEGER NOT NULL REFERENCES viewers (id) ON DELETE CASCADE,
        bound_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_complete_sync_run_id INTEGER,
        last_complete_visible_ids TEXT,
        PRIMARY KEY (discussion_id, viewer_id),
        FOREIGN KEY (discussion_id, viewer_id, last_complete_sync_run_id)
            REFERENCES sync_runs (discussion_id, viewer_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE comment_observation (
        discussion_id INTEGER NOT NULL,
        viewer_id INTEGER NOT NULL REFERENCES viewers (id) ON DELETE CASCADE,
        comment_row_id INTEGER NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        current_visibility TEXT
            CHECK (current_visibility IN ('visible', 'not_currently_visible')),
        PRIMARY KEY (discussion_id, viewer_id, comment_row_id),
        FOREIGN KEY (discussion_id, comment_row_id)
            REFERENCES comments (discussion_id, id) ON DELETE CASCADE,
        CHECK (first_seen_at <= last_seen_at)
    )
    """,
    """
    CREATE INDEX ix_comment_observation_scope
        ON comment_observation (discussion_id, viewer_id, first_seen_at, comment_row_id)
    """,
    """
    CREATE TABLE notification_sync_state (
        viewer_id INTEGER PRIMARY KEY REFERENCES viewers (id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'syncing', 'failed')),
        last_sync_started_at TEXT,
        last_sync_finished_at TEXT,
        last_cursor TEXT,
        last_error_category TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE reply_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        viewer_id INTEGER NOT NULL REFERENCES viewers (id) ON DELETE CASCADE,
        discussion_id INTEGER REFERENCES discussions (id) ON DELETE CASCADE,
        remote_event_id TEXT,
        source_comment_id INTEGER,
        target_comment_id INTEGER,
        object_type TEXT,
        oid INTEGER,
        root_comment_id INTEGER,
        author_id INTEGER,
        event_type TEXT NOT NULL CHECK (length(event_type) > 0),
        event_time TEXT,
        target_availability TEXT NOT NULL DEFAULT 'unknown'
            CHECK (target_availability IN ('unknown', 'available', 'unavailable')),
        event_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (event_status IN ('pending', 'ready', 'superseded', 'cancelled')),
        discovered_at TEXT NOT NULL,
        first_seen_sync_run_id INTEGER REFERENCES sync_runs (id) ON DELETE SET NULL,
        dedup_key TEXT,
        details TEXT
    )
    """,
    """
    CREATE INDEX ix_reply_events_scope
        ON reply_events (viewer_id, discussion_id, discovered_at, id)
    """,
    """
    CREATE UNIQUE INDEX uq_reply_events_remote_event
        ON reply_events (viewer_id, remote_event_id)
        WHERE remote_event_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX uq_reply_events_source_rpid
        ON reply_events (viewer_id, discussion_id, source_comment_id)
        WHERE discussion_id IS NOT NULL
          AND source_comment_id IS NOT NULL
          AND remote_event_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX uq_reply_events_composite
        ON reply_events (viewer_id, object_type, oid, root_comment_id,
                         source_comment_id, target_comment_id, author_id, event_time)
        WHERE discussion_id IS NULL
          AND remote_event_id IS NULL
          AND object_type IS NOT NULL
          AND oid IS NOT NULL
          AND root_comment_id IS NOT NULL
          AND source_comment_id IS NOT NULL
          AND target_comment_id IS NOT NULL
          AND author_id IS NOT NULL
          AND event_time IS NOT NULL
    """,
    """
    CREATE TABLE outbound_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        viewer_id INTEGER NOT NULL REFERENCES viewers (id) ON DELETE CASCADE,
        discussion_id INTEGER NOT NULL REFERENCES discussions (id) ON DELETE CASCADE,
        target_comment_id INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        based_on_sync_run_id INTEGER,
        status TEXT NOT NULL DEFAULT 'prepared'
            CHECK (status IN ('prepared', 'confirmed', 'sending', 'succeeded',
                              'unknown', 'retryable_failed', 'terminal_failed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_attempt_at TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        last_error TEXT,
        FOREIGN KEY (discussion_id, target_comment_id)
            REFERENCES comments (discussion_id, comment_id) ON DELETE RESTRICT,
        FOREIGN KEY (discussion_id, viewer_id, based_on_sync_run_id)
            REFERENCES sync_runs (discussion_id, viewer_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_outbound_replies_scope
        ON outbound_replies (viewer_id, discussion_id, status, created_at, id)
    """,
)

_MIGRATIONS: dict[int, tuple[str, ...]] = {}


def _rollback(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        connection.execute("ROLLBACK")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock: another writer may have created and
        # committed the schema while we were waiting for the lock. Treat that
        # as success instead of failing with a spurious migration error.
        if _schema_table_exists(connection):
            connection.execute("ROLLBACK")
            return
        for statement in _SCHEMA_V1_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)", (SCHEMA_VERSION,)
        )
        connection.execute("COMMIT")
    except BaseException:
        _rollback(connection)
        raise


def _schema_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_version'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None or not isinstance(row[0], int):
        return None
    return row[0]


def _migrate_schema(connection: sqlite3.Connection, current: int) -> None:
    while current < SCHEMA_VERSION:
        statements = _MIGRATIONS.get(current)
        if statements is None:
            raise _error("schema_unknown")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute("UPDATE schema_version SET version = ? WHERE id = 1", (current + 1,))
            connection.execute("COMMIT")
        except BaseException:
            _rollback(connection)
            raise
        current += 1


def _validate_schema_version(connection: sqlite3.Connection) -> None:
    version = _read_schema_version(connection)
    if version is None:
        raise _error("schema_unknown")
    if version > SCHEMA_VERSION:
        raise _error("schema_too_new")
    if version < SCHEMA_VERSION:
        _migrate_schema(connection, version)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    if not _schema_table_exists(connection):
        _create_schema(connection)
        return
    _validate_schema_version(connection)


def _check_readable_schema(connection: sqlite3.Connection) -> None:
    if not _schema_table_exists(connection):
        raise _error("schema_unknown")
    version = _read_schema_version(connection)
    if version is None:
        raise _error("schema_unknown")
    if version > SCHEMA_VERSION:
        raise _error("schema_too_new")
    if version < SCHEMA_VERSION:
        raise _error("schema_unknown")


def _translate_sqlite_error(error: sqlite3.Error, *, path: Path, phase: str) -> PersistenceError:
    if isinstance(error, sqlite3.IntegrityError):
        return _error("constraint")
    message = str(error).lower()
    if "locked" in message or "busy" in message:
        return _error("lock_timeout")
    if "not a database" in message or "malformed" in message or "no such table" in message:
        return _error("schema_unknown")
    if phase == "open":
        return _path_error("open", path)
    if phase == "commit":
        return _error("commit")
    if phase == "schema":
        return _error("migration")
    return _error("transaction")


@contextmanager
def _write_transaction(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a serialized, fail-closed write transaction."""

    try:
        connection = _connect_read_write(database_path)
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=database_path, phase="open") from error
    except OSError as error:
        raise _path_error("open", database_path) from error

    try:
        _ensure_schema(connection)
    except PersistenceError:
        connection.close()
        raise
    except sqlite3.Error as error:
        connection.close()
        raise _translate_sqlite_error(error, path=database_path, phase="schema") from error

    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        connection.close()
        raise _translate_sqlite_error(error, path=database_path, phase="begin") from error

    committing = False
    try:
        _run_fault("transaction_start")
        yield connection
        _run_fault("before_commit")
        committing = True
        connection.execute("COMMIT")
    except PersistenceError:
        _rollback(connection)
        raise
    except sqlite3.Error as error:
        _rollback(connection)
        phase = "commit" if committing else "write"
        raise _translate_sqlite_error(error, path=database_path, phase=phase) from error
    except Exception as error:
        _rollback(connection)
        category = "commit" if committing else "transaction"
        raise _error(category) from error
    finally:
        connection.close()


def _open_readonly(database_path: Path) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.exists():
        raise _path_error("not_found", path)
    try:
        connection = _connect_read_only(path)
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=path, phase="open") from error
    try:
        _check_readable_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _find_viewer_row(connection: sqlite3.Connection, viewer: Viewer) -> sqlite3.Row | None:
    if viewer.authenticated:
        return connection.execute(
            """
            SELECT id, platform, authenticated, platform_user_id, username
            FROM viewers
            WHERE platform = ? AND authenticated = 1 AND platform_user_id = ?
            """,
            (viewer.platform, viewer.platform_user_id),
        ).fetchone()
    return connection.execute(
        """
        SELECT id, platform, authenticated, platform_user_id, username
        FROM viewers
        WHERE platform = ? AND authenticated = 0
        """,
        (viewer.platform,),
    ).fetchone()


def _find_discussion_row(
    connection: sqlite3.Connection, discussion: DiscussionReference
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, platform, object_type, oid, aid, bvid, root_comment_id, focus_comment_id
        FROM discussions
        WHERE platform = ? AND object_type = ? AND oid = ? AND root_comment_id = ?
        """,
        (discussion.platform, discussion.object_type, discussion.aid, discussion.root_comment_id),
    ).fetchone()


def _viewer_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "platform": row["platform"],
        "authenticated": bool(row["authenticated"]),
        "platform_user_id": row["platform_user_id"],
        "username": row["username"],
    }


def _discussion_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "platform": row["platform"],
        "object_type": row["object_type"],
        "oid": row["oid"],
        "aid": row["aid"],
        "bvid": row["bvid"],
        "root_comment_id": row["root_comment_id"],
        "focus_comment_id": row["focus_comment_id"],
    }


def _comment_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "comment_id": row["comment_id"],
        "author_id": row["author_id"],
        "username": row["username"],
        "content": row["content"],
        "root_id": row["root_id"],
        "parent_id": row["parent_id"],
        "created_at": row["created_at"],
        "video_id": row["video_id"],
        "reply_count": row["reply_count"],
    }


def _upsert_viewer(connection: sqlite3.Connection, viewer: Viewer, now: str) -> int:
    if viewer.authenticated:
        row = connection.execute(
            """
            SELECT id, username FROM viewers
            WHERE platform = ? AND authenticated = 1 AND platform_user_id = ?
            """,
            (viewer.platform, viewer.platform_user_id),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO viewers
                    (platform, authenticated, platform_user_id, username, created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (viewer.platform, viewer.platform_user_id, viewer.username, now, now),
            )
            if cursor.lastrowid is None:
                raise _error("constraint")
            return cursor.lastrowid
        viewer_id = row["id"]
        if viewer.username is not None and viewer.username != row["username"]:
            connection.execute(
                "UPDATE viewers SET username = ?, updated_at = ? WHERE id = ?",
                (viewer.username, now, viewer_id),
            )
        return viewer_id

    row = connection.execute(
        "SELECT id FROM viewers WHERE platform = ? AND authenticated = 0",
        (viewer.platform,),
    ).fetchone()
    if row is not None:
        return row["id"]
    cursor = connection.execute(
        """
        INSERT INTO viewers
            (platform, authenticated, platform_user_id, username, created_at, updated_at)
        VALUES (?, 0, NULL, NULL, ?, ?)
        """,
        (viewer.platform, now, now),
    )
    if cursor.lastrowid is None:
        raise _error("constraint")
    return cursor.lastrowid


def _upsert_discussion(
    connection: sqlite3.Connection, discussion: DiscussionReference, now: str
) -> int:
    row = connection.execute(
        """
        SELECT id, bvid, focus_comment_id FROM discussions
        WHERE platform = ? AND object_type = ? AND oid = ? AND root_comment_id = ?
        """,
        (
            discussion.platform,
            discussion.object_type,
            discussion.aid,
            discussion.root_comment_id,
        ),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO discussions
                (platform, object_type, oid, root_comment_id, aid, bvid,
                 focus_comment_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discussion.platform,
                discussion.object_type,
                discussion.aid,
                discussion.root_comment_id,
                discussion.aid,
                discussion.bvid,
                discussion.focus_comment_id,
                now,
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise _error("constraint")
        return cursor.lastrowid

    discussion_id = row["id"]
    merged_bvid = discussion.bvid or row["bvid"]
    merged_focus = (
        discussion.focus_comment_id
        if discussion.focus_comment_id is not None
        else row["focus_comment_id"]
    )
    if merged_bvid != row["bvid"] or merged_focus != row["focus_comment_id"]:
        connection.execute(
            """
            UPDATE discussions SET bvid = ?, focus_comment_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (merged_bvid, merged_focus, now, discussion_id),
        )
    return discussion_id


def _read_comment_rows(
    connection: sqlite3.Connection, discussion_id: int
) -> dict[int, sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT id, comment_id, root_id, parent_id, author_id, username, content,
               created_at, video_id, reply_count
        FROM comments
        WHERE discussion_id = ?
        """,
        (discussion_id,),
    ).fetchall()
    return {row["comment_id"]: row for row in rows}


def _upsert_comment(
    connection: sqlite3.Connection,
    *,
    discussion_id: int,
    stored: sqlite3.Row | None,
    comment: Comment,
    now: str,
) -> tuple[int, bool]:
    """Insert or merge one comment fact with placeholder-safe semantics.

    Placeholder values (``0`` IDs, empty display fields) never overwrite
    already stored facts, and later complete values may backfill placeholders.
    Returns ``(comment_row_id, relationship_conflict)``: a stored ``(0, 0)``
    relationship is backfilled by a real one, an incoming ``(0, 0)`` keeps the
    stored relationship, and two conflicting real relationships are reported
    without overwriting the stored values.
    """

    if stored is None:
        cursor = connection.execute(
            """
            INSERT INTO comments
                (discussion_id, comment_id, author_id, username, content, root_id,
                 parent_id, created_at, video_id, reply_count, created_utc, updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discussion_id,
                comment.comment_id,
                comment.user_id,
                comment.username,
                comment.content,
                comment.root_id,
                comment.parent_id,
                comment.created_at,
                comment.video_id,
                comment.reply_count,
                now,
                now,
            ),
        )
        if cursor.lastrowid is None:
            raise _error("constraint")
        return cursor.lastrowid, False

    stored_pair = (stored["root_id"], stored["parent_id"])
    incoming_pair = (comment.root_id, comment.parent_id)
    if stored_pair == (0, 0) and incoming_pair != (0, 0):
        merged_pair = incoming_pair
        conflict = False
    elif incoming_pair == (0, 0):
        merged_pair = stored_pair
        conflict = False
    elif stored_pair != incoming_pair:
        merged_pair = stored_pair
        conflict = True
    else:
        merged_pair = stored_pair
        conflict = False

    connection.execute(
        """
        UPDATE comments
        SET author_id = ?, username = ?, content = ?, created_at = ?, video_id = ?,
            reply_count = ?, root_id = ?, parent_id = ?, updated_utc = ?
        WHERE id = ?
        """,
        (
            comment.user_id or stored["author_id"],
            comment.username or stored["username"],
            comment.content or stored["content"],
            comment.created_at or stored["created_at"],
            comment.video_id or stored["video_id"],
            max(stored["reply_count"], comment.reply_count),
            merged_pair[0],
            merged_pair[1],
            now,
            stored["id"],
        ),
    )
    return stored["id"], conflict


def _read_viewer_state(
    connection: sqlite3.Connection, *, discussion_id: int, viewer_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT bound_at, updated_at, last_complete_sync_run_id, last_complete_visible_ids
        FROM viewer_state
        WHERE discussion_id = ? AND viewer_id = ?
        """,
        (discussion_id, viewer_id),
    ).fetchone()


def _ensure_viewer_state(
    connection: sqlite3.Connection, *, discussion_id: int, viewer_id: int, now: str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO viewer_state
            (discussion_id, viewer_id, bound_at, updated_at,
             last_complete_sync_run_id, last_complete_visible_ids)
        VALUES (?, ?, ?, ?, NULL, NULL)
        """,
        (discussion_id, viewer_id, now, now),
    )


def _read_ever_seen(
    connection: sqlite3.Connection, *, discussion_id: int, viewer_id: int
) -> set[int]:
    rows = connection.execute(
        """
        SELECT c.comment_id
        FROM comment_observation o
        JOIN comments c ON c.id = o.comment_row_id
        WHERE o.discussion_id = ? AND o.viewer_id = ?
        """,
        (discussion_id, viewer_id),
    ).fetchall()
    return {row["comment_id"] for row in rows}


def _upsert_observation(
    connection: sqlite3.Connection,
    *,
    discussion_id: int,
    viewer_id: int,
    comment_row_id: int,
    now: str,
    visibility: str | None,
) -> None:
    """Insert or touch an observation; ``first_seen_at`` is never rewritten."""

    if visibility is None:
        connection.execute(
            """
            INSERT INTO comment_observation
                (discussion_id, viewer_id, comment_row_id, first_seen_at,
                 last_seen_at, current_visibility)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT (discussion_id, viewer_id, comment_row_id)
            DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (discussion_id, viewer_id, comment_row_id, now, now),
        )
        return
    connection.execute(
        """
        INSERT INTO comment_observation
            (discussion_id, viewer_id, comment_row_id, first_seen_at,
             last_seen_at, current_visibility)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (discussion_id, viewer_id, comment_row_id)
        DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            current_visibility = excluded.current_visibility
        """,
        (discussion_id, viewer_id, comment_row_id, now, now, visibility),
    )


def _mark_not_currently_visible(
    connection: sqlite3.Connection,
    *,
    discussion_id: int,
    viewer_id: int,
    comment_row_ids: Iterable[int],
) -> None:
    connection.executemany(
        """
        UPDATE comment_observation
        SET current_visibility = 'not_currently_visible'
        WHERE discussion_id = ? AND viewer_id = ? AND comment_row_id = ?
        """,
        [(discussion_id, viewer_id, row_id) for row_id in comment_row_ids],
    )


def _insert_sync_run(
    connection: sqlite3.Connection,
    *,
    discussion_id: int,
    viewer_id: int,
    started_at: str,
    finished_at: str,
    generated_at: str,
    complete: bool,
    observed_ids: tuple[int, ...],
    newly_observed_ids: tuple[int, ...],
    not_currently_visible_ids: tuple[int, ...],
    previous_visible_ids: list[int] | None,
    diagnostics_json: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO sync_runs
            (discussion_id, viewer_id, started_at, finished_at, generated_at, complete,
             observed_ids, newly_observed_ids, not_currently_visible_ids,
             previous_visible_ids, diagnostics)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            discussion_id,
            viewer_id,
            started_at,
            finished_at,
            generated_at,
            int(complete),
            _encode_json(list(observed_ids)),
            _encode_json(list(newly_observed_ids)),
            _encode_json(list(not_currently_visible_ids)),
            None if previous_visible_ids is None else _encode_json(list(previous_visible_ids)),
            diagnostics_json,
        ),
    )
    if cursor.lastrowid is None:
        raise _error("constraint")
    return cursor.lastrowid


def _update_viewer_state(
    connection: sqlite3.Connection,
    *,
    discussion_id: int,
    viewer_id: int,
    updated_at: str,
    complete: bool,
    last_complete_sync_run_id: int | None,
    last_complete_visible_ids: tuple[int, ...] | None,
) -> None:
    if complete:
        connection.execute(
            """
            UPDATE viewer_state
            SET updated_at = ?, last_complete_sync_run_id = ?, last_complete_visible_ids = ?
            WHERE discussion_id = ? AND viewer_id = ?
            """,
            (
                updated_at,
                last_complete_sync_run_id,
                _encode_json(list(last_complete_visible_ids or ())),
                discussion_id,
                viewer_id,
            ),
        )
        return
    connection.execute(
        "UPDATE viewer_state SET updated_at = ? WHERE discussion_id = ? AND viewer_id = ?",
        (updated_at, discussion_id, viewer_id),
    )


def find_viewer(database_path: Path, viewer: Viewer) -> dict[str, Any] | None:
    """Return the stored viewer for a stable viewer identity, or ``None``."""

    connection = _open_readonly(database_path)
    try:
        row = _find_viewer_row(connection, viewer)
        return None if row is None else _viewer_dict(row)
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


def find_discussion(database_path: Path, discussion: DiscussionReference) -> dict[str, Any] | None:
    """Return the stored discussion for a natural identity, or ``None``."""

    connection = _open_readonly(database_path)
    try:
        row = _find_discussion_row(connection, discussion)
        return None if row is None else _discussion_dict(row)
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


def list_viewer_discussions(database_path: Path, viewer: Viewer) -> list[dict[str, Any]]:
    """List the discussions tracked for ``viewer`` in stable order."""

    connection = _open_readonly(database_path)
    try:
        viewer_row = _find_viewer_row(connection, viewer)
        if viewer_row is None:
            return []
        rows = connection.execute(
            """
            SELECT d.platform, d.object_type, d.oid, d.aid, d.bvid, d.root_comment_id,
                   d.focus_comment_id, s.bound_at, s.updated_at
            FROM viewer_state s
            JOIN discussions d ON d.id = s.discussion_id
            WHERE s.viewer_id = ?
            ORDER BY s.updated_at, d.platform, d.object_type, d.oid, d.root_comment_id
            """,
            (viewer_row["id"],),
        ).fetchall()
        return [
            {
                **_discussion_dict(row),
                "bound_at": row["bound_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


def get_viewer_discussion_state(
    database_path: Path, viewer: Viewer, discussion: DiscussionReference
) -> dict[str, Any] | None:
    """Return tracked state, ever-seen, last complete baseline and observations."""

    connection = _open_readonly(database_path)
    try:
        viewer_row = _find_viewer_row(connection, viewer)
        discussion_row = _find_discussion_row(connection, discussion)
        if viewer_row is None or discussion_row is None:
            return None
        state = connection.execute(
            """
            SELECT bound_at, updated_at, last_complete_sync_run_id, last_complete_visible_ids
            FROM viewer_state
            WHERE discussion_id = ? AND viewer_id = ?
            """,
            (discussion_row["id"], viewer_row["id"]),
        ).fetchone()
        if state is None:
            return None
        observation_rows = connection.execute(
            """
            SELECT c.comment_id, o.first_seen_at, o.last_seen_at, o.current_visibility
            FROM comment_observation o
            JOIN comments c ON c.id = o.comment_row_id
            WHERE o.discussion_id = ? AND o.viewer_id = ?
            ORDER BY o.first_seen_at, c.comment_id
            """,
            (discussion_row["id"], viewer_row["id"]),
        ).fetchall()
        observations = [
            {
                "comment_id": row["comment_id"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "current_visibility": row["current_visibility"],
            }
            for row in observation_rows
        ]
        return {
            "tracked": True,
            "bound_at": state["bound_at"],
            "updated_at": state["updated_at"],
            "ever_seen_ids": [item["comment_id"] for item in observations],
            "last_complete_visible_ids": (
                None
                if state["last_complete_visible_ids"] is None
                else _decode_id_list(state["last_complete_visible_ids"])
            ),
            "last_complete_sync_run_id": state["last_complete_sync_run_id"],
            "observations": observations,
        }
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


def list_sync_runs(
    database_path: Path, viewer: Viewer, discussion: DiscussionReference
) -> list[dict[str, Any]]:
    """Return the append-only sync-run ledger in stable chronological order."""

    connection = _open_readonly(database_path)
    try:
        viewer_row = _find_viewer_row(connection, viewer)
        discussion_row = _find_discussion_row(connection, discussion)
        if viewer_row is None or discussion_row is None:
            return []
        rows = connection.execute(
            """
            SELECT id, started_at, finished_at, generated_at, complete,
                   observed_ids, newly_observed_ids, not_currently_visible_ids,
                   previous_visible_ids, diagnostics
            FROM sync_runs
            WHERE discussion_id = ? AND viewer_id = ?
            ORDER BY finished_at, id
            """,
            (discussion_row["id"], viewer_row["id"]),
        ).fetchall()
        return [
            {
                "run_id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "generated_at": row["generated_at"],
                "complete": bool(row["complete"]),
                "observed_ids": _decode_id_list(row["observed_ids"]),
                "newly_observed_ids": _decode_id_list(row["newly_observed_ids"]),
                "not_currently_visible_ids": _decode_id_list(row["not_currently_visible_ids"]),
                "previous_visible_ids": (
                    None
                    if row["previous_visible_ids"] is None
                    else _decode_id_list(row["previous_visible_ids"])
                ),
                "diagnostics": _decode_diagnostics(row["diagnostics"]),
            }
            for row in rows
        ]
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


def list_comments(database_path: Path, discussion: DiscussionReference) -> list[dict[str, Any]]:
    """Return normalized, viewer-independent comment facts for a discussion."""

    connection = _open_readonly(database_path)
    try:
        discussion_row = _find_discussion_row(connection, discussion)
        if discussion_row is None:
            return []
        rows = connection.execute(
            """
            SELECT comment_id, author_id, username, content, root_id, parent_id,
                   created_at, video_id, reply_count
            FROM comments
            WHERE discussion_id = ?
            ORDER BY created_at, comment_id
            """,
            (discussion_row["id"],),
        ).fetchall()
        return [_comment_dict(row) for row in rows]
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


def list_observations(
    database_path: Path, viewer: Viewer, discussion: DiscussionReference
) -> list[dict[str, Any]]:
    """Return viewer-scoped comment observations in stable order."""

    connection = _open_readonly(database_path)
    try:
        viewer_row = _find_viewer_row(connection, viewer)
        discussion_row = _find_discussion_row(connection, discussion)
        if viewer_row is None or discussion_row is None:
            return []
        rows = connection.execute(
            """
            SELECT c.comment_id, o.first_seen_at, o.last_seen_at, o.current_visibility
            FROM comment_observation o
            JOIN comments c ON c.id = o.comment_row_id
            WHERE o.discussion_id = ? AND o.viewer_id = ?
            ORDER BY o.first_seen_at, c.comment_id
            """,
            (discussion_row["id"], viewer_row["id"]),
        ).fetchall()
        return [
            {
                "comment_id": row["comment_id"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "current_visibility": row["current_visibility"],
            }
            for row in rows
        ]
    except sqlite3.Error as error:
        raise _translate_sqlite_error(error, path=Path(database_path), phase="query") from error
    finally:
        connection.close()


__all__ = [
    "SCHEMA_VERSION",
    "PersistenceError",
    "clear_fault_hooks",
    "find_discussion",
    "find_viewer",
    "get_viewer_discussion_state",
    "inject_fault",
    "list_comments",
    "list_observations",
    "list_sync_runs",
    "list_viewer_discussions",
]
