"""Read-only Bilibili comment tree extraction."""

from .adapter import BilibiliAdapter
from .models import Comment, Diagnostic, FetchResult, FetchStats, VideoInfo
from .tree import CommentGraphError, TreeBuildResult, build_comment_forest, trace_to_root

__all__ = [
    "BilibiliAdapter",
    "Comment",
    "CommentGraphError",
    "Diagnostic",
    "FetchResult",
    "FetchStats",
    "TreeBuildResult",
    "VideoInfo",
    "build_comment_forest",
    "trace_to_root",
]

__version__ = "0.1.0"
