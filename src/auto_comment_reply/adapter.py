"""The only module allowed to know Bilibili's private web API details."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse

import httpx

from .errors import (
    AccessDeniedError,
    AuthenticationError,
    BilibiliError,
    BusinessError,
    HttpError,
    NetworkError,
    PaginationError,
    ParameterError,
    RateLimitError,
    ResponseParseError,
)
from .models import Comment, Diagnostic, FetchResult, FetchStats, VideoInfo
from .reference import (
    DiscussionReference,
    build_discussion_reference,
    parse_comment_reference,
    validate_url_authority,
)
from .wbi import derive_mixin_key, sign_wbi_params

logger = logging.getLogger(__name__)

API_BASE = "https://api.bilibili.com"
VIEW_ENDPOINT = f"{API_BASE}/x/web-interface/view"
NAV_ENDPOINT = f"{API_BASE}/x/web-interface/nav"
MAIN_REPLY_ENDPOINT = f"{API_BASE}/x/v2/reply/wbi/main"
CHILD_REPLY_ENDPOINT = f"{API_BASE}/x/v2/reply/reply"

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AID_RE = re.compile(r"(?:^|/)(?:av)?(?P<aid>[1-9][0-9]*)(?:$|[/?#])", re.IGNORECASE)
_ALLOWED_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_SHORT_LINK_HOPS = 5


class BilibiliAdapter:
    """Read and normalize complete Bilibili comment trees.

    The adapter is deliberately synchronous and conservative. Sequential requests plus a small
    delay make completeness and branch-level diagnostics easier to reason about than a large
    fan-out crawler, and reduce avoidable pressure on Bilibili's private endpoints.
    """

    def __init__(
        self,
        *,
        cookie: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
        retries: int = 2,
        retry_backoff: float = 0.5,
        request_delay: float = 0.25,
        max_root_pages: int = 10_000,
        max_reply_pages: int = 10_000,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if retries < 0:
            raise ValueError("retries 不能小于 0")
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if request_delay < 0 or retry_backoff < 0:
            raise ValueError("request_delay 和 retry_backoff 不能小于 0")
        if max_root_pages <= 0 or max_reply_pages <= 0:
            raise ValueError("分页安全上限必须大于 0")
        if cookie and ("\r" in cookie or "\n" in cookie):
            raise ValueError("Cookie 不能包含换行符")

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bilibili.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if cookie:
            headers["Cookie"] = cookie.strip()

        self._owns_client = client is None
        self._client = client or httpx.Client(headers=headers, timeout=timeout)
        if client is not None:
            self._client.headers.update(headers)

        self._retries = retries
        self._retry_backoff = retry_backoff
        self._request_delay = request_delay
        self._max_root_pages = max_root_pages
        self._max_reply_pages = max_reply_pages
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._mixin_key: str | None = None
        self._mixin_key_loaded_at: float | None = None

    def __enter__(self) -> BilibiliAdapter:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self, video_reference: str) -> FetchResult:
        """Read every currently visible root and child reply for one video."""

        video = self.resolve_video(video_reference)
        stats = FetchStats()
        diagnostics: list[Diagnostic] = []
        comments: dict[int, Comment] = {}
        root_order: list[int] = []
        complete = True

        try:
            roots_complete = self._fetch_roots(
                video=video,
                comments=comments,
                root_order=root_order,
                diagnostics=diagnostics,
                stats=stats,
            )
            complete = complete and roots_complete
        except BilibiliError as error:
            diagnostics.append(self._diagnostic_from_error(error, scope="root_pagination"))
            complete = False

        # A root-page failure should not prevent already discovered branches from being completed.
        preview_root_ids = {comment.root_id for comment in comments.values() if not comment.is_root}
        for position, root_id in enumerate(root_order, start=1):
            root = comments.get(root_id)
            if root is None:
                continue
            has_preview = root_id in preview_root_ids
            if root.reply_count <= 0 and not has_preview:
                continue
            logger.info(
                "读取楼中楼 %s/%s（根评论 %s，接口计数 %s）",
                position,
                len(root_order),
                root_id,
                root.reply_count,
            )
            try:
                branch_complete = self._fetch_child_replies(
                    video=video,
                    root=root,
                    comments=comments,
                    diagnostics=diagnostics,
                    stats=stats,
                )
                complete = complete and branch_complete
            except BilibiliError as error:
                diagnostics.append(
                    self._diagnostic_from_error(error, scope=f"root:{root_id}:replies")
                )
                complete = False

        normalized = list(comments.values())
        stats.root_comments_fetched = sum(comment.is_root for comment in normalized)
        stats.reply_comments_fetched = len(normalized) - stats.root_comments_fetched
        stats.total_comments_fetched = len(normalized)
        self._warn_on_total_count_drift(normalized, diagnostics, stats)
        complete = complete and not any(item.severity == "error" for item in diagnostics)

        return FetchResult(
            video=video,
            comments=normalized,
            complete=complete,
            diagnostics=diagnostics,
            stats=stats,
        )

    def fetch_reference(self, reference: str) -> FetchResult:
        """Dispatch one input to discussion-sync or legacy full-video modes."""
        raw = reference.strip()
        if not raw:
            raise ParameterError("输入不能为空。")
        candidate = raw if "://" in raw else f"https://{raw}"
        host = (urlparse(candidate).hostname or "").lower()
        if host == "b23.tv":
            original_has_marker = self._url_has_comment_marker(candidate)
            expanded = self._resolve_short_link(candidate)
            expanded_has_marker = self._url_has_comment_marker(expanded)
            if original_has_marker or expanded_has_marker:
                return self.fetch_discussion(expanded)
            return self.fetch(expanded)
        if host and self._is_allowed_bilibili_host(host):
            if self._url_has_comment_marker(candidate):
                return self.fetch_discussion(candidate)
            return self.fetch(candidate)
        return self.fetch(raw)

    def fetch_discussion(self, comment_share_link: str) -> FetchResult:
        """Strictly sync one root discussion addressed by a comment share link.

        The root comment itself and its first reply page come from one
        ``/x/v2/reply/reply`` call. The main comment endpoint is never used.
        """
        raw = comment_share_link.strip()
        if not raw:
            raise ParameterError("评论链接不能为空。")
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed_candidate = urlparse(candidate)
        validate_url_authority(parsed_candidate, context="评论链接")
        host = (parsed_candidate.hostname or "").lower()
        if host == "b23.tv":
            candidate = self._resolve_short_link(candidate)
            parsed_candidate = urlparse(candidate)
            validate_url_authority(parsed_candidate, context="评论链接")
            host = (parsed_candidate.hostname or "").lower()
        if not self._is_allowed_bilibili_host(host):
            raise ParameterError("只接受 Bilibili 评论链接或 b23.tv 短链。")

        comment_reference = parse_comment_reference(candidate)
        video = self.resolve_video(candidate)
        discussion = build_discussion_reference(video, comment_reference)

        stats = FetchStats()
        diagnostics: list[Diagnostic] = []
        scope = f"root:{discussion.root_comment_id}:replies"

        try:
            data = self._request_api(
                CHILD_REPLY_ENDPOINT,
                params={
                    "oid": video.aid,
                    "type": 1,
                    "root": discussion.root_comment_id,
                    "pn": 1,
                    "ps": 20,
                },
                scope=scope,
            )
        except BilibiliError as error:
            diagnostics.append(self._diagnostic_from_error(error, scope=scope))
            return self._finalize_discussion_result(
                video=video,
                discussion=discussion,
                comments={},
                diagnostics=diagnostics,
                stats=stats,
            )

        if not isinstance(data, Mapping):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope=scope,
                    message="楼中楼接口缺少 data 对象。",
                )
            )
            return self._finalize_discussion_result(
                video=video,
                discussion=discussion,
                comments={},
                diagnostics=diagnostics,
                stats=stats,
            )

        stats.reply_pages_fetched += 1

        root_valid = True
        root_comment: Comment | None = None
        raw_root = data.get("root")
        if not isinstance(raw_root, Mapping):
            root_valid = False
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="root_metadata_missing",
                    scope=scope,
                    message="楼中楼响应缺少目标根评论 data.root 对象。",
                    details={"root_comment_id": discussion.root_comment_id},
                )
            )
        elif self._is_invisible_flag(raw_root.get("invisible")):
            root_valid = False
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="root_not_visible",
                    scope=scope,
                    message=(
                        "目标根评论当前不可见（invisible=true），本轮不视为完整同步；"
                        "这不代表评论已删除。"
                    ),
                    details={"root_comment_id": discussion.root_comment_id},
                )
            )
        else:
            root_comment = self._parse_comment(
                raw_root,
                video_id=video.bvid,
                scope=f"root:{discussion.root_comment_id}:metadata",
                diagnostics=diagnostics,
            )
            if root_comment is None:
                root_valid = False
            elif root_comment.comment_id != discussion.root_comment_id:
                root_valid = False
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        category="root_id_mismatch",
                        scope=scope,
                        message="楼中楼响应返回的根评论与请求的 root_comment_id 不一致。",
                        details={
                            "requested_root_comment_id": discussion.root_comment_id,
                            "actual_rpid": root_comment.comment_id,
                        },
                    )
                )
            elif not root_comment.is_root:
                root_valid = False
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        category="root_relationship_invalid",
                        scope=scope,
                        message="目标根评论的 root/parent 关系不是根节点。",
                        details={
                            "root_id": root_comment.root_id,
                            "parent_id": root_comment.parent_id,
                        },
                    )
                )

        page = data.get("page")
        expected_reply_count: int | None = None
        if isinstance(page, Mapping):
            api_count = self._optional_int(page.get("count"))
            if api_count is not None:
                expected_reply_count = api_count
                if root_comment is not None and api_count != root_comment.reply_count:
                    diagnostics.append(
                        Diagnostic(
                            severity="warning",
                            category="count_changed",
                            scope=scope,
                            message="根评论 rcount 与楼中楼接口 page.count 不一致，"
                            "以接口计数继续判断分页。",
                            details={
                                "rcount": root_comment.reply_count,
                                "page_count": api_count,
                            },
                        )
                    )
        if expected_reply_count is None and root_comment is not None:
            expected_reply_count = root_comment.reply_count
        if root_valid and expected_reply_count is not None:
            stats.expected_total_comments = 1 + expected_reply_count

        comments: dict[int, Comment] = {}
        if root_valid and root_comment is not None:
            self._upsert_comment(comments, root_comment, diagnostics=diagnostics, stats=stats)
        if "replies" not in data:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope=scope,
                    message="楼中楼响应缺少 replies 字段。",
                )
            )
            return self._finalize_discussion_result(
                video=video,
                discussion=discussion,
                comments=comments,
                diagnostics=diagnostics,
                stats=stats,
            )

        raw_replies = data.get("replies") or []
        if not isinstance(raw_replies, list):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope=scope,
                    message="楼中楼响应中的 replies 不是列表。",
                )
            )
            return self._finalize_discussion_result(
                video=video,
                discussion=discussion,
                comments=comments,
                diagnostics=diagnostics,
                stats=stats,
            )

        parsed_page_replies: list[Comment] = []
        for raw_item in raw_replies:
            comment = self._parse_comment(
                raw_item,
                video_id=video.bvid,
                scope=f"root:{discussion.root_comment_id}:reply_page:1",
                diagnostics=diagnostics,
            )
            if comment is None:
                continue
            parsed_page_replies.append(comment)

        kept_replies = [
            comment
            for comment in parsed_page_replies
            if comment.root_id == discussion.root_comment_id
        ]
        excluded_root_ids = sorted(
            {
                comment.root_id
                for comment in parsed_page_replies
                if comment.root_id != discussion.root_comment_id
            }
        )
        if excluded_root_ids:
            diagnostics.append(
                Diagnostic(
                    severity="warning",
                    category="foreign_root_reply_excluded",
                    scope=scope,
                    message="楼中楼响应包含不属于目标根讨论的回复，已排除。",
                    details={
                        "excluded_count": len(parsed_page_replies) - len(kept_replies),
                        "excluded_root_ids": excluded_root_ids,
                    },
                )
            )

        for reply in kept_replies:
            self._upsert_comment(comments, reply, diagnostics=diagnostics, stats=stats)

        if not root_valid:
            return self._finalize_discussion_result(
                video=video,
                discussion=discussion,
                comments=comments,
                diagnostics=diagnostics,
                stats=stats,
            )

        seen_api_ids = {reply.comment_id for reply in kept_replies}
        seen_page_fingerprints: set[tuple[int, ...]] = set()
        if seen_api_ids:
            seen_page_fingerprints.add(tuple(reply.comment_id for reply in kept_replies))

        if (
            not raw_replies
            and expected_reply_count is not None
            and len(seen_api_ids) < expected_reply_count
        ):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="pagination_incomplete",
                    scope=scope,
                    message="楼中楼在达到接口总数前提前返回空页。",
                    details={
                        "expected": expected_reply_count,
                        "actual": len(seen_api_ids),
                        "page": 1,
                    },
                )
            )
            return self._finalize_discussion_result(
                video=video,
                discussion=discussion,
                comments=comments,
                diagnostics=diagnostics,
                stats=stats,
            )

        if expected_reply_count is None or len(seen_api_ids) < expected_reply_count:
            try:
                self._fetch_reply_pages(
                    video=video,
                    root=root_comment,
                    comments=comments,
                    diagnostics=diagnostics,
                    stats=stats,
                    start_page=2,
                    seen_api_ids=seen_api_ids,
                    seen_page_fingerprints=seen_page_fingerprints,
                    expected_count=expected_reply_count,
                )
            except BilibiliError as error:
                diagnostics.append(self._diagnostic_from_error(error, scope=scope))
        else:
            self._warn_on_reply_count_drift(
                discussion.root_comment_id, expected_reply_count, seen_api_ids, diagnostics
            )

        return self._finalize_discussion_result(
            video=video,
            discussion=discussion,
            comments=comments,
            diagnostics=diagnostics,
            stats=stats,
        )

    def resolve_video(self, video_reference: str) -> VideoInfo:
        """Resolve BV/AV/numeric/Bilibili URL input to stable video metadata."""

        identifier, value = self._parse_video_reference(video_reference)
        params = {"bvid": value} if identifier == "bvid" else {"aid": value}
        data = self._request_api(VIEW_ENDPOINT, params=params, scope="video")
        if not isinstance(data, Mapping):
            raise ResponseParseError("视频信息接口缺少 data 对象。")

        try:
            aid = self._strict_int(data.get("aid"), field="aid")
            bvid = self._strict_string(data.get("bvid"), field="bvid")
            title = self._strict_string(data.get("title"), field="title")
            owner = data.get("owner")
            if not isinstance(owner, Mapping):
                raise ValueError("owner")
            owner_id = self._strict_int(owner.get("mid"), field="owner.mid")
            owner_name = self._strict_string(owner.get("name"), field="owner.name")
        except (TypeError, ValueError) as error:
            raise ResponseParseError(
                "视频信息响应缺少必需字段。", details={"field": str(error)}
            ) from error

        comment_count: int | None = None
        stat = data.get("stat")
        if isinstance(stat, Mapping):
            try:
                comment_count = self._strict_int(stat.get("reply"), field="stat.reply")
            except (TypeError, ValueError):
                comment_count = None

        return VideoInfo(
            aid=aid,
            bvid=bvid,
            title=title,
            owner_id=owner_id,
            owner_name=owner_name,
            visible_comment_count_hint=comment_count,
        )

    def _parse_video_reference(self, raw_reference: str) -> tuple[str, str | int]:
        reference = raw_reference.strip()
        if not reference:
            raise ParameterError("视频链接、BV 号或 AV 号不能为空。")

        bvid_match = re.fullmatch(_BVID_RE, reference)
        if bvid_match:
            return ("bvid", bvid_match.group(0))

        direct_aid = re.fullmatch(r"(?:av)?([1-9][0-9]*)", reference, re.IGNORECASE)
        if direct_aid:
            return ("aid", int(direct_aid.group(1)))

        candidate = reference if "://" in reference else f"https://{reference}"
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if not self._is_allowed_bilibili_host(host):
            raise ParameterError("只接受 Bilibili 视频链接、b23.tv 短链、BV 号或 AV 号。")

        if host == "b23.tv":
            candidate = self._resolve_short_link(candidate)
            parsed = urlparse(candidate)

        bvid_match = _BVID_RE.search(parsed.path)
        if bvid_match:
            return ("bvid", bvid_match.group(0))

        aid_match = _AID_RE.search(parsed.path)
        if aid_match:
            return ("aid", int(aid_match.group("aid")))

        raise ParameterError("无法从输入中识别 BV 号或 AV 号。")

    @staticmethod
    def _is_allowed_bilibili_host(host: str) -> bool:
        return host in _ALLOWED_BILIBILI_HOSTS or host.endswith(".bilibili.com")

    @staticmethod
    def _url_has_comment_marker(url: str) -> bool:
        """Detect any comment marker case-insensitively so routing never silently downgrades."""
        parsed = urlparse(url)
        query_names = {
            name.lower() for name, _value in parse_qsl(parsed.query, keep_blank_values=True)
        }
        if {"comment_root_id", "comment_secondary_id"} & query_names:
            return True
        fragment = parsed.fragment.strip()
        return bool(fragment) and fragment.lower().startswith("reply")

    @staticmethod
    def _is_invisible_flag(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1"}
        return value == 1

    def _finalize_discussion_result(
        self,
        *,
        video: VideoInfo,
        discussion: DiscussionReference,
        comments: dict[int, Comment],
        diagnostics: list[Diagnostic],
        stats: FetchStats,
    ) -> FetchResult:
        normalized = list(comments.values())
        stats.root_comments_fetched = sum(item.is_root for item in normalized)
        stats.reply_comments_fetched = len(normalized) - stats.root_comments_fetched
        stats.total_comments_fetched = len(normalized)
        self._warn_on_total_count_drift(normalized, diagnostics, stats)
        complete = not any(item.severity == "error" for item in diagnostics)
        return FetchResult(
            video=video,
            comments=normalized,
            complete=complete,
            diagnostics=diagnostics,
            stats=stats,
            discussion=discussion,
        )

    def _resolve_short_link(self, initial_url: str) -> str:
        """Resolve b23 redirects safely without requesting the final target.

        Every hop must be a valid http(s) Location on b23.tv until the first
        non-b23 target, which must be an allowed Bilibili host. That final URL
        is returned for parsing and is never requested here.
        """

        current_url = initial_url
        seen_urls: set[str] = set()
        for _hop in range(_MAX_SHORT_LINK_HOPS):
            if current_url in seen_urls:
                raise ParameterError("b23.tv 短链跳转出现循环，已拒绝继续处理。")
            seen_urls.add(current_url)
            validate_url_authority(urlparse(current_url), context="b23.tv 短链")
            try:
                response = self._http_get(current_url, scope="short_link")
            except NetworkError as error:
                if isinstance(error.__cause__, httpx.RemoteProtocolError):
                    raise ParameterError(
                        "b23.tv 短链跳转协议畸形，已拒绝继续处理。"
                    ) from error.__cause__
                raise
            if response.status_code not in _REDIRECT_STATUSES:
                raise ParameterError("b23.tv 短链未返回有效的跳转响应。")

            location = response.headers.get("Location")
            if not location:
                raise ParameterError("b23.tv 返回了不含 Location 的跳转响应。")
            next_url = urljoin(str(response.url), location)
            parsed_next = urlparse(next_url)
            validate_url_authority(parsed_next, context="b23.tv 跳转目标")
            next_host = (parsed_next.hostname or "").lower()
            if next_host == "b23.tv":
                current_url = next_url
                continue
            if not self._is_allowed_bilibili_host(next_host):
                raise ParameterError("b23.tv 短链跳转到了非 Bilibili 地址，已拒绝继续处理。")
            return next_url

        raise ParameterError("b23.tv 短链跳转次数超过安全上限。")

    def _fetch_roots(
        self,
        *,
        video: VideoInfo,
        comments: dict[int, Comment],
        root_order: list[int],
        diagnostics: list[Diagnostic],
        stats: FetchStats,
    ) -> bool:
        offset = ""
        seen_offsets: set[str] = set()

        while True:
            if stats.root_pages_fetched >= self._max_root_pages:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        category="pagination_limit",
                        scope="root_pagination",
                        message="主评论分页达到安全上限，结果不完整。",
                        details={"max_pages": self._max_root_pages},
                    )
                )
                return False

            if offset in seen_offsets:
                raise PaginationError(
                    "主评论分页游标发生循环。", details={"offset": offset or "<first-page>"}
                )
            seen_offsets.add(offset)

            data = self._fetch_main_page(video=video, offset=offset)
            stats.root_pages_fetched += 1
            logger.info("已读取主评论第 %s 页", stats.root_pages_fetched)

            cursor = data.get("cursor")
            if not isinstance(cursor, Mapping):
                raise ResponseParseError("主评论响应缺少 cursor 分页对象。")

            all_count = self._optional_int(cursor.get("all_count"))
            if all_count is not None:
                stats.expected_total_comments = all_count

            if "replies" not in data:
                raise ResponseParseError("主评论响应缺少 replies 字段。")
            raw_replies = data.get("replies")
            if raw_replies is None:
                raw_replies = []
            if not isinstance(raw_replies, list):
                raise ResponseParseError("主评论响应中的 replies 不是列表。")

            top_replies = data.get("top_replies") or []
            if not isinstance(top_replies, list):
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        category="response_parse",
                        scope="root_pagination",
                        message="置顶评论字段 top_replies 不是列表。",
                    )
                )
                top_replies = []

            for raw_item in [*top_replies, *raw_replies]:
                comment = self._parse_comment(
                    raw_item,
                    video_id=video.bvid,
                    scope=f"root_page:{stats.root_pages_fetched}",
                    diagnostics=diagnostics,
                )
                if comment is None:
                    continue
                if not comment.is_root:
                    diagnostics.append(
                        Diagnostic(
                            severity="error",
                            category="invalid_relationship",
                            scope=f"comment:{comment.comment_id}",
                            message="主评论接口返回了非根节点关系。",
                            details={
                                "root_id": comment.root_id,
                                "parent_id": comment.parent_id,
                            },
                        )
                    )
                    continue

                is_new = self._upsert_comment(
                    comments, comment, diagnostics=diagnostics, stats=stats
                )
                if is_new:
                    root_order.append(comment.comment_id)
                self._collect_embedded_replies(
                    raw_item,
                    video=video,
                    comments=comments,
                    diagnostics=diagnostics,
                    stats=stats,
                )

            is_end = cursor.get("is_end")
            if (
                is_end is True
                or is_end == 1
                or (isinstance(is_end, str) and is_end.lower() == "true")
            ):
                return True

            pagination_reply = cursor.get("pagination_reply")
            next_offset = (
                pagination_reply.get("next_offset")
                if isinstance(pagination_reply, Mapping)
                else None
            )
            if isinstance(next_offset, str) and next_offset:
                offset = next_offset
                continue
            explicitly_not_ended = (
                is_end is False
                or is_end == 0
                or (isinstance(is_end, str) and is_end.lower() == "false")
            )
            if not raw_replies and not explicitly_not_ended:
                return True
            raise PaginationError("主评论响应表示尚未结束，但没有提供有效的 next_offset。")

    def _fetch_main_page(self, *, video: VideoInfo, offset: str) -> Mapping[str, Any]:
        params = {
            "oid": video.aid,
            "type": 1,
            "mode": 3,
            "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
            "plat": 1,
            "seek_rpid": "",
            "web_location": 1315875,
        }

        # A WBI key can rotate. Refresh once for signature/access codes before surfacing the error.
        for force_refresh in (False, True):
            mixin_key = self._get_mixin_key(force_refresh=force_refresh)
            signed = sign_wbi_params(
                params,
                mixin_key=mixin_key,
                timestamp=int(self._clock()),
            )
            try:
                data = self._request_api(
                    MAIN_REPLY_ENDPOINT,
                    params=sorted(signed.items()),
                    scope="root_pagination",
                )
                if not isinstance(data, Mapping):
                    raise ResponseParseError("主评论接口缺少 data 对象。")
                return data
            except AccessDeniedError as error:
                if not force_refresh and error.api_code in {-403, -352}:
                    continue
                raise

        raise AccessDeniedError("WBI 签名刷新后仍无法访问主评论接口。")

    def _fetch_child_replies(
        self,
        *,
        video: VideoInfo,
        root: Comment,
        comments: dict[int, Comment],
        diagnostics: list[Diagnostic],
        stats: FetchStats,
    ) -> bool:
        return self._fetch_reply_pages(
            video=video,
            root=root,
            comments=comments,
            diagnostics=diagnostics,
            stats=stats,
            start_page=1,
        )

    def _fetch_reply_pages(
        self,
        *,
        video: VideoInfo,
        root: Comment,
        comments: dict[int, Comment],
        diagnostics: list[Diagnostic],
        stats: FetchStats,
        start_page: int,
        seen_api_ids: set[int] | None = None,
        seen_page_fingerprints: set[tuple[int, ...]] | None = None,
        expected_count: int | None = None,
    ) -> bool:
        page_number = start_page
        if expected_count is None:
            expected_count = root.reply_count
        seen_api_ids = set() if seen_api_ids is None else seen_api_ids
        seen_page_fingerprints = set() if seen_page_fingerprints is None else seen_page_fingerprints

        while True:
            if page_number > self._max_reply_pages:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        category="pagination_limit",
                        scope=f"root:{root.comment_id}:replies",
                        message="楼中楼分页达到安全上限，当前分支不完整。",
                        details={"max_pages": self._max_reply_pages},
                    )
                )
                return False

            data = self._request_api(
                CHILD_REPLY_ENDPOINT,
                params={
                    "oid": video.aid,
                    "type": 1,
                    "root": root.comment_id,
                    "pn": page_number,
                    "ps": 20,
                },
                scope=f"root:{root.comment_id}:reply_page:{page_number}",
            )
            if not isinstance(data, Mapping):
                raise ResponseParseError("楼中楼接口缺少 data 对象。")
            stats.reply_pages_fetched += 1

            raw_root = data.get("root")
            if isinstance(raw_root, Mapping):
                parsed_root = self._parse_comment(
                    raw_root,
                    video_id=video.bvid,
                    scope=f"root:{root.comment_id}:metadata",
                    diagnostics=diagnostics,
                )
                if parsed_root is not None:
                    self._upsert_comment(
                        comments, parsed_root, diagnostics=diagnostics, stats=stats
                    )

            page = data.get("page")
            if isinstance(page, Mapping):
                api_count = self._optional_int(page.get("count"))
                if api_count is not None:
                    if api_count != expected_count:
                        diagnostics.append(
                            Diagnostic(
                                severity="warning",
                                category="count_changed",
                                scope=f"root:{root.comment_id}:replies",
                                message="抓取期间楼中楼计数发生变化，以最新接口计数继续判断分页。",
                                details={"previous": expected_count, "current": api_count},
                            )
                        )
                    expected_count = api_count

            if "replies" not in data:
                raise ResponseParseError("楼中楼响应缺少 replies 字段。")
            raw_replies = data.get("replies")
            if raw_replies is None:
                raw_replies = []
            if not isinstance(raw_replies, list):
                raise ResponseParseError("楼中楼响应中的 replies 不是列表。")

            page_ids: list[int] = []
            for raw_item in raw_replies:
                comment = self._parse_comment(
                    raw_item,
                    video_id=video.bvid,
                    scope=f"root:{root.comment_id}:reply_page:{page_number}",
                    diagnostics=diagnostics,
                )
                if comment is None:
                    continue
                page_ids.append(comment.comment_id)
                seen_api_ids.add(comment.comment_id)
                self._upsert_comment(comments, comment, diagnostics=diagnostics, stats=stats)

            fingerprint = tuple(page_ids)
            if fingerprint and fingerprint in seen_page_fingerprints:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        category="pagination_loop",
                        scope=f"root:{root.comment_id}:replies",
                        message="楼中楼接口重复返回相同页面，已停止以防止死循环。",
                        details={"page": page_number, "comment_ids": page_ids},
                    )
                )
                return False
            if fingerprint:
                seen_page_fingerprints.add(fingerprint)

            if not raw_replies:
                if expected_count is not None and len(seen_api_ids) < expected_count:
                    diagnostics.append(
                        Diagnostic(
                            severity="error",
                            category="pagination_incomplete",
                            scope=f"root:{root.comment_id}:replies",
                            message="楼中楼在达到接口总数前提前返回空页。",
                            details={
                                "expected": expected_count,
                                "actual": len(seen_api_ids),
                                "page": page_number,
                            },
                        )
                    )
                    return False
                self._warn_on_reply_count_drift(
                    root.comment_id, expected_count, seen_api_ids, diagnostics
                )
                return True

            if (
                expected_count is not None
                and expected_count > 0
                and len(seen_api_ids) >= expected_count
            ):
                self._warn_on_reply_count_drift(
                    root.comment_id, expected_count, seen_api_ids, diagnostics
                )
                return True

            page_number += 1

    def _collect_embedded_replies(
        self,
        raw_root: Any,
        *,
        video: VideoInfo,
        comments: dict[int, Comment],
        diagnostics: list[Diagnostic],
        stats: FetchStats,
    ) -> None:
        if not isinstance(raw_root, Mapping):
            return
        embedded = raw_root.get("replies") or []
        if not isinstance(embedded, list):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope="embedded_replies",
                    message="主评论内嵌 replies 字段不是列表。",
                )
            )
            return
        for raw_item in embedded:
            comment = self._parse_comment(
                raw_item,
                video_id=video.bvid,
                scope="embedded_replies",
                diagnostics=diagnostics,
            )
            if comment is not None:
                self._upsert_comment(comments, comment, diagnostics=diagnostics, stats=stats)

    def _parse_comment(
        self,
        raw_item: Any,
        *,
        video_id: str,
        scope: str,
        diagnostics: list[Diagnostic],
    ) -> Comment | None:
        if not isinstance(raw_item, Mapping):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope=scope,
                    message="评论条目不是 JSON 对象，已跳过。",
                )
            )
            return None

        try:
            comment_id = self._strict_int(raw_item.get("rpid"), field="rpid")
            root_id = self._strict_int(raw_item.get("root"), field="root")
            parent_id = self._strict_int(raw_item.get("parent"), field="parent")
        except (TypeError, ValueError) as error:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope=scope,
                    message="评论缺少建树所需的 rpid/root/parent 字段，已跳过。",
                    details={"field": str(error)},
                )
            )
            return None

        member = raw_item.get("member")
        content_object = raw_item.get("content")
        missing_fields: list[str] = []

        user_id = 0
        username = ""
        if isinstance(member, Mapping):
            try:
                user_id = self._strict_int(member.get("mid"), field="member.mid")
            except (TypeError, ValueError):
                missing_fields.append("member.mid")
            raw_username = member.get("uname")
            if isinstance(raw_username, str):
                username = raw_username
            else:
                missing_fields.append("member.uname")
        else:
            missing_fields.extend(("member.mid", "member.uname"))

        content = ""
        if isinstance(content_object, Mapping) and isinstance(content_object.get("message"), str):
            content = content_object["message"]
        else:
            missing_fields.append("content.message")

        try:
            created_at = self._strict_int(raw_item.get("ctime"), field="ctime")
        except (TypeError, ValueError):
            created_at = 0
            missing_fields.append("ctime")

        parsed_reply_count = self._optional_int(raw_item.get("rcount"))
        reply_count = parsed_reply_count or 0
        if parsed_reply_count is None and root_id == 0 and parent_id == 0:
            missing_fields.append("rcount")
        if missing_fields:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="response_parse",
                    scope=f"comment:{comment_id}",
                    message="评论展示或身份字段缺失，已使用安全占位值；结果标记为不完整。",
                    details={"fields": sorted(set(missing_fields))},
                )
            )

        return Comment(
            comment_id=comment_id,
            user_id=user_id,
            username=username,
            content=content,
            root_id=root_id,
            parent_id=parent_id,
            created_at=created_at,
            video_id=video_id,
            reply_count=max(reply_count, 0),
        )

    def _upsert_comment(
        self,
        comments: dict[int, Comment],
        incoming: Comment,
        *,
        diagnostics: list[Diagnostic],
        stats: FetchStats,
    ) -> bool:
        existing = comments.get(incoming.comment_id)
        if existing is None:
            comments[incoming.comment_id] = incoming
            return True

        stats.duplicate_comments_seen += 1
        if (existing.root_id, existing.parent_id) != (incoming.root_id, incoming.parent_id):
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    category="relationship_conflict",
                    scope=f"comment:{incoming.comment_id}",
                    message="同一 comment_id 出现互相冲突的 root/parent 关系。",
                    details={
                        "existing": [existing.root_id, existing.parent_id],
                        "incoming": [incoming.root_id, incoming.parent_id],
                    },
                )
            )
            return False

        comments[incoming.comment_id] = replace(
            existing,
            user_id=incoming.user_id or existing.user_id,
            username=incoming.username or existing.username,
            content=incoming.content or existing.content,
            created_at=incoming.created_at or existing.created_at,
            reply_count=max(existing.reply_count, incoming.reply_count),
        )
        return False

    def _get_mixin_key(self, *, force_refresh: bool = False) -> str:
        now = self._monotonic()
        if (
            not force_refresh
            and self._mixin_key is not None
            and self._mixin_key_loaded_at is not None
            and now - self._mixin_key_loaded_at < 600
        ):
            return self._mixin_key

        # Anonymous nav responses use code=-101 while still returning usable wbi_img data.
        data = self._request_api(NAV_ENDPOINT, scope="wbi_key", allowed_api_codes={-101})
        if not isinstance(data, Mapping):
            raise ResponseParseError("nav 接口缺少 data 对象，无法生成 WBI 签名。")
        wbi_img = data.get("wbi_img")
        if not isinstance(wbi_img, Mapping):
            raise ResponseParseError("nav 响应缺少 wbi_img，无法生成 WBI 签名。")
        img_url = wbi_img.get("img_url")
        sub_url = wbi_img.get("sub_url")
        if not isinstance(img_url, str) or not isinstance(sub_url, str):
            raise ResponseParseError("nav 响应中的 WBI 图片地址无效。")
        try:
            self._mixin_key = derive_mixin_key(img_url, sub_url)
        except ValueError as error:
            raise ResponseParseError(str(error)) from error
        self._mixin_key_loaded_at = now
        return self._mixin_key

    def _request_api(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, str]] | None = None,
        scope: str,
        allowed_api_codes: set[int] | None = None,
    ) -> Any:
        response = self._http_get(url, params=params, scope=scope)
        try:
            payload = response.json()
        except ValueError as error:
            raise ResponseParseError(
                "Bilibili 返回了无法解析的 JSON。",
                status_code=response.status_code,
                details={"scope": scope},
            ) from error
        if not isinstance(payload, Mapping):
            raise ResponseParseError("Bilibili API 响应不是 JSON 对象。")

        code = self._optional_int(payload.get("code"))
        message = payload.get("message") or payload.get("msg") or "未知错误"
        if code == 0 or (allowed_api_codes is not None and code in allowed_api_codes):
            return payload.get("data")
        if code == -101:
            raise AuthenticationError("Bilibili 登录态无效或已过期。", api_code=code)
        if code in {-352, -403, -412}:
            raise AccessDeniedError(
                f"Bilibili 拒绝访问：{message}", api_code=code, details={"scope": scope}
            )
        if code in {-799, -509}:
            raise RateLimitError(
                f"Bilibili 请求频率受限（code={code}）：{message}",
                api_code=code,
                retryable=True,
                details={"scope": scope},
            )
        if code == -400:
            raise ParameterError(f"Bilibili 接口参数错误：{message}", api_code=code)
        if code in {-404, 100100404}:
            raise BusinessError(f"视频或评论不存在：{message}", api_code=code)
        if code == 12002:
            raise BusinessError("该视频评论区已关闭。", api_code=code)
        raise BusinessError(
            f"Bilibili 业务错误（code={code}）：{message}",
            api_code=code,
            details={"scope": scope},
        )

    def _http_get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | list[tuple[str, str]] | None = None,
        scope: str,
    ) -> httpx.Response:
        attempts = self._retries + 1
        for attempt in range(attempts):
            self._pace_request()
            try:
                response = self._client.get(
                    url,
                    params=params,
                    follow_redirects=False,
                )
            except httpx.RequestError as error:
                if attempt + 1 < attempts:
                    self._sleep(self._retry_backoff * (2**attempt))
                    continue
                raise NetworkError(
                    f"网络请求失败：{error.__class__.__name__}",
                    retryable=True,
                    details={"scope": scope, "attempts": attempts},
                ) from error

            status = response.status_code
            if status in {403, 412}:
                error_type = RateLimitError if status == 412 else AccessDeniedError
                raise error_type(
                    f"Bilibili 返回 HTTP {status}，可能是登录态失效或触发风控。",
                    status_code=status,
                    details={"scope": scope},
                )
            if status == 429 or status >= 500:
                if attempt + 1 < attempts:
                    self._sleep(self._retry_backoff * (2**attempt))
                    continue
                if status == 429:
                    raise RateLimitError(
                        "Bilibili 请求过于频繁（HTTP 429）。",
                        status_code=status,
                        retryable=True,
                        details={"scope": scope, "attempts": attempts},
                    )
                raise HttpError(
                    f"Bilibili 服务暂时不可用（HTTP {status}）。",
                    status_code=status,
                    retryable=True,
                    details={"scope": scope, "attempts": attempts},
                )
            if status >= 400:
                raise HttpError(
                    f"Bilibili 返回 HTTP {status}。",
                    status_code=status,
                    details={"scope": scope},
                )
            return response

        raise NetworkError("请求在没有响应的情况下结束。", details={"scope": scope})

    def _pace_request(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self._request_delay - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()

    @staticmethod
    def _strict_int(value: Any, *, field: str) -> int:
        if isinstance(value, bool):
            raise TypeError(field)
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise TypeError(field) from error
        if result < 0:
            raise ValueError(field)
        return result

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _strict_string(value: Any, *, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise TypeError(field)
        return value

    @staticmethod
    def _diagnostic_from_error(error: BilibiliError, *, scope: str) -> Diagnostic:
        details = {
            "retryable": error.retryable,
            **error.details,
        }
        if error.status_code is not None:
            details["status_code"] = error.status_code
        if error.api_code is not None:
            details["api_code"] = error.api_code
        return Diagnostic(
            severity="error",
            category=error.category,
            scope=scope,
            message=error.message,
            details=details,
        )

    @staticmethod
    def _warn_on_total_count_drift(
        comments: list[Comment],
        diagnostics: list[Diagnostic],
        stats: FetchStats,
    ) -> None:
        expected = stats.expected_total_comments
        if expected is None:
            return
        actual = len(comments)
        if actual != expected:
            diagnostics.append(
                Diagnostic(
                    severity="warning",
                    category="count_changed",
                    scope="fetch",
                    message="唯一评论总数与接口计数不同；分页均已按接口终止条件结束，可能是置顶、删除或抓取期间数据变化。",
                    details={"expected": expected, "actual": actual},
                )
            )

    @staticmethod
    def _warn_on_reply_count_drift(
        root_id: int,
        expected: int | None,
        seen_ids: set[int],
        diagnostics: list[Diagnostic],
    ) -> None:
        if expected is None or len(seen_ids) == expected:
            return
        diagnostics.append(
            Diagnostic(
                severity="warning",
                category="count_changed",
                scope=f"root:{root_id}:replies",
                message="楼中楼唯一评论数与接口计数不同；分页已按接口终止条件完成。",
                details={"expected": expected, "actual": len(seen_ids)},
            )
        )
