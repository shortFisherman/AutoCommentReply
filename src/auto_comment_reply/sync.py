"""Persistent sync semantics: apply one finalized FetchResult as an atomic run.

The adapter remains the platform-read boundary. This module turns an already
structurally validated targeted ``FetchResult`` plus its final schema-1.2
output document into one all-or-nothing SQLite sync run.

Algorithm (all inside one serialized transaction)
------------------------------------------------

1. Form ``observed_ids`` from the deduplicated comments and read
   ``ever_seen_before`` before touching observations.
2. Compute ``newly_observed_ids = observed_ids - ever_seen_before``; this step
   does not depend on ``complete``.
3. Upsert discussion/comment facts with placeholder-safe merge semantics and
   touch first/last-seen for every observed comment. A relationship conflict
   degrades the run to ``complete=false`` and is never silently overwritten.
4. If the final ``complete`` is true, read the most recent complete baseline
   ``previous_visible_ids`` (empty when no baseline exists yet), atomically
   replace it with ``observed_ids``, mark observed comments ``visible`` and
   the previous-baseline diff ``not_currently_visible``.
5. If ``complete`` is false, absorb facts and ever-seen only: no baseline
   replacement, no missing/deleted/unavailable inference, and previously
   stored ``current_visibility`` values remain untouched.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import storage
from .models import Comment, FetchResult
from .storage import PersistenceError

__all__ = ["PersistenceError", "SyncOutcome", "persist_discussion_sync"]


def _relationship_conflict_diagnostic() -> dict[str, Any]:
    """Return a fixed, sanitized relationship-conflict diagnostic."""

    return {
        "severity": "error",
        "category": "relationship_conflict",
        "scope": "comments",
        "message": "同一评论存在冲突的 root/parent 关系；既有关系未被覆盖，本轮标记为不完整。",
        "details": {},
    }


def _append_relationship_conflict_diagnostic(diagnostics: list[Any]) -> None:
    if any(
        isinstance(item, dict) and item.get("category") == "relationship_conflict"
        for item in diagnostics
    ):
        return
    diagnostics.append(_relationship_conflict_diagnostic())


def _merge_comment_pair(first: Comment, second: Comment) -> tuple[Comment, bool]:
    """Merge two same-round observations of one comment placeholder-safely.

    Returns the merged comment plus whether the two observations carry two
    conflicting real root/parent relationships.
    """

    first_pair = (first.root_id, first.parent_id)
    second_pair = (second.root_id, second.parent_id)
    if first_pair == second_pair:
        relationship = first_pair
        conflict = False
    elif first_pair == (0, 0):
        relationship = second_pair
        conflict = False
    elif second_pair == (0, 0):
        relationship = first_pair
        conflict = False
    else:
        relationship = first_pair
        conflict = True

    merged = Comment(
        comment_id=first.comment_id,
        user_id=first.user_id or second.user_id,
        username=first.username or second.username,
        content=first.content or second.content,
        root_id=relationship[0],
        parent_id=relationship[1],
        created_at=first.created_at or second.created_at,
        video_id=first.video_id or second.video_id,
        reply_count=max(first.reply_count, second.reply_count),
    )
    return merged, conflict


def _dedupe_comments(comments: list[Comment]) -> tuple[list[Comment], bool]:
    """Merge same-round duplicate comment IDs without silently dropping conflicts."""

    merged: dict[int, Comment] = {}
    conflict = False
    for comment in comments:
        previous = merged.get(comment.comment_id)
        if previous is None:
            merged[comment.comment_id] = comment
            continue
        merged[comment.comment_id], pair_conflict = _merge_comment_pair(previous, comment)
        conflict = conflict or pair_conflict
    return sorted(merged.values(), key=lambda item: item.comment_id), conflict


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """Committed result of one persistent sync run."""

    run_id: int
    complete: bool
    observed_ids: tuple[int, ...]
    newly_observed_ids: tuple[int, ...]
    not_currently_visible_ids: tuple[int, ...]
    generated_at: str


def persist_discussion_sync(
    database_path: Path,
    result: FetchResult,
    document: MutableMapping[str, Any],
) -> SyncOutcome:
    """Persist one finalized targeted discussion sync as an atomic run.

    ``document`` is the final schema 1.2 document produced by
    ``build_output_document``. Its ``complete``, ``diagnostics`` and
    ``generated_at`` values are persisted verbatim; ``result.complete`` is
    deliberately never consulted here. Entity facts come from
    ``result.discussion``, ``result.viewer`` and ``result.comments``.

    A relationship conflict degrades the run instead of raising: the stored
    relationship is never overwritten, a fixed sanitized error diagnostic is
    appended, and ``document`` is mutated in place so its final ``complete``
    and ``diagnostics`` exactly match the committed run. The previous complete
    baseline is left untouched and no missing/visibility diff is computed.
    """

    if result.discussion is None:
        raise storage._error("unsupported_discussion")

    complete = _require_bool(document.get("complete"))
    diagnostics = _require_list(document.get("diagnostics"))
    generated_at = _require_nonempty_string(document.get("generated_at"))

    comments, same_round_conflict = _dedupe_comments(result.comments)
    observed_ids = tuple(item.comment_id for item in comments)

    with storage._write_transaction(database_path) as connection:
        started_at = storage._now_utc()
        now = storage._format_utc(started_at)

        viewer_id = storage._upsert_viewer(connection, result.viewer, now)
        discussion_id = storage._upsert_discussion(connection, result.discussion, now)

        storage._ensure_viewer_state(
            connection, discussion_id=discussion_id, viewer_id=viewer_id, now=now
        )
        ever_seen_before = storage._read_ever_seen(
            connection, discussion_id=discussion_id, viewer_id=viewer_id
        )
        newly_observed_ids = tuple(sorted(set(observed_ids) - ever_seen_before))

        stored_comments = storage._read_comment_rows(connection, discussion_id)
        relationship_conflict = same_round_conflict
        comment_row_ids: dict[int, int] = {}
        for comment in comments:
            row_id, row_conflict = storage._upsert_comment(
                connection,
                discussion_id=discussion_id,
                stored=stored_comments.get(comment.comment_id),
                comment=comment,
                now=now,
            )
            comment_row_ids[comment.comment_id] = row_id
            relationship_conflict = relationship_conflict or row_conflict

        if relationship_conflict:
            complete = False
            _append_relationship_conflict_diagnostic(diagnostics)

        try:
            diagnostics_json = storage._encode_json(diagnostics)
        except (TypeError, ValueError) as error:
            raise storage._error("invalid_document") from error

        if complete:
            state = storage._read_viewer_state(
                connection, discussion_id=discussion_id, viewer_id=viewer_id
            )
            previous_visible_ids = storage._decode_visible_ids(
                state["last_complete_visible_ids"] if state is not None else None
            )
        else:
            previous_visible_ids = None

        for comment_id in observed_ids:
            storage._upsert_observation(
                connection,
                discussion_id=discussion_id,
                viewer_id=viewer_id,
                comment_row_id=comment_row_ids[comment_id],
                now=now,
                visibility="visible" if complete else None,
            )

        if complete:
            not_currently_visible_ids = tuple(sorted(set(previous_visible_ids) - set(observed_ids)))
            storage._mark_not_currently_visible(
                connection,
                discussion_id=discussion_id,
                viewer_id=viewer_id,
                comment_row_ids=(
                    stored_comments[comment_id]["id"]
                    for comment_id in not_currently_visible_ids
                    if comment_id in stored_comments
                ),
            )
        else:
            not_currently_visible_ids = ()

        finished_at = storage._now_utc()
        if finished_at < started_at:
            finished_at = started_at
        finished_text = storage._format_utc(finished_at)

        run_id = storage._insert_sync_run(
            connection,
            discussion_id=discussion_id,
            viewer_id=viewer_id,
            started_at=now,
            finished_at=finished_text,
            generated_at=generated_at,
            complete=complete,
            observed_ids=observed_ids,
            newly_observed_ids=newly_observed_ids,
            not_currently_visible_ids=not_currently_visible_ids,
            previous_visible_ids=previous_visible_ids if complete else None,
            diagnostics_json=diagnostics_json,
        )

        storage._update_viewer_state(
            connection,
            discussion_id=discussion_id,
            viewer_id=viewer_id,
            updated_at=finished_text,
            complete=complete,
            last_complete_sync_run_id=run_id if complete else None,
            last_complete_visible_ids=observed_ids if complete else None,
        )

        document["complete"] = complete
        document["diagnostics"] = diagnostics

    return SyncOutcome(
        run_id=run_id,
        complete=complete,
        observed_ids=observed_ids,
        newly_observed_ids=newly_observed_ids,
        not_currently_visible_ids=not_currently_visible_ids,
        generated_at=generated_at,
    )


def _require_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise storage._error("invalid_document")
    return value


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise storage._error("invalid_document")
    return value


def _require_nonempty_string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise storage._error("invalid_document")
    return value
