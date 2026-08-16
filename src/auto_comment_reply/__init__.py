"""Read-only Bilibili comment tree extraction."""

from .adapter import BilibiliAdapter
from .models import Comment, Diagnostic, FetchResult, FetchStats, VideoInfo
from .reference import (
    CommentReference,
    DiscussionReference,
    build_discussion_reference,
    parse_comment_reference,
)
from .tree import CommentGraphError, TreeBuildResult, build_comment_forest, trace_to_root

__all__ = [
    "BilibiliAdapter",
    "Comment",
    "CommentGraphError",
    "CommentReference",
    "Diagnostic",
    "DiscussionReference",
    "FetchResult",
    "FetchStats",
    "TreeBuildResult",
    "VideoInfo",
    "build_discussion_reference",
    "build_comment_forest",
    "parse_comment_reference",
    "trace_to_root",
]

__version__ = "0.1.0"
