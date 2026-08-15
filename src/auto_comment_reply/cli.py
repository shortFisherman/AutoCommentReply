"""Read-only command-line entry point for MVP1."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from .adapter import BilibiliAdapter
from .errors import BilibiliError
from .output import build_output_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-comment-reply",
        description="完整读取一条 Bilibili 视频的可见评论树（MVP1，只读）。",
    )
    parser.add_argument("video", help="BV 号、AV 号、数字 aid、视频链接或 b23.tv 短链")
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="JSON 输出路径；默认 '-' 表示标准输出",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="从本机私有文件读取 Cookie；未指定时读取 BILIBILI_COOKIE 环境变量",
    )
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的输出文件")
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数（默认 15）")
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="网络和临时服务错误重试次数（默认 2）",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.25,
        help="连续请求间的最小间隔秒数（默认 0.25）",
    )
    parser.add_argument(
        "--max-root-pages",
        type=int,
        default=10_000,
        help="主评论分页安全上限；触发后输出标记为不完整",
    )
    parser.add_argument(
        "--max-reply-pages",
        type=int,
        default=10_000,
        help="单个楼中楼分页安全上限；触发后输出标记为不完整",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="只显示错误日志")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示调试日志")
    return parser


def _load_cookie(cookie_file: Path | None) -> str | None:
    if cookie_file is None:
        value = os.environ.get("BILIBILI_COOKIE", "").strip()
        return value or None

    try:
        value = cookie_file.read_text(encoding="utf-8-sig").strip()
    except OSError as error:
        raise ValueError(f"无法读取 Cookie 文件：{error}") from error
    if not value:
        raise ValueError("Cookie 文件为空。")
    if "\r" in value or "\n" in value:
        raise ValueError("Cookie 文件必须只包含一行 Cookie 字符串。")
    return value


def _write_output(path: Path, content: str, *, force: bool) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise FileExistsError(f"输出文件已存在：{path}（使用 --force 才会覆盖）")
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)

    output_path = None if args.output == "-" else Path(args.output)
    if output_path is not None and output_path.exists() and not args.force:
        logging.error("输出文件已存在：%s（使用 --force 才会覆盖）", output_path)
        return 1

    try:
        cookie = _load_cookie(args.cookie_file)
        if cookie is None:
            logging.info("未提供 Cookie：将读取匿名账号当前可见的评论。")

        with BilibiliAdapter(
            cookie=cookie,
            timeout=args.timeout,
            retries=args.retries,
            request_delay=args.request_delay,
            max_root_pages=args.max_root_pages,
            max_reply_pages=args.max_reply_pages,
        ) as adapter:
            result = adapter.fetch(args.video)

        document = build_output_document(result)
        indent = None if args.compact else 2
        content = json.dumps(document, ensure_ascii=False, indent=indent) + "\n"

        if output_path is None:
            sys.stdout.write(content)
        else:
            _write_output(output_path, content, force=args.force)
            logging.info("JSON 已写入 %s", output_path.resolve())

        if document["complete"]:
            logging.info(
                "读取完成：根评论 %s，回复 %s，对话链 %s。",
                document["stats"]["root_comments_fetched"],
                document["stats"]["reply_comments_fetched"],
                document["stats"]["conversation_chains"],
            )
            return 0

        logging.error("读取结果不完整；请查看 JSON 中的 diagnostics。")
        return 2
    except (BilibiliError, ValueError, OSError) as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
