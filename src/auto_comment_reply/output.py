"""Stable JSON output for MVP1 consumers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .models import Comment, FetchResult, Viewer
from .tree import build_comment_forest

SCHEMA_VERSION_LEGACY = "1.0"
SCHEMA_VERSION_DISCUSSION = "1.2"


def derive_is_self(comment: Comment, viewer: Viewer) -> bool | None:
    """Derive the output-only viewer-relative ``is_self`` tri-state.

    Unknown authors keep the ``0`` placeholder in the fact model and must be
    reported as ``null`` rather than a misleading ``false``.
    """

    if not viewer.authenticated or viewer.platform_user_id is None:
        return None
    if comment.user_id <= 0:
        return None
    return comment.user_id == viewer.platform_user_id


def _discussion_comment_document(comment: Comment, viewer: Viewer) -> dict[str, Any]:
    return {
        "comment_id": comment.comment_id,
        "author_id": comment.user_id,
        "is_self": derive_is_self(comment, viewer),
        "username": comment.username,
        "content": comment.content,
        "root_id": comment.root_id,
        "parent_id": comment.parent_id,
        "created_at": comment.created_at,
        "video_id": comment.video_id,
        "reply_count": comment.reply_count,
    }


def build_output_document(
    result: FetchResult,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    graph = build_comment_forest(result.comments)
    chains = graph.conversation_chains()
    diagnostics = [*result.diagnostics, *graph.diagnostics]
    complete = result.complete and not any(item.severity == "error" for item in diagnostics)

    stats = result.stats.to_dict()
    stats["orphan_comments"] = len(graph.orphans)
    stats["conversation_chains"] = len(chains)

    timestamp = generated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    comments = sorted(result.comments, key=lambda item: (item.created_at, item.comment_id))
    schema_version = (
        SCHEMA_VERSION_DISCUSSION if result.discussion is not None else SCHEMA_VERSION_LEGACY
    )
    head: dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "complete": complete,
        "video": result.video.to_dict(),
    }
    if result.discussion is not None:
        head["discussion"] = result.discussion.to_dict()
        head["viewer"] = result.viewer.to_dict()

        def serialize_comment(comment: Comment) -> dict[str, Any]:
            return _discussion_comment_document(comment, result.viewer)

        comment_documents = [serialize_comment(comment) for comment in comments]
        tree_documents = [
            tree.to_dict(comment_serializer=serialize_comment) for tree in graph.trees
        ]
    else:
        comment_documents = [comment.to_dict() for comment in comments]
        tree_documents = [tree.to_dict() for tree in graph.trees]

    tail: dict[str, Any] = {
        "stats": stats,
        "comments": comment_documents,
        "trees": tree_documents,
        "conversation_chains": chains,
        "orphan_comment_ids": [comment.comment_id for comment in graph.orphans],
        "duplicate_comment_ids": graph.duplicate_comment_ids,
        "diagnostics": [item.to_dict() for item in diagnostics],
    }
    return {**head, **tail}


def render_json(result: FetchResult, *, indent: int | None = 2) -> str:
    return json.dumps(build_output_document(result), ensure_ascii=False, indent=indent) + "\n"
