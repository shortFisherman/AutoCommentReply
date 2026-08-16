"""M2 offline acceptance: output schema, author identity and is_self (A3/A5/A7)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from _helpers import BVID, UNIQUE_SECRET, VIEWER_MID

from auto_comment_reply.models import Comment, FetchResult, FetchStats, VideoInfo
from auto_comment_reply.output import build_output_document
from auto_comment_reply.reference import DiscussionReference

try:
    from auto_comment_reply.models import Viewer
except ImportError:  # pragma: no cover - M2 source not landed yet
    Viewer = None  # type: ignore[assignment]


def require_viewer() -> None:
    if Viewer is None:
        pytest.fail("M2 Viewer 模型尚未实现（source 未就绪）")


def video() -> VideoInfo:
    return VideoInfo(
        aid=42,
        bvid=BVID,
        title="fixture video",
        owner_id=7,
        owner_name="owner",
        visible_comment_count_hint=3,
    )


def discussion() -> DiscussionReference:
    return DiscussionReference(
        platform="bilibili",
        object_type="video",
        aid=42,
        bvid=BVID,
        root_comment_id=100,
        focus_comment_id=None,
    )


def comment(
    comment_id: int,
    *,
    user_id: int,
    username: str = "",
    root_id: int = 0,
    parent_id: int = 0,
    created_at: int | None = None,
) -> Comment:
    return Comment(
        comment_id=comment_id,
        user_id=user_id,
        username=username,
        content=f"comment-{comment_id}",
        root_id=root_id,
        parent_id=parent_id,
        created_at=created_at if created_at is not None else comment_id,
        video_id=BVID,
    )


def make_result(
    *,
    viewer: Any,
    comments: list[Comment],
    with_discussion: bool = True,
) -> FetchResult:
    stats = FetchStats(
        reply_pages_fetched=1,
        root_comments_fetched=sum(item.is_root for item in comments),
        reply_comments_fetched=sum(not item.is_root for item in comments),
        total_comments_fetched=len(comments),
    )
    base = {
        "video": video(),
        "comments": comments,
        "complete": True,
        "diagnostics": [],
        "stats": stats,
    }
    if with_discussion:
        base["discussion"] = discussion()
    try:
        return FetchResult(viewer=viewer, **base)
    except TypeError as error:
        pytest.fail(f"M2 FetchResult.viewer 字段尚未实现：{error}")


def comment_dicts(document: dict[str, Any]) -> list[dict[str, Any]]:
    collected = list(document.get("comments", []))
    stack = [*document.get("trees", [])]
    while stack:
        node = stack.pop()
        collected.append(node["comment"])
        stack.extend(node.get("children", []))
    return collected


def assert_key_absent(value: Any, key: str) -> None:
    if isinstance(value, dict):
        assert key not in value, f"unexpected key {key!r} in {value!r}"
        for child in value.values():
            assert_key_absent(child, key)
    elif isinstance(value, list):
        for child in value:
            assert_key_absent(child, key)


def test_discussion_schema_1_2_emits_viewer_author_id_and_is_self_tristate() -> None:
    require_viewer()
    viewer = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    document = build_output_document(
        make_result(
            viewer=viewer,
            comments=[
                comment(100, user_id=VIEWER_MID, username="self", created_at=1),
                comment(
                    110,
                    user_id=VIEWER_MID + 1,
                    username="other",
                    root_id=100,
                    parent_id=100,
                    created_at=2,
                ),
                comment(111, user_id=0, username="", root_id=100, parent_id=110, created_at=3),
                comment(200, user_id=999, username="flat-other", created_at=4),
            ],
        )
    )

    assert document["schema_version"] == "1.2"
    assert document["viewer"] == {
        "platform": "bilibili",
        "authenticated": True,
        "platform_user_id": VIEWER_MID,
        "username": "display-only",
    }

    by_id = {item["comment_id"]: item for item in document["comments"]}
    assert by_id[100]["author_id"] == VIEWER_MID
    assert by_id[100]["is_self"] is True
    assert by_id[110]["author_id"] == VIEWER_MID + 1
    assert by_id[110]["is_self"] is False
    assert by_id[111]["is_self"] is None
    assert by_id[200]["is_self"] is False
    for item in by_id.values():
        assert "user_id" not in item

    tree = document["trees"][0]
    assert tree["comment"]["author_id"] == VIEWER_MID
    assert tree["comment"]["is_self"] is True
    child = tree["children"][0]
    assert child["comment"]["author_id"] == VIEWER_MID + 1
    assert child["comment"]["is_self"] is False
    assert child["children"][0]["comment"]["is_self"] is None


def test_discussion_schema_1_2_never_emits_user_id_alias_anywhere() -> None:
    require_viewer()
    viewer = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    document = build_output_document(
        make_result(
            viewer=viewer,
            comments=[
                comment(100, user_id=VIEWER_MID, created_at=1),
                comment(110, user_id=999, root_id=100, parent_id=100, created_at=2),
            ],
        )
    )

    assert_key_absent(document, "user_id")
    assert any("author_id" in item for item in comment_dicts(document))


def test_legacy_schema_1_0_fields_and_shape_are_unchanged() -> None:
    require_viewer()
    viewer = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    document = build_output_document(
        make_result(
            viewer=viewer,
            comments=[
                comment(100, user_id=VIEWER_MID, created_at=1),
                comment(110, user_id=999, root_id=100, parent_id=100, created_at=2),
            ],
            with_discussion=False,
        )
    )

    assert document["schema_version"] == "1.0"
    assert "viewer" not in document
    assert_key_absent(document, "author_id")
    assert_key_absent(document, "is_self")
    for item in comment_dicts(document):
        assert "user_id" in item
        assert item["user_id"] >= 0


def test_anonymous_discussion_viewer_and_all_is_self_are_null() -> None:
    require_viewer()
    viewer = Viewer(
        platform="bilibili",
        authenticated=False,
        platform_user_id=None,
        username=None,
    )
    document = build_output_document(
        make_result(
            viewer=viewer,
            comments=[
                comment(100, user_id=VIEWER_MID, created_at=1),
                comment(110, user_id=999, root_id=100, parent_id=100, created_at=2),
            ],
        )
    )

    assert document["viewer"] == {
        "platform": "bilibili",
        "authenticated": False,
        "platform_user_id": None,
        "username": None,
    }
    for item in comment_dicts(document):
        assert item["is_self"] is None
        assert "author_id" in item
    assert document["comments"][0]["author_id"] == VIEWER_MID


def test_is_self_is_output_derived_and_not_stored_on_comment_fact() -> None:
    require_viewer()
    item = comment(100, user_id=VIEWER_MID)
    assert not hasattr(item, "is_self")
    assert "is_self" not in item.to_dict()


def test_username_never_affects_is_self_or_discussion_identity() -> None:
    require_viewer()
    comments = [
        comment(100, user_id=VIEWER_MID, created_at=1),
        comment(110, user_id=999, root_id=100, parent_id=100, created_at=2),
    ]
    first = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="alice",
    )
    second = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="completely-different-name",
    )

    first_doc = build_output_document(make_result(viewer=first, comments=comments))
    second_doc = build_output_document(make_result(viewer=second, comments=comments))

    assert [item["is_self"] for item in first_doc["comments"]] == [True, False]
    assert [item["is_self"] for item in second_doc["comments"]] == [True, False]
    assert first_doc["discussion"] == second_doc["discussion"]
    assert first_doc["discussion"]["identity"] == {
        "platform": "bilibili",
        "object_type": "video",
        "oid": 42,
        "root_comment_id": 100,
    }


def test_discussion_identity_scope_and_root_pages_are_viewer_independent() -> None:
    require_viewer()
    comments = [
        comment(100, user_id=VIEWER_MID, created_at=1),
        comment(110, user_id=999, root_id=100, parent_id=100, created_at=2),
    ]
    viewers = [
        Viewer(platform="bilibili", authenticated=False, platform_user_id=None, username=None),
        Viewer(
            platform="bilibili",
            authenticated=True,
            platform_user_id=VIEWER_MID,
            username="one",
        ),
        Viewer(
            platform="bilibili",
            authenticated=True,
            platform_user_id=888888,
            username="two",
        ),
    ]

    expected_discussion = discussion().to_dict()
    for viewer in viewers:
        document = build_output_document(make_result(viewer=viewer, comments=comments))
        assert document["discussion"] == expected_discussion
        assert document["stats"]["root_pages_fetched"] == 0
        assert document["stats"]["reply_pages_fetched"] == 1
        assert document["stats"]["total_comments_fetched"] == 2


def test_output_documents_and_reprs_never_echo_credentials() -> None:
    require_viewer()
    viewer = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    result = make_result(
        viewer=viewer,
        comments=[
            comment(100, user_id=VIEWER_MID, created_at=1),
            comment(110, user_id=999, root_id=100, parent_id=100, created_at=2),
        ],
    )
    document = build_output_document(result)
    payloads = [
        json.dumps(document, ensure_ascii=False),
        repr(document),
        repr(result),
        repr(result.viewer),
        repr(result.comments[0]),
        repr(result.diagnostics),
    ]
    for text in payloads:
        assert UNIQUE_SECRET not in text
