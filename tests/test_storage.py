"""M3 offline acceptance: schema, constraints, query API, locks, sanitization."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import BVID, UNIQUE_SECRET, VIEWER_MID

from auto_comment_reply import storage
from auto_comment_reply.models import (
    ANONYMOUS_VIEWER,
    Comment,
    FetchResult,
    FetchStats,
    VideoInfo,
    Viewer,
)
from auto_comment_reply.output import build_output_document
from auto_comment_reply.reference import DiscussionReference
from auto_comment_reply.storage import PersistenceError
from auto_comment_reply.sync import persist_discussion_sync


def video(aid: int = 42, bvid: str = BVID) -> VideoInfo:
    return VideoInfo(aid=aid, bvid=bvid, title="fixture video", owner_id=7, owner_name="owner")


def discussion(
    root_comment_id: int = 100, *, aid: int = 42, bvid: str = BVID
) -> DiscussionReference:
    return DiscussionReference(
        platform="bilibili",
        object_type="video",
        aid=aid,
        bvid=bvid,
        root_comment_id=root_comment_id,
    )


def comment(
    comment_id: int,
    *,
    user_id: int = 1,
    root_id: int = 0,
    parent_id: int = 0,
) -> Comment:
    return Comment(
        comment_id=comment_id,
        user_id=user_id,
        username=f"user-{comment_id}",
        content=f"comment-{comment_id}",
        root_id=root_id,
        parent_id=parent_id,
        created_at=comment_id,
        video_id=BVID,
    )


def make_result(
    comments: list[Comment],
    *,
    viewer: Viewer = ANONYMOUS_VIEWER,
    discussion_ref: DiscussionReference | None = None,
) -> FetchResult:
    stats = FetchStats(
        reply_pages_fetched=1,
        root_comments_fetched=sum(item.is_root for item in comments),
        reply_comments_fetched=sum(not item.is_root for item in comments),
        total_comments_fetched=len(comments),
    )
    return FetchResult(
        video=video(),
        comments=comments,
        complete=True,
        diagnostics=[],
        stats=stats,
        discussion=discussion_ref if discussion_ref is not None else discussion(),
        viewer=viewer,
    )


def sync(path: Path, result: FetchResult) -> Any:
    document = build_output_document(result, generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    return persist_discussion_sync(path, result, document)


def test_fresh_database_gets_schema_v1_wal_and_future_tables(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {
        "schema_version",
        "viewers",
        "discussions",
        "comments",
        "sync_runs",
        "viewer_state",
        "comment_observation",
        "notification_sync_state",
        "reply_events",
        "outbound_replies",
    } <= tables


def test_future_schema_version_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        connection.execute("UPDATE schema_version SET version = 99 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(PersistenceError) as exc_info:
        sync(db, make_result([comment(100), comment(110, root_id=100, parent_id=100)]))
    assert exc_info.value.category == "schema_too_new"

    with pytest.raises(PersistenceError) as exc_info2:
        storage.list_comments(db, discussion())
    assert exc_info2.value.category == "schema_too_new"


def test_large_int64_ids_round_trip_without_precision_loss(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    big = 9_007_199_254_740_993
    sync(
        db,
        make_result(
            [comment(big, user_id=9_223_372_036_854_775_806)],
            discussion_ref=discussion(root_comment_id=big),
        ),
    )
    facts = storage.list_comments(db, discussion(root_comment_id=big))
    assert facts[0]["comment_id"] == big
    assert facts[0]["author_id"] == 9_223_372_036_854_775_806
    assert "user_id" not in facts[0]


def test_anonymous_viewer_is_one_stable_entity_per_platform(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    sync(db, make_result([comment(110, root_id=100, parent_id=100)]))

    assert storage.find_viewer(db, ANONYMOUS_VIEWER) == {
        "platform": "bilibili",
        "authenticated": False,
        "platform_user_id": None,
        "username": None,
    }
    connection = sqlite3.connect(db)
    try:
        count = connection.execute(
            "SELECT count(*) FROM viewers WHERE authenticated = 0"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_query_api_is_viewer_scoped_and_stably_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "store.sqlite"
    tick = 0

    def fake_now() -> datetime:
        nonlocal tick
        tick += 1
        return datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(hours=tick)

    monkeypatch.setattr(storage, "_now_utc", fake_now)
    viewer = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="me",
    )
    other = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID + 1,
        username="them",
    )

    sync(db, make_result([comment(100)], viewer=viewer, discussion_ref=discussion(100)))
    sync(db, make_result([comment(200)], viewer=other, discussion_ref=discussion(200)))
    sync(
        db,
        make_result(
            [comment(100), comment(110, root_id=100, parent_id=100)],
            viewer=viewer,
            discussion_ref=discussion(100),
        ),
    )

    assert [item["root_comment_id"] for item in storage.list_viewer_discussions(db, viewer)] == [
        100
    ]
    assert [item["root_comment_id"] for item in storage.list_viewer_discussions(db, other)] == [200]

    state = storage.get_viewer_discussion_state(db, viewer, discussion(100))
    assert state is not None
    assert state["ever_seen_ids"] == [100, 110]
    assert state["last_complete_visible_ids"] == [100, 110]
    runs = storage.list_sync_runs(db, viewer, discussion(100))
    assert [run["observed_ids"] for run in runs] == [[100], [100, 110]]
    assert runs[0]["finished_at"] < runs[1]["finished_at"]


def test_observation_visibility_check_rejects_unavailable(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        (discussion_id,) = connection.execute("SELECT id FROM discussions LIMIT 1").fetchone()
        (viewer_id,) = connection.execute("SELECT id FROM viewers LIMIT 1").fetchone()
        (comment_row_id,) = connection.execute("SELECT id FROM comments LIMIT 1").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO comment_observation
                    (discussion_id, viewer_id, comment_row_id, first_seen_at,
                     last_seen_at, current_visibility)
                VALUES (?, ?, ?, '2026-01-01T00:00:00.000000Z',
                        '2026-01-01T00:00:00.000000Z', 'unavailable')
                """,
                (discussion_id, viewer_id, comment_row_id),
            )
    finally:
        connection.close()


