"""Pure parsing of expanded Bilibili comment share links (M1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, parse_qsl, urlsplit

from .errors import ParameterError
from .models import VideoInfo

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AID_RE = re.compile(r"(?:^|/)(?:av)?(?P<aid>[1-9][0-9]*)(?:$|[/?#])", re.IGNORECASE)
_POSITIVE_INT_RE = re.compile(r"[0-9]+")
_REPLY_FRAGMENT_RE = re.compile(r"reply([0-9]+)")
_ALLOWED_BILIBILI_HOSTS = frozenset({"bilibili.com", "www.bilibili.com", "m.bilibili.com"})
_ALLOWED_URL_PORTS = frozenset({None, 80, 443})


def validate_url_authority(parsed_url: SplitResult, *, context: str) -> None:
    """Reject unsafe URL components before any network use."""
    if parsed_url.scheme.lower() not in {"http", "https"}:
        raise ParameterError(f"{context}必须是 http(s) 地址。")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ParameterError(f"{context}不得包含用户名或密码。")
    try:
        port = parsed_url.port
    except ValueError as error:
        raise ParameterError(f"{context}包含无效端口。") from error
    if port not in _ALLOWED_URL_PORTS:
        raise ParameterError(f"{context}端口仅允许 80/443 或缺省。")
    if parsed_url.hostname is None:
        raise ParameterError(f"{context}缺少有效主机名。")


@dataclass(frozen=True, slots=True)
class CommentReference:
    """Immutable comment link parsed from an expanded Bilibili URL.

    ``focus_comment_id`` is only a user-selected focus candidate and must never be used
    as ``root_id`` or ``parent_id`` when building the comment tree.
    """

    bvid: str | None
    aid: int | None
    root_comment_id: int
    secondary_comment_id: int | None
    fragment_comment_id: int | None
    focus_comment_id: int | None


@dataclass(frozen=True, slots=True)
class DiscussionReference:
    """Canonical discussion identity resolved against platform video metadata."""

    platform: str
    object_type: str
    aid: int
    bvid: str
    root_comment_id: int
    focus_comment_id: int | None = None

    @property
    def identity(self) -> tuple[str, str, int, int]:
        """Return the focus-independent discussion identity tuple."""
        return (self.platform, self.object_type, self.aid, self.root_comment_id)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable output form, including the focus-independent identity."""
        return {
            "platform": self.platform,
            "object_type": self.object_type,
            "oid": self.aid,
            "aid": self.aid,
            "bvid": self.bvid,
            "root_comment_id": self.root_comment_id,
            "focus_comment_id": self.focus_comment_id,
            "identity": {
                "platform": self.platform,
                "object_type": self.object_type,
                "oid": self.aid,
                "root_comment_id": self.root_comment_id,
            },
        }


def parse_comment_reference(url: str) -> CommentReference:
    """Parse an expanded Bilibili comment share URL into an immutable reference."""
    raw_url = url.strip()
    if not raw_url:
        raise ParameterError("评论链接不能为空。")

    parsed = urlsplit(raw_url)
    validate_url_authority(parsed, context="评论链接")

    host = parsed.hostname.lower()
    if host == "b23.tv":
        raise ParameterError("b23.tv 短链必须先展开才能解析。")
    if not _is_allowed_bilibili_host(host):
        raise ParameterError("只接受 Bilibili 域名下的评论链接。")
    if "/video/" not in parsed.path:
        raise ParameterError("评论链接必须指向 Bilibili 视频页。")

    bvid = _extract_bvid(parsed.path)
    aid = None if bvid is not None else _extract_aid(parsed.path)
    if bvid is None and aid is None:
        raise ParameterError("评论链接中无法提取 BV 号或 AV 号。")

    root_comment_id = _required_positive_int(parsed.query, "comment_root_id")
    secondary_comment_id = _optional_positive_int(parsed.query, "comment_secondary_id")
    fragment_comment_id = _parse_reply_fragment(parsed.fragment)
    focus_comment_id = _resolve_focus(secondary_comment_id, fragment_comment_id)

    return CommentReference(
        bvid=bvid,
        aid=aid,
        root_comment_id=root_comment_id,
        secondary_comment_id=secondary_comment_id,
        fragment_comment_id=fragment_comment_id,
        focus_comment_id=focus_comment_id,
    )


def build_discussion_reference(
    video: VideoInfo, reference: CommentReference
) -> DiscussionReference:
    """Build the canonical discussion identity from resolved video metadata."""
    if reference.bvid is not None and reference.bvid != video.bvid:
        raise ParameterError("评论链接中的视频标识与视频信息不一致。")
    if reference.aid is not None and reference.aid != video.aid:
        raise ParameterError("评论链接中的视频标识与视频信息不一致。")

    return DiscussionReference(
        platform="bilibili",
        object_type="video",
        aid=video.aid,
        bvid=video.bvid,
        root_comment_id=reference.root_comment_id,
        focus_comment_id=reference.focus_comment_id,
    )


def _is_allowed_bilibili_host(host: str) -> bool:
    return host in _ALLOWED_BILIBILI_HOSTS or host.endswith(".bilibili.com")


def _extract_bvid(path: str) -> str | None:
    match = _BVID_RE.search(path)
    return match.group(0) if match is not None else None


def _extract_aid(path: str) -> int | None:
    match = _AID_RE.search(path)
    return int(match.group("aid")) if match is not None else None


def _query_values(query: str, name: str) -> list[str]:
    return [value for key, value in parse_qsl(query, keep_blank_values=True) if key == name]


def _required_positive_int(query: str, name: str) -> int:
    values = _query_values(query, name)
    if not values:
        raise ParameterError(f"评论链接缺少必需参数 {name}。")
    if len(values) > 1:
        raise ParameterError(f"评论链接中参数 {name} 重复。")
    return _positive_int(values[0], name=name)


def _optional_positive_int(query: str, name: str) -> int | None:
    values = _query_values(query, name)
    if not values:
        return None
    if len(values) > 1:
        raise ParameterError(f"评论链接中参数 {name} 重复。")
    return _positive_int(values[0], name=name)


def _positive_int(raw_value: str, *, name: str) -> int:
    if _POSITIVE_INT_RE.fullmatch(raw_value) is None:
        raise ParameterError(f"评论链接中的 {name} 必须是正整数。")
    parsed = int(raw_value)
    if parsed <= 0:
        raise ParameterError(f"评论链接中的 {name} 必须是正整数。")
    return parsed


def _parse_reply_fragment(fragment: str) -> int | None:
    if not fragment:
        return None
    match = _REPLY_FRAGMENT_RE.fullmatch(fragment)
    if match is None:
        raise ParameterError("评论链接中的 #reply 焦点格式无效。")
    return _positive_int(match.group(1), name="#reply")


def _resolve_focus(secondary_comment_id: int | None, fragment_comment_id: int | None) -> int | None:
    if (
        secondary_comment_id is not None
        and fragment_comment_id is not None
        and secondary_comment_id != fragment_comment_id
    ):
        raise ParameterError("评论链接中的 comment_secondary_id 与 #reply 焦点不一致。")
    return secondary_comment_id if secondary_comment_id is not None else fragment_comment_id
