"""M3 offline acceptance: sync-run semantics, baselines, diff ledger, rollback."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _helpers import BVID, VIEWER_MID

from auto_comment_reply import storage
from auto_comment_reply.models import (
    ANONYMOUS_VIEWER,
    Comment,
    Diagnostic,
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
    username: str = "",
    content: str = "",
    root_id: int = 0,
    parent_id: int = 0,
    created_at: int = 0,
) -> Comment:
    return Comment(
        comment_id=comment_id,
        user_id=user_id,
        username=username or f"user-{comment_id}",
        content=content or f"comment-{comment_id}",
        root_id=root_id,
        parent_id=parent_id,
        created_at=created_at if created_at else comment_id,
        video_id=BVID,
    )


def make_result(
    comments: list[Comment],
    *,
    viewer: Viewer = ANONYMOUS_VIEWER,
    discussion_ref: DiscussionReference | None = None,
    complete: bool = True,
    diagnostics: list[Diagnostic] | None = None,
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
        complete=complete,
        diagnostics=list(diagnostics or []),
        stats=stats,
        discussion=discussion_ref if discussion_ref is not None else discussion(),
        viewer=viewer,
    )


def make_document(result: FetchResult, *, complete: bool | None = None) -> dict[str, Any]:
    document = build_output_document(result, generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    if complete is not None:
        document["complete"] = complete
    return document


def sync(path: Path, result: FetchResult, *, complete: bool | None = None) -> Any:
    return persist_discussion_sync(path, result, make_document(result, complete=complete))


def test_legacy_result_rejected_without_creating_database(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    legacy = FetchResult(
        video=video(),
        comments=[],
        complete=True,
        diagnostics=[],
        stats=FetchStats(),
        discussion=None,
        viewer=ANONYMOUS_VIEWER,
    )
    document = {"complete": True, "diagnostics": [], "generated_at": "2026-01-02T03:04:05Z"}
    with pytest.raises(PersistenceError) as exc_info:
        persist_discussion_sync(db, legacy, document)
    assert exc_info.value.category == "unsupported_discussion"
    assert not db.exists()


def test_invalid_document_rejected_without_creating_database(tmp_path: Path) -> None:
    db = tmp_path / "invalid.sqlite"
    result = make_result([comment(100)])
    invalid_documents = [
        {},
        {"complete": True, "diagnostics": [], "generated_at": ""},
        {"complete": "yes", "diagnostics": [], "generated_at": "2026-01-02T03:04:05Z"},
        {"complete": True, "diagnostics": "not-a-list", "generated_at": "2026-01-02T03:04:05Z"},
    ]
    for document in invalid_documents:
        with pytest.raises(PersistenceError) as exc_info:
            persist_discussion_sync(db, result, document)
        assert exc_info.value.category == "invalid_document"
        assert not db.exists()


def test_complete_runs_compute_newly_observed_and_visibility_diff(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    first = make_result(
        [
            comment(100, created_at=1),
            comment(110, root_id=100, parent_id=100, created_at=2),
        ]
    )
    outcome1 = sync(db, first)
    assert outcome1.newly_observed_ids == (100, 110)
    assert outcome1.not_currently_visible_ids == ()

    state = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state is not None
    assert state["last_complete_visible_ids"] == [100, 110]
    assert state["ever_seen_ids"] == [100, 110]

    second = make_result(
        [
            comment(100, created_at=1),
            comment(120, root_id=100, parent_id=100, created_at=3),
        ]
    )
    outcome2 = sync(db, second)
    assert outcome2.newly_observed_ids == (120,)
    assert outcome2.not_currently_visible_ids == (110,)

    state2 = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state2 is not None
    assert state2["last_complete_visible_ids"] == [100, 120]
    by_id = {item["comment_id"]: item for item in state2["observations"]}
    assert by_id[100]["current_visibility"] == "visible"
    assert by_id[110]["current_visibility"] == "not_currently_visible"
    assert by_id[120]["current_visibility"] == "visible"

    runs = storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
    assert [run["run_id"] for run in runs] == [outcome1.run_id, outcome2.run_id]
    assert runs[1]["newly_observed_ids"] == [120]
    assert runs[1]["not_currently_visible_ids"] == [110]
    assert runs[1]["previous_visible_ids"] == [100, 110]


def test_incomplete_run_absorbs_observations_without_baseline_change(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    sync(db, make_result([comment(100)]))
    partial = make_result([comment(100), comment(200)])
    outcome = sync(db, partial, complete=False)
    assert outcome.complete is False
    assert outcome.newly_observed_ids == (200,)
    assert outcome.not_currently_visible_ids == ()

    state = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state is not None
    assert state["last_complete_visible_ids"] == [100]
    assert state["ever_seen_ids"] == [100, 200]
    by_id = {item["comment_id"]: item for item in state["observations"]}
    assert by_id[100]["current_visibility"] == "visible"
    assert by_id[200]["current_visibility"] is None

    runs = storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
    assert len(runs) == 2
    assert runs[1]["complete"] is False
    assert runs[1]["previous_visible_ids"] is None
    assert runs[1]["not_currently_visible_ids"] == []


def test_empty_complete_run_clears_baseline_conservatively(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    sync(db, make_result([comment(100)]))
    outcome = sync(db, make_result([]))
    assert outcome.newly_observed_ids == ()
    assert outcome.not_currently_visible_ids == (100,)

    state = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state is not None
    assert state["last_complete_visible_ids"] == []
    by_id = {item["comment_id"]: item for item in state["observations"]}
    assert by_id[100]["current_visibility"] == "not_currently_visible"


def test_identical_snapshot_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    result = make_result(
        [
            comment(100),
            comment(110, root_id=100, parent_id=100),
        ]
    )
    sync(db, result)
    second = sync(db, result)
    assert second.newly_observed_ids == ()
    assert second.not_currently_visible_ids == ()

    assert len(storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())) == 2
    connection = sqlite3.connect(db)
    try:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("comments", "comment_observation", "viewer_state")
        }
    finally:
        connection.close()
    assert counts == {"comments": 2, "comment_observation": 2, "viewer_state": 1}


def test_viewers_are_isolated_and_username_change_keeps_identity(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    alice = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="alice",
    )
    bob = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="bob",
    )
    sync(db, make_result([comment(100)], viewer=alice))
    sync(
        db,
        make_result(
            [
                comment(100),
                comment(110, root_id=100, parent_id=100),
            ],
            viewer=bob,
        ),
    )

    assert storage.find_viewer(db, bob) == {
        "platform": "bilibili",
        "authenticated": True,
        "platform_user_id": VIEWER_MID,
        "username": "bob",
    }
    assert storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion()) is None

    connection = sqlite3.connect(db)
    try:
        count = connection.execute(
            "SELECT count(*) FROM viewers WHERE authenticated = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_persisted_complete_diagnostics_and_generated_at_come_from_document(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sync.sqlite"
    result = make_result(
        [comment(100)],
        complete=True,
        diagnostics=[
            Diagnostic(
                severity="warning",
                category="probe",
                scope="test",
                message="kept",
                details={"k": 1},
            )
        ],
    )
    document = build_output_document(result, generated_at=datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC))
    document["complete"] = False
    outcome = persist_discussion_sync(db, result, document)
    assert outcome.complete is False

    state = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state is not None
    assert state["last_complete_visible_ids"] is None
    runs = storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
    assert runs[0]["complete"] is False
    assert runs[0]["generated_at"] == "2026-02-03T04:05:06Z"
    assert runs[0]["diagnostics"] == [
        {
            "severity": "warning",
            "category": "probe",
            "scope": "test",
            "message": "kept",
            "details": {"k": 1},
        }
    ]

    followup = make_result([comment(100)], complete=False)
    followup_document = make_document(followup, complete=True)
    persist_discussion_sync(db, followup, followup_document)
    state2 = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state2 is not None
    assert state2["last_complete_visible_ids"] == [100]


def test_relationship_conflict_degrades_run_without_overwriting_or_baseline_change(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sync.sqlite"
    sync(
        db,
        make_result(
            [
                comment(100),
                comment(110, root_id=100, parent_id=100),
            ]
        ),
    )
    conflicting = make_result([comment(110, root_id=100, parent_id=50)])
    document = make_document(conflicting)
    outcome = persist_discussion_sync(db, conflicting, document)
    assert outcome.complete is False
    assert document["complete"] is False
    conflict_diagnostics = [
        item for item in document["diagnostics"] if item.get("category") == "relationship_conflict"
    ]
    assert len(conflict_diagnostics) == 1
    assert conflict_diagnostics[0]["severity"] == "error"
    assert conflict_diagnostics[0]["details"] == {}

    facts = {item["comment_id"]: item for item in storage.list_comments(db, discussion())}
    assert (facts[110]["root_id"], facts[110]["parent_id"]) == (100, 100)
    state = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state is not None
    assert state["last_complete_visible_ids"] == [100, 110]
    observations = {item["comment_id"]: item for item in state["observations"]}
    assert observations[110]["current_visibility"] == "visible"

    runs = storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
    assert len(runs) == 2
    assert runs[1]["complete"] is False
    assert runs[1]["previous_visible_ids"] is None
    assert runs[1]["not_currently_visible_ids"] == []
    assert runs[1]["observed_ids"] == [110]
    assert any(item.get("category") == "relationship_conflict" for item in runs[1]["diagnostics"])


def test_fault_before_commit_rolls_back_entire_run(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    sync(db, make_result([comment(100)]))

    def explode() -> None:
        raise RuntimeError("injected")

    with storage.inject_fault("before_commit", explode), pytest.raises(PersistenceError):
        sync(
            db,
            make_result(
                [
                    comment(100),
                    comment(110, root_id=100, parent_id=100),
                ]
            ),
        )

    state = storage.get_viewer_discussion_state(db, ANONYMOUS_VIEWER, discussion())
    assert state is not None
    assert state["last_complete_visible_ids"] == [100]
    assert len(storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())) == 1
    assert len(storage.list_comments(db, discussion())) == 1

    outcome = sync(
        db,
        make_result(
            [
                comment(100),
                comment(110, root_id=100, parent_id=100),
            ]
        ),
    )
    assert outcome.newly_observed_ids == (110,)


def test_redetected_comment_restores_visible_but_keeps_first_seen(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    sync(
        db,
        make_result(
            [
                comment(100),
                comment(110, root_id=100, parent_id=100),
            ]
        ),
    )
    sync(db, make_result([comment(100)]))
    before = {
        item["comment_id"]: item
        for item in storage.list_observations(db, ANONYMOUS_VIEWER, discussion())
    }
    sync(
        db,
        make_result(
            [
                comment(100),
                comment(110, root_id=100, parent_id=100),
            ]
        ),
    )

    after = {
        item["comment_id"]: item
        for item in storage.list_observations(db, ANONYMOUS_VIEWER, discussion())
    }
    assert after[110]["current_visibility"] == "visible"
    assert after[110]["first_seen_at"] == before[110]["first_seen_at"]


def test_placeholder_values_never_overwrite_stored_facts(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    placeholder = Comment(
        comment_id=100,
        user_id=0,
        username="",
        content="",
        root_id=0,
        parent_id=0,
        created_at=0,
        video_id=BVID,
    )
    sync(db, make_result([placeholder]))
    sync(
        db,
        make_result(
            [comment(100, user_id=7, username="real", content="real text", created_at=999)]
        ),
    )
    sync(db, make_result([placeholder]))

    assert storage.list_comments(db, discussion()) == [
        {
            "comment_id": 100,
            "author_id": 7,
            "username": "real",
            "content": "real text",
            "root_id": 0,
            "parent_id": 0,
            "created_at": 999,
            "video_id": BVID,
            "reply_count": 0,
        }
    ]
    assert "user_id" not in storage.list_comments(db, discussion())[0]


def test_placeholder_relationship_is_backfilled_by_real_observation(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    sync(db, make_result([comment(110)]))
    real = make_result([comment(100), comment(110, root_id=100, parent_id=100)])
    outcome = persist_discussion_sync(db, real, make_document(real))

    assert outcome.complete is True
    facts = {item["comment_id"]: item for item in storage.list_comments(db, discussion())}
    assert (facts[110]["root_id"], facts[110]["parent_id"]) == (100, 100)
    runs = storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
    assert runs[-1]["complete"] is True
    assert not any(
        item.get("category") == "relationship_conflict" for item in runs[-1]["diagnostics"]
    )


def test_same_run_duplicate_placeholder_merges_to_real_relationship(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    result = make_result([comment(110), comment(110, root_id=100, parent_id=100)])
    document = make_document(result)
    outcome = persist_discussion_sync(db, result, document)

    assert outcome.complete is True
    facts = storage.list_comments(db, discussion())
    assert len(facts) == 1
    assert (facts[0]["root_id"], facts[0]["parent_id"]) == (100, 100)
    assert not any(
        item.get("category") == "relationship_conflict" for item in document["diagnostics"]
    )


def test_same_run_duplicate_real_relationship_conflict_degrades_run(tmp_path: Path) -> None:
    db = tmp_path / "sync.sqlite"
    result = make_result(
        [
            comment(100),
            comment(110, root_id=100, parent_id=100),
            comment(110, root_id=100, parent_id=50),
        ]
    )
    document = make_document(result)
    outcome = persist_discussion_sync(db, result, document)

    assert outcome.complete is False
    assert document["complete"] is False
    facts = {item["comment_id"]: item for item in storage.list_comments(db, discussion())}
    assert len(facts) == 2
    assert (facts[110]["root_id"], facts[110]["parent_id"]) == (100, 100)
    runs = storage.list_sync_runs(db, ANONYMOUS_VIEWER, discussion())
    assert len(runs) == 1
    assert runs[0]["complete"] is False
    assert runs[0]["observed_ids"] == [100, 110]
    assert runs[0]["not_currently_visible_ids"] == []
    assert any(item.get("category") == "relationship_conflict" for item in runs[0]["diagnostics"])