def _insert_reply_event(
    connection: sqlite3.Connection,
    viewer_id: int,
    *,
    discussion_id: int | None = None,
    remote_event_id: str | None = None,
    source_comment_id: int | None = None,
    target_comment_id: int | None = None,
    object_type: str | None = None,
    oid: int | None = None,
    root_comment_id: int | None = None,
    author_id: int | None = None,
    event_type: str = "new_reply",
    event_time: str | None = None,
    target_availability: str = "unknown",
    event_status: str = "pending",
    dedup_key: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO reply_events
            (viewer_id, discussion_id, remote_event_id, source_comment_id,
             target_comment_id, object_type, oid, root_comment_id, author_id,
             event_type, event_time, target_availability, event_status,
             discovered_at, dedup_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            viewer_id,
            discussion_id,
            remote_event_id,
            source_comment_id,
            target_comment_id,
            object_type,
            oid,
            root_comment_id,
            author_id,
            event_type,
            event_time,
            target_availability,
            event_status,
            "2026-01-01T00:00:00.000000Z",
            dedup_key,
        ),
    )


def test_reply_events_insertable_before_discussion_or_comment_resolution(
    tmp_path: Path,
) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        viewer_id = connection.execute("SELECT id FROM viewers LIMIT 1").fetchone()[0]
        # Remove every resolved discussion/comment fact; only the viewer remains.
        connection.execute("DELETE FROM viewer_state")
        connection.execute("DELETE FROM sync_runs")
        connection.execute("DELETE FROM comments")
        connection.execute("DELETE FROM discussions")
        connection.commit()
        assert connection.execute("SELECT count(*) FROM discussions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM comments").fetchone()[0] == 0

        _insert_reply_event(
            connection,
            viewer_id,
            remote_event_id="evt-1",
            target_availability="unknown",
            event_status="pending",
        )
        connection.commit()
        row = connection.execute(
            "SELECT discussion_id, remote_event_id, target_availability, event_status "
            "FROM reply_events"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[0] is None
    assert row[1] == "evt-1"
    assert row[2] == "unknown"
    assert row[3] == "pending"


def test_reply_event_target_availability_is_independent_of_status(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        viewer_id = connection.execute("SELECT id FROM viewers LIMIT 1").fetchone()[0]
        pairs = [
            ("unavailable", "ready"),
            ("available", "cancelled"),
            ("unknown", "superseded"),
        ]
        for availability, status in pairs:
            _insert_reply_event(
                connection,
                viewer_id,
                remote_event_id=f"evt-{availability}-{status}",
                target_availability=availability,
                event_status=status,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_reply_event(
                connection,
                viewer_id,
                remote_event_id="evt-invalid-visibility",
                target_availability="visible",
                event_status="pending",
            )
        connection.execute(
            "UPDATE reply_events SET event_status = 'pending' "
            "WHERE remote_event_id = 'evt-unavailable-ready'"
        )
        connection.commit()
        rows = connection.execute(
            "SELECT target_availability, event_status FROM reply_events ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        ("unavailable", "pending"),
        ("available", "cancelled"),
        ("unknown", "superseded"),
    ]
    assert "visible" not in {row[0] for row in rows}


def test_reply_event_partial_unique_indexes_follow_stable_key_priority(
    tmp_path: Path,
) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        (viewer_id,) = connection.execute("SELECT id FROM viewers LIMIT 1").fetchone()
        (discussion_id,) = connection.execute("SELECT id FROM discussions LIMIT 1").fetchone()

        # Priority 1: one remote event id per viewer.
        _insert_reply_event(
            connection, viewer_id, discussion_id=discussion_id, remote_event_id="e1"
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_reply_event(
                connection, viewer_id, discussion_id=discussion_id, remote_event_id="e1"
            )

        # A different remote id may reuse the same source rpid (priority isolation).
        _insert_reply_event(
            connection,
            viewer_id,
            discussion_id=discussion_id,
            remote_event_id="e2",
            source_comment_id=110,
        )
        _insert_reply_event(
            connection,
            viewer_id,
            discussion_id=discussion_id,
            remote_event_id="e3",
            source_comment_id=110,
        )

        # Priority 2: without a remote id, source rpid is unique per viewer/discussion.
        _insert_reply_event(
            connection, viewer_id, discussion_id=discussion_id, source_comment_id=120
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_reply_event(
                connection, viewer_id, discussion_id=discussion_id, source_comment_id=120
            )

        # Priority 3: unresolved discussions dedupe on the composite natural key.
        composite = {
            "object_type": "video",
            "oid": 42,
            "root_comment_id": 100,
            "source_comment_id": 130,
            "target_comment_id": 100,
            "author_id": 7,
            "event_time": "2026-01-01T01:00:00Z",
        }
        _insert_reply_event(connection, viewer_id, **composite)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_reply_event(connection, viewer_id, **composite)
        _insert_reply_event(
            connection,
            viewer_id,
            **{**composite, "event_time": "2026-01-01T02:00:00Z"},
        )
        connection.commit()
    finally:
        connection.close()


def test_outbound_reply_statuses_and_target_natural_key_scope(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        (viewer_id,) = connection.execute("SELECT id FROM viewers LIMIT 1").fetchone()
        (discussion_id,) = connection.execute("SELECT id FROM discussions LIMIT 1").fetchone()
        (run_id,) = connection.execute("SELECT id FROM sync_runs LIMIT 1").fetchone()

        def insert_outbound(
            *,
            idempotency_key: str,
            status: str,
            target_comment_id: int,
            based_on_sync_run_id: int | None = None,
        ) -> None:
            connection.execute(
                """
                INSERT INTO outbound_replies
                    (viewer_id, discussion_id, target_comment_id, idempotency_key,
                     content, content_hash, based_on_sync_run_id, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'x', 'h', ?, ?, '2026-01-01T00:00:00.000000Z',
                        '2026-01-01T00:00:00.000000Z')
                """,
                (
                    viewer_id,
                    discussion_id,
                    target_comment_id,
                    idempotency_key,
                    based_on_sync_run_id,
                    status,
                ),
            )

        # All seven M6 states are legal; ``confirmed`` is accepted and the
        # default is ``prepared``.
        for status in (
            "prepared",
            "confirmed",
            "sending",
            "succeeded",
            "unknown",
            "retryable_failed",
            "terminal_failed",
        ):
            insert_outbound(idempotency_key=f"k-{status}", status=status, target_comment_id=100)
        connection.execute(
            """
            INSERT INTO outbound_replies
                (viewer_id, discussion_id, target_comment_id, idempotency_key,
                 content, content_hash, based_on_sync_run_id, created_at, updated_at)
            VALUES (?, ?, 100, 'k-default', 'x', 'h', ?, '2026-01-01T00:00:00.000000Z',
                    '2026-01-01T00:00:00.000000Z')
            """,
            (viewer_id, discussion_id, run_id),
        )
        row = connection.execute(
            "SELECT status FROM outbound_replies WHERE idempotency_key = 'k-default'"
        ).fetchone()
        assert row is not None and row[0] == "prepared"

        # ``draft`` belongs to the old draft-centric model and is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            insert_outbound(idempotency_key="k-draft", status="draft", target_comment_id=100)

        # ``target_comment_id`` is a platform comment id linked to the
        # (discussion_id, comment_id) natural key of comments.
        with pytest.raises(sqlite3.IntegrityError):
            insert_outbound(
                idempotency_key="k-unknown-target", status="prepared", target_comment_id=999
            )

        # A based-on run must belong to the same (discussion_id, viewer_id) scope.
        connection.commit()
        sync(db, make_result([comment(200)], discussion_ref=discussion(root_comment_id=200)))
        (other_run_id,) = connection.execute(
            "SELECT id FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            insert_outbound(
                idempotency_key="k-wrong-scope",
                status="prepared",
                target_comment_id=100,
                based_on_sync_run_id=other_run_id,
            )
        connection.commit()
    finally:
        connection.close()


def test_comments_store_no_is_self_or_global_visibility(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    connection = sqlite3.connect(db)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(comments)").fetchall()}
    finally:
        connection.close()
    assert "is_self" not in columns
    assert "visibility" not in columns
    assert "user_id" not in columns
    assert {
        "comment_id",
        "author_id",
        "username",
        "content",
        "root_id",
        "parent_id",
        "created_at",
        "video_id",
        "reply_count",
    } <= columns


def test_persistence_errors_are_sanitized(tmp_path: Path) -> None:
    target = tmp_path / "not-a-database.sqlite"
    target.write_text(f"{UNIQUE_SECRET} private-comment-body-should-not-leak", encoding="utf-8")
    result = make_result([comment(100)])
    document = build_output_document(result, generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    with pytest.raises(PersistenceError) as exc_info:
        persist_discussion_sync(target, result, document)
    assert exc_info.value.category == "schema_unknown"
    assert UNIQUE_SECRET not in str(exc_info.value)
    assert "private-comment-body-should-not-leak" not in str(exc_info.value)


def test_credentials_never_persist_to_database_bytes(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    sync(db, make_result([comment(110, root_id=100, parent_id=100)]))
    assert UNIQUE_SECRET.encode() not in db.read_bytes()


def test_lock_timeout_fails_closed_without_partial_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))
    monkeypatch.setattr(storage, "_BUSY_TIMEOUT_MS", 50)

    holder = sqlite3.connect(db)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(PersistenceError) as exc_info:
            sync(db, make_result([comment(100), comment(110, root_id=100, parent_id=100)]))
        assert exc_info.value.category == "lock_timeout"
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert len(storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())) == 1
    assert len(storage.list_comments(db, discussion())) == 1


def test_path_failures_use_fixed_sanitized_message(tmp_path: Path) -> None:
    directory = tmp_path / "database-as-directory"
    directory.mkdir()
    result = make_result([comment(100)])
    document = build_output_document(result, generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))

    with pytest.raises(PersistenceError) as exc_info:
        persist_discussion_sync(directory, result, document)
    assert exc_info.value.category == "open"
    assert exc_info.value.message == storage._ERROR_MESSAGES["open"]
    assert str(directory) not in str(exc_info.value)
    assert Path.home().name not in str(exc_info.value)

    missing = tmp_path / "missing.sqlite"
    with pytest.raises(PersistenceError) as exc_info2:
        storage.list_comments(missing, discussion())
    assert exc_info2.value.category == "not_found"
    assert str(missing) not in str(exc_info2.value)


def test_corrupted_json_columns_fail_closed_without_leaking(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    sync(db, make_result([comment(100)]))

    corruptions = [
        ("sync_runs", "observed_ids", f"{{broken-{UNIQUE_SECRET}"),
        ("sync_runs", "diagnostics", f'"not-a-list-{UNIQUE_SECRET}"'),
        ("viewer_state", "last_complete_visible_ids", f'[{{"secret": "{UNIQUE_SECRET}"}}]'),
    ]
    for table, column, value in corruptions:
        connection = sqlite3.connect(db)
        try:
            connection.execute(f"UPDATE {table} SET {column} = ?", (value,))
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(PersistenceError) as exc_info:
            if table == "viewer_state":
                storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
            else:
                storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
        assert exc_info.value.category == "schema_unknown"
        assert UNIQUE_SECRET not in str(exc_info.value)
        assert value not in str(exc_info.value)


def test_concurrent_first_open_creates_schema_once_without_migration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "store.sqlite"
    both_entered = threading.Barrier(2)
    real_ensure_schema = storage._ensure_schema

    def synchronized_ensure_schema(connection: sqlite3.Connection) -> None:
        both_entered.wait(timeout=10)
        real_ensure_schema(connection)

    monkeypatch.setattr(storage, "_ensure_schema", synchronized_ensure_schema)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            sync(db, make_result([comment(100)]))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM sync_runs").fetchone()[0] == 2
    finally:
        connection.close()
