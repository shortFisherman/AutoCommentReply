"""Shared offline fixture builders for the M2 local-auth viewer tests.

All fixtures use MockTransport so the suite never touches the network and
never contains real credentials. The cookie secret below is unique and
completely fake; tests assert it never leaks into observable output.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

IMG_URL = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
SUB_URL = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
BVID = "BV1xx411c7mD"
VIEWER_MID = 123456
UNIQUE_SECRET = "SESSDATA=opdeepseekflash_m2_fake_secret_7f3a9c21;bili_jct=fake_jct_4b8d2e60"


def api_response(request: httpx.Request, data: Any, *, code: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": code, "message": "OK" if code == 0 else "denied", "data": data},
        request=request,
    )


def raw_comment(
    rpid: int,
    *,
    root: int = 0,
    parent: int = 0,
    rcount: int = 0,
    mid: Any = None,
    include_mid: bool = True,
    username: str | None = None,
    replies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rpid": rpid,
        "root": root,
        "parent": parent,
        "rcount": rcount,
        "ctime": rpid,
        "content": {"message": f"comment-{rpid}"},
    }
    member: dict[str, Any] = {}
    if include_mid:
        member["mid"] = str(rpid + 1_000) if mid is None else mid
    member["uname"] = username if username is not None else f"user-{rpid}"
    item["member"] = member
    if replies is not None:
        item["replies"] = replies
    return item


def share_url(root: int, *, focus: int | None = None, bvid: str = BVID) -> str:
    url = f"https://www.bilibili.com/video/{bvid}/?comment_root_id={root}"
    if focus is not None:
        url = f"{url}&comment_secondary_id={focus}"
    return url


def view_data() -> dict[str, Any]:
    return {
        "aid": 42,
        "bvid": BVID,
        "title": "fixture video",
        "owner": {"mid": 7, "name": "owner"},
        "stat": {"reply": 7},
    }


def login_nav_data(
    *,
    is_login: bool = True,
    mid: Any = VIEWER_MID,
    uname: Any = "display-only",
    include_wbi: bool = True,
) -> dict[str, Any]:
    """Build a logged-in nav data payload; ``None`` omits optional fields."""

    data: dict[str, Any] = {"isLogin": is_login}
    if mid is not None:
        data["mid"] = mid
    if uname is not None:
        data["uname"] = uname
    if include_wbi:
        data["wbi_img"] = {"img_url": IMG_URL, "sub_url": SUB_URL}
    return data


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def discussion_reply_payload(
    root: dict[str, Any],
    replies: list[dict[str, Any]] | None = None,
    *,
    count: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "page": {"num": 1, "size": 20, "count": count},
        "root": root,
        "replies": replies if replies is not None else [],
    }
    return payload
