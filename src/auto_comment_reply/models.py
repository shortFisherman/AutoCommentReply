"""Platform-neutral models emitted by the adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .reference import DiscussionReference


@dataclass(frozen=True, slots=True)
class Comment:
    """A normalized comment independent of Bilibili response layout."""

    comment_id: int
    user_id: int
    username: str
    content: str
    root_id: int
    parent_id: int
    created_at: int
    video_id: str
    reply_count: int = 0

    @property
    def is_root(self) -> bool:
        return self.root_id == 0 and self.parent_id == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "user_id": self.user_id,
            "username": self.username,
            "content": self.content,
            "root_id": self.root_id,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "video_id": self.video_id,
            "reply_count": self.reply_count,
        }


@dataclass(frozen=True, slots=True)
class VideoInfo:
    aid: int
    bvid: str
    title: str
    owner_id: int
    owner_name: str
    visible_comment_count_hint: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "aid": self.aid,
            "bvid": self.bvid,
            "title": self.title,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "visible_comment_count_hint": self.visible_comment_count_hint,
            "url": f"https://www.bilibili.com/video/{self.bvid}",
        }


@dataclass(frozen=True, slots=True, eq=False)
class Viewer:
    """Credential-free snapshot of the current local session's viewer identity.

    ``platform_user_id`` is the stable identity (Bilibili mid). ``username`` is
    display-only and must never participate in identity comparisons. This object
    never holds cookies or any other authentication material.
    """

    platform: str
    authenticated: bool
    platform_user_id: int | None
    username: str | None

    @property
    def identity(self) -> tuple[str, bool, int | None]:
        """Stable identity fields; ``username`` is display-only and excluded."""
        return (self.platform, self.authenticated, self.platform_user_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Viewer):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self) -> int:
        return hash(self.identity)

    def __post_init__(self) -> None:
        if self.authenticated:
            if (
                isinstance(self.platform_user_id, bool)
                or not isinstance(self.platform_user_id, int)
                or self.platform_user_id <= 0
            ):
                raise ValueError("authenticated viewer 必须有正整数 platform_user_id")
            if self.username is not None and not isinstance(self.username, str):
                raise ValueError("authenticated viewer 的 username 必须是字符串或 null")
        elif self.platform_user_id is not None or self.username is not None:
            raise ValueError("anonymous viewer 不能携带平台身份字段")

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "authenticated": self.authenticated,
            "platform_user_id": self.platform_user_id,
            "username": self.username,
        }


ANONYMOUS_VIEWER = Viewer(
    platform="bilibili",
    authenticated=False,
    platform_user_id=None,
    username=None,
)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: Literal["info", "warning", "error"]
    category: str
    scope: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "scope": self.scope,
            "message": self.message,
            "details": self.details,
        }


@dataclass(slots=True)
class FetchStats:
    root_pages_fetched: int = 0
    reply_pages_fetched: int = 0
    expected_total_comments: int | None = None
    root_comments_fetched: int = 0
    reply_comments_fetched: int = 0
    total_comments_fetched: int = 0
    duplicate_comments_seen: int = 0
    orphan_comments: int = 0
    conversation_chains: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_pages_fetched": self.root_pages_fetched,
            "reply_pages_fetched": self.reply_pages_fetched,
            "expected_total_comments": self.expected_total_comments,
            "root_comments_fetched": self.root_comments_fetched,
            "reply_comments_fetched": self.reply_comments_fetched,
            "total_comments_fetched": self.total_comments_fetched,
            "duplicate_comments_seen": self.duplicate_comments_seen,
            "orphan_comments": self.orphan_comments,
            "conversation_chains": self.conversation_chains,
        }


@dataclass(slots=True)
class FetchResult:
    video: VideoInfo
    comments: list[Comment]
    complete: bool
    diagnostics: list[Diagnostic]
    stats: FetchStats
    discussion: DiscussionReference | None = None
    viewer: Viewer = ANONYMOUS_VIEWER
