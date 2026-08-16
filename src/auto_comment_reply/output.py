"""Stable JSON output for MVP1 consumers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .models import FetchResult
from .tree import build_comment_forest

SCHEMA_VERSION_LEGACY = "1.0"
SCHEMA_VERSION_DISCUSSION = "1.1"


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
    tail: dict[str, Any] = {
        "stats": stats,
        "comments": [comment.to_dict() for comment in comments],
        "trees": [tree.to_dict() for tree in graph.trees],
        "conversation_chains": chains,
        "orphan_comment_ids": [comment.comment_id for comment in graph.orphans],
        "duplicate_comment_ids": graph.duplicate_comment_ids,
        "diagnostics": [item.to_dict() for item in diagnostics],
    }
    return {**head, **tail}


def render_json(result: FetchResult, *, indent: int | None = 2) -> str:
    return json.dumps(build_output_document(result), ensure_ascii=False, indent=indent) + "\n"
