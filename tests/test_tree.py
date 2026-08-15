from datetime import UTC, datetime

import pytest

from auto_comment_reply.models import Comment, FetchResult, FetchStats, VideoInfo
from auto_comment_reply.output import build_output_document
from auto_comment_reply.tree import CommentGraphError, build_comment_forest, trace_to_root


def comment(
    comment_id: int,
    *,
    root_id: int = 0,
    parent_id: int = 0,
    created_at: int | None = None,
) -> Comment:
    return Comment(
        comment_id=comment_id,
        user_id=comment_id + 1_000,
        username=f"user-{comment_id}",
        content=f"comment-{comment_id}",
        root_id=root_id,
        parent_id=parent_id,
        created_at=created_at if created_at is not None else comment_id,
        video_id="BV1xx411c7mD",
    )


def test_builds_stable_forest_and_all_root_to_leaf_chains() -> None:
    comments = [
        comment(120, root_id=100, parent_id=100),
        comment(111, root_id=100, parent_id=110),
        comment(210, root_id=200, parent_id=200),
        comment(100),
        comment(110, root_id=100, parent_id=100),
        comment(200),
    ]

    graph = build_comment_forest(comments)

    assert [tree.comment.comment_id for tree in graph.trees] == [100, 200]
    assert graph.conversation_chains() == [[100, 110, 111], [100, 120], [200, 210]]
    assert graph.orphans == []
    assert graph.diagnostics == []
    assert [item.comment_id for item in trace_to_root(111, comments)] == [100, 110, 111]


def test_missing_parent_preserves_broken_subtree_as_orphans() -> None:
    comments = [
        comment(100),
        comment(110, root_id=100, parent_id=999),
        comment(111, root_id=100, parent_id=110),
    ]

    graph = build_comment_forest(comments)

    assert graph.conversation_chains() == [[100]]
    assert [item.comment_id for item in graph.orphans] == [110, 111]
    assert [item.category for item in graph.diagnostics] == ["missing_parent"]
    with pytest.raises(CommentGraphError, match="缺少节点 999"):
        trace_to_root(110, comments)


def test_cycle_is_detected_without_recursive_loop() -> None:
    comments = [
        comment(100),
        comment(110, root_id=100, parent_id=111),
        comment(111, root_id=100, parent_id=110),
    ]

    graph = build_comment_forest(comments)

    assert [item.comment_id for item in graph.orphans] == [110, 111]
    assert graph.diagnostics[0].category == "comment_cycle"
    assert set(graph.diagnostics[0].details["comment_ids"]) == {110, 111}
    with pytest.raises(CommentGraphError, match="存在循环"):
        trace_to_root(110, comments)


def test_root_mismatch_is_explicit_and_makes_output_incomplete() -> None:
    comments = [comment(100), comment(200), comment(210, root_id=100, parent_id=200)]
    fetch = FetchResult(
        video=VideoInfo(
            aid=1,
            bvid="BV1xx411c7mD",
            title="test",
            owner_id=2,
            owner_name="owner",
        ),
        comments=comments,
        complete=True,
        diagnostics=[],
        stats=FetchStats(root_comments_fetched=2, reply_comments_fetched=1),
    )

    document = build_output_document(fetch, generated_at=datetime(2026, 8, 14, tzinfo=UTC))

    assert document["complete"] is False
    assert document["orphan_comment_ids"] == [210]
    assert document["diagnostics"][0]["category"] == "root_mismatch"
    assert document["generated_at"] == "2026-08-14T00:00:00Z"


def test_duplicate_comment_id_is_deterministic() -> None:
    original = comment(100, created_at=1)
    duplicate = comment(100, created_at=2)

    graph = build_comment_forest([original, duplicate])

    assert graph.comment_index[100] == original
    assert graph.duplicate_comment_ids == [100]
    assert graph.diagnostics[0].severity == "warning"
    assert trace_to_root(100, [original, duplicate]) == [original]


def test_trace_unknown_comment_uses_graph_error_contract() -> None:
    with pytest.raises(CommentGraphError, match="不存在评论 999"):
        trace_to_root(999, [comment(100)])
