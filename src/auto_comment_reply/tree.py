"""Comment graph validation, tree construction, and conversation-chain extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .models import Comment, Diagnostic


class CommentGraphError(ValueError):
    """Raised when a requested parent chain cannot be resolved safely."""


@dataclass(slots=True)
class CommentNode:
    comment: Comment
    children: list[CommentNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        root_document: dict[str, Any] = {
            "comment": self.comment.to_dict(),
            "children": [],
        }
        stack: list[tuple[CommentNode, dict[str, Any]]] = [(self, root_document)]
        while stack:
            node, document = stack.pop()
            child_documents: list[dict[str, Any]] = []
            document["children"] = child_documents
            for child in node.children:
                child_document: dict[str, Any] = {
                    "comment": child.comment.to_dict(),
                    "children": [],
                }
                child_documents.append(child_document)
                stack.append((child, child_document))
        return root_document


@dataclass(slots=True)
class TreeBuildResult:
    trees: list[CommentNode]
    orphans: list[Comment]
    duplicate_comment_ids: list[int]
    diagnostics: list[Diagnostic]
    comment_index: dict[int, Comment]

    def conversation_chains(self) -> list[list[int]]:
        """Return every root-to-leaf branch as normalized comment IDs."""

        chains: list[list[int]] = []
        for root in self.trees:
            stack: list[tuple[CommentNode, list[int]]] = [(root, [])]
            while stack:
                node, prefix = stack.pop()
                path = [*prefix, node.comment.comment_id]
                if not node.children:
                    chains.append(path)
                    continue
                for child in reversed(node.children):
                    stack.append((child, path))
        return chains


def _comment_sort_key(comment: Comment) -> tuple[int, int]:
    return (comment.created_at, comment.comment_id)


def trace_to_root(comment_id: int, comments: dict[int, Comment] | list[Comment]) -> list[Comment]:
    """Resolve one comment's direct-parent chain and return it in root-first order."""

    if isinstance(comments, dict):
        index = comments
    else:
        index = {}
        for comment in comments:
            index.setdefault(comment.comment_id, comment)

    if comment_id not in index:
        raise CommentGraphError(f"读取结果中不存在评论 {comment_id}")

    reverse_path: list[Comment] = []
    visited: set[int] = set()
    current_id = comment_id

    while True:
        if current_id in visited:
            raise CommentGraphError(f"评论父链存在循环，重复节点为 {current_id}")
        visited.add(current_id)

        current = index.get(current_id)
        if current is None:
            raise CommentGraphError(f"评论父链缺少节点 {current_id}")
        reverse_path.append(current)

        if current.is_root:
            root_id = current.comment_id
            break
        if current.parent_id == 0:
            raise CommentGraphError(f"非根评论 {current.comment_id} 的 parent_id 为 0")
        current_id = current.parent_id

    path = list(reversed(reverse_path))
    for comment in path[1:]:
        if comment.root_id != root_id:
            raise CommentGraphError(
                f"评论 {comment.comment_id} 的 root_id={comment.root_id}，"
                f"但父链落在根评论 {root_id}"
            )
    return path


def build_comment_forest(comments: list[Comment]) -> TreeBuildResult:
    """Build validated trees while preserving broken nodes as explicit orphans."""

    index: dict[int, Comment] = {}
    duplicate_ids: set[int] = set()
    diagnostics: list[Diagnostic] = []

    for comment in comments:
        if comment.comment_id in index:
            duplicate_ids.add(comment.comment_id)
            continue
        index[comment.comment_id] = comment

    if duplicate_ids:
        diagnostics.append(
            Diagnostic(
                severity="warning",
                category="duplicate_comment",
                scope="tree",
                message="输入中出现重复 comment_id，建树时保留首次出现的记录。",
                details={"comment_ids": sorted(duplicate_ids)},
            )
        )

    # A cached value is the resolved root ID; None means the chain is invalid.
    resolved: dict[int, int | None] = {}
    issue_keys: set[tuple[Any, ...]] = set()

    def add_error(category: str, message: str, details: dict[str, Any]) -> None:
        key = (category, json.dumps(details, ensure_ascii=False, sort_keys=True))
        if key in issue_keys:
            return
        issue_keys.add(key)
        diagnostics.append(
            Diagnostic(
                severity="error",
                category=category,
                scope="tree",
                message=message,
                details=details,
            )
        )

    for start_id in index:
        if start_id in resolved:
            continue

        path: list[int] = []
        positions: dict[int, int] = {}
        cursor = start_id
        invalid = False

        while cursor not in resolved:
            if cursor in positions:
                cycle = path[positions[cursor] :]
                add_error(
                    "comment_cycle",
                    "评论父链存在循环，相关节点未挂入正常评论树。",
                    {"comment_ids": cycle},
                )
                for path_id in path:
                    resolved[path_id] = None
                invalid = True
                break

            positions[cursor] = len(path)
            path.append(cursor)
            comment = index[cursor]

            if comment.is_root:
                resolved[cursor] = cursor
                break

            if comment.root_id == 0 or comment.parent_id == 0:
                add_error(
                    "invalid_relationship",
                    "非根评论的 root_id 和 parent_id 必须都非 0。",
                    {
                        "comment_id": comment.comment_id,
                        "root_id": comment.root_id,
                        "parent_id": comment.parent_id,
                    },
                )
                for path_id in path:
                    resolved[path_id] = None
                invalid = True
                break

            if comment.parent_id not in index:
                add_error(
                    "missing_parent",
                    "评论的直接父节点不在读取结果中。",
                    {
                        "comment_id": comment.comment_id,
                        "parent_id": comment.parent_id,
                    },
                )
                for path_id in path:
                    resolved[path_id] = None
                invalid = True
                break

            cursor = comment.parent_id

        if invalid:
            continue

        resolved_root = resolved.get(cursor)
        for path_id in reversed(path):
            if path_id in resolved:
                resolved_root = resolved[path_id]
                continue

            comment = index[path_id]
            if resolved_root is None:
                resolved[path_id] = None
                continue

            if comment.root_id != resolved_root:
                add_error(
                    "root_mismatch",
                    "评论声明的 root_id 与 parent_id 父链实际到达的根评论不一致。",
                    {
                        "comment_id": comment.comment_id,
                        "declared_root_id": comment.root_id,
                        "resolved_root_id": resolved_root,
                    },
                )
                resolved[path_id] = None
                resolved_root = None
                continue

            resolved[path_id] = resolved_root

    nodes = {comment_id: CommentNode(comment) for comment_id, comment in index.items()}
    trees: list[CommentNode] = []
    orphans: list[Comment] = []

    for comment_id, comment in index.items():
        if resolved.get(comment_id) is None:
            orphans.append(comment)
        elif comment.is_root:
            trees.append(nodes[comment_id])
        else:
            nodes[comment.parent_id].children.append(nodes[comment_id])

    for node in nodes.values():
        node.children.sort(key=lambda child: _comment_sort_key(child.comment))
    trees.sort(key=lambda node: _comment_sort_key(node.comment))
    orphans.sort(key=_comment_sort_key)

    return TreeBuildResult(
        trees=trees,
        orphans=orphans,
        duplicate_comment_ids=sorted(duplicate_ids),
        diagnostics=diagnostics,
        comment_index=index,
    )
