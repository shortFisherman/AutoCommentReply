from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_comment_reply import cli
from auto_comment_reply.errors import ParameterError
from auto_comment_reply.models import Comment, Diagnostic, FetchResult, FetchStats, VideoInfo
from auto_comment_reply.reference import DiscussionReference


def make_result(
    *,
    complete: bool,
    discussion: DiscussionReference | None = None,
) -> FetchResult:
    diagnostics = []
    if not complete:
        diagnostics.append(
            Diagnostic(
                severity="error",
                category="pagination",
                scope="test",
                message="partial",
            )
        )
    return FetchResult(
        video=VideoInfo(
            aid=42,
            bvid="BV1xx411c7mD",
            title="fixture",
            owner_id=7,
            owner_name="owner",
            visible_comment_count_hint=1,
        ),
        comments=[
            Comment(
                comment_id=100,
                user_id=8,
                username="user",
                content="hello",
                root_id=0,
                parent_id=0,
                created_at=1,
                video_id="BV1xx411c7mD",
            )
        ],
        complete=complete,
        diagnostics=diagnostics,
        stats=FetchStats(
            expected_total_comments=1,
            root_comments_fetched=1,
            total_comments_fetched=1,
        ),
        discussion=discussion,
    )


def fake_adapter_class(result: FetchResult | Exception, captured: dict[str, object]):
    class FakeAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def fetch_reference(self, reference: str) -> FetchResult:
            captured["reference"] = reference
            if isinstance(result, Exception):
                raise result
            return result

    return FakeAdapter


def test_cli_complete_result_prints_json_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=True), captured)
    )
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)

    exit_code = cli.main(["BV1xx411c7mD", "--compact", "--quiet"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["complete"] is True
    assert captured["reference"] == "BV1xx411c7mD"
    assert captured["cookie"] is None


def test_cli_share_link_dispatches_targeted_fetch_and_exposes_discussion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    result = make_result(
        complete=True,
        discussion=DiscussionReference(
            platform="bilibili",
            object_type="video",
            aid=42,
            bvid="BV1xx411c7mD",
            root_comment_id=100,
            focus_comment_id=None,
        ),
    )
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)

    link = "https://www.bilibili.com/video/BV1xx411c7mD/?comment_root_id=100"
    exit_code = cli.main([link, "--compact", "--quiet"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["reference"] == link
    assert document["discussion"]["root_comment_id"] == 100
    assert document["discussion"]["identity"] == {
        "platform": "bilibili",
        "object_type": "video",
        "oid": 42,
        "root_comment_id": 100,
    }


def test_cli_incomplete_result_is_written_and_returns_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=False), captured)
    )
    output = tmp_path / "comments.json"

    exit_code = cli.main(["BV1xx411c7mD", "-o", str(output), "--quiet"])

    assert exit_code == 2
    assert json.loads(output.read_text(encoding="utf-8"))["complete"] is False


def test_cli_fatal_adapter_error_returns_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "BilibiliAdapter",
        fake_adapter_class(ParameterError("bad video"), captured),
    )

    exit_code = cli.main(["invalid", "--quiet"])

    assert exit_code == 1
    assert "bad video" in caplog.text


def test_cookie_file_takes_precedence_and_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    secret = "SESSDATA=super-secret; DedeUserID=7"
    cookie_file = tmp_path / "private.cookie"
    cookie_file.write_text(secret, encoding="utf-8-sig")
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=environment")
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=True), captured)
    )

    exit_code = cli.main(
        ["BV1xx411c7mD", "--cookie-file", str(cookie_file), "--compact", "--quiet"]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    assert captured["cookie"] == secret
    assert secret not in streams.out
    assert secret not in streams.err


def test_write_output_requires_force_to_replace_existing_file(tmp_path: Path) -> None:
    output = tmp_path / "comments.json"
    output.write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError):
        cli._write_output(output, "new", force=False)

    cli._write_output(output, "new", force=True)
    assert output.read_text(encoding="utf-8") == "new"


def test_cli_existing_output_returns_one_before_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=True), captured)
    )
    output = tmp_path / "comments.json"
    output.write_text("existing", encoding="utf-8")

    exit_code = cli.main(["BV1xx411c7mD", "-o", str(output), "--quiet"])

    assert exit_code == 1
    assert captured == {}
    assert "输出文件已存在" in caplog.text
