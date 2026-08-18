"""Read-only Bilibili comment tree extraction."""

from .adapter import BilibiliAdapter
from .models import (
    ANONYMOUS_VIEWER,
    Comment,
    Diagnostic,
    FetchResult,
    FetchStats,
    VideoInfo,
    Viewer,
)
from .reference import (
    CommentReference,
    DiscussionReference,
    build_discussion_reference,
    parse_comment_reference,
)
from .sync import PersistenceError, SyncOutcome, persist_discussion_sync
from .tree import CommentGraphError, TreeBuildResult, build_comment_forest, trace_to_root

__all__ = [
    "ANONYMOUS_VIEWER",
    "BilibiliAdapter",
    "Comment",
    "CommentGraphError",
    "CommentReference",
    "Diagnostic",
    "DiscussionReference",
    "FetchResult",
    "FetchStats",
    "PersistenceError",
    "SyncOutcome",
    "TreeBuildResult",
    "VideoInfo",
    "Viewer",
    "build_discussion_reference",
    "build_comment_forest",
    "parse_comment_reference",
    "persist_discussion_sync",
    "trace_to_root",
]

__version__ = "0.1.0"
