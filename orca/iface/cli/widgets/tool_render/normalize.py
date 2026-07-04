"""tool_render/normalize.py —— (executor, tool, args, result) → RenderItem 纯函数。

回答「同一个工具在不同 backend 怎么归一成统一的渲染契约？」：本模块是 render layer
的**第一段纯函数**（render-layer-design-draft §3.1 / §6）::

    normalize_tool(executor, tool_name, args, result, status) -> RenderItem

契约铁律（§6.4 派生投影的纯函数性证明）：
  - 无 I/O（不读文件、不读环境）
  - 无副作用（不写 tape、不缓存 mutable 状态）
  - 给定相同输入必产出相同 RenderItem（含 diff 算法确定性）

fail loud（§6.2 / §13）：
  - args 非 dict → ``NormalizeError``（translator 层应保证 args 已解析为 dict）
  - opencode read 目录 XML 解析失败 → 降级 ``is_dir=False`` + warning log（不 raise，
    避免单工具渲染失败阻塞整个 TUI；fail visible 而非 fail loud）

依赖单向（§7.2）：只依赖 ``orca.schema`` + stdlib，**禁止** import ``orca.exec`` /
``orca.run`` / ``orca.events.bus``。
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Any, Callable

from orca.schema import RenderItem, RenderToolKind, ToolStatus

logger = logging.getLogger(__name__)


class NormalizeError(Exception):
    """normalizer 收到不符合契约的输入（如 args 非 dict）。

    translator 层应保证 args 已解析为 dict；本错误说明上游契约破裂，
    应在 translator / executor 修，不在 normalizer 兜底（§6.2 / §13）。
    """


# ── (executor, tool_name) → kind 主表（§6.1）─────────────────────────────────
#
# v1 覆盖 claude + opencode（codex 延后到 v1.5）。新 backend 接入加行即可（§4.3）。
#
# 格式：{executor: {tool_name: kind}}
_TOOL_KIND_MAP: dict[str, dict[str, RenderToolKind]] = {
    "claude": {
        "Read": "file_read",
        "Write": "file_write",
        "Edit": "file_edit",
        "Bash": "shell",
        "Glob": "glob",
        "Grep": "grep",
    },
    "opencode": {
        "read": "file_read",
        "write": "file_write",
        "edit": "file_edit",
        "bash": "shell",
        "glob": "glob",
        "grep": "grep",
    },
}


def _resolve_kind(executor: str, tool_name: str) -> RenderToolKind:
    """查 §6.1 表，未命中 → ``unknown`` 兜底（§12.6）。

    executor 全部小写化匹配（容错：profile 名 ``Claude`` / ``claude`` 都能命中）。
    """
    by_tool = _TOOL_KIND_MAP.get(executor.lower())
    if by_tool is None:
        return "unknown"
    return by_tool.get(tool_name, "unknown")


# ── per-kind payload normalizer 注册表（§6.3）────────────────────────────────
#
# 每个 kind 一个 ``(args, result) -> payload_dict`` 纯函数。
# payload 字段契约见 spec §5.2。
_PayloadNormalizer = Callable[[dict[str, Any], str | None], dict[str, Any]]


def normalize_tool(
    executor: str,
    tool_name: str,
    args: Any,
    result: str | None,
    status: ToolStatus,
) -> RenderItem:
    """ ``(executor, tool_name, args, result, status) → RenderItem`` 纯函数（§6.2）。

    参数：
      executor：backend 标识（"claude" / "opencode" / "codex" / ...），用于查 §6.1 表
      tool_name：原始 backend 工具名（如 claude 的 ``Read`` / opencode 的 ``read``）
      args：Event.data.args（必须 dict；非 dict → NormalizeError）
      result：Event.data.result（``None`` 表示 tool_call 阶段；非 None 时 translator 保证 str）
      status：``running``（call 阶段）/ ``completed``（result 阶段）/ error / interrupted

    返回：填充好 payload + title + subtitle + raw 的 RenderItem（v1 不附加视觉信息）
    """
    if not isinstance(args, dict):
        raise NormalizeError(
            f"args must be dict, got {type(args).__name__}: {args!r}"
        )

    kind = _resolve_kind(executor, tool_name)
    payload = _PAYLOAD_NORMALIZERS[kind](args, result)
    title = _make_title(kind, tool_name, payload)
    subtitle = _make_subtitle(kind, payload)
    return RenderItem(
        kind=kind,
        status=status,
        title=title,
        subtitle=subtitle,
        payload=payload,
        raw={"args": args, "result": result, "tool_name": tool_name},
    )


# ── per-kind payload 实现 ─────────────────────────────────────────────────────


def _normalize_file_read(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """file_read payload（§5.2 / §6.3）。

    - opencode read 目录：result 含 ``<type>directory</type>`` XML → 解析 entries
    - 其他情况：按文件读取，result 文本行号化为 content
    """
    path = str(args.get("file_path") or args.get("filePath") or args.get("path") or "")
    text = result or ""

    if "<type>directory</type>" in text:
        entries = _parse_opencode_dir_entries(text)
        if entries is not None:
            return {"path": path, "is_dir": True, "entries": entries, "truncated": False}
        # XML 解析失败 → 降级 + warning（§13 fail visible：不 raise）
        logger.warning(
            "opencode read 目录 XML 解析失败，降级 is_dir=False 原样文本展示（path=%s）",
            path,
        )

    content = _line_numbered(text)
    return {"path": path, "is_dir": False, "content": content, "truncated": False}


def _normalize_file_write(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """file_write payload（§5.2）。

    claude ``Write`` / opencode ``write``：``args.content`` 为新文件全文。
    """
    path = str(args.get("file_path") or args.get("filePath") or args.get("path") or "")
    raw_content = str(args.get("content") or "")
    content = _line_numbered(raw_content)
    return {"path": path, "content": content, "bytes": len(raw_content.encode("utf-8"))}


def _normalize_file_edit(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """file_edit payload（§5.2 / §6.3）。

    claude/opencode ``Edit/edit``：``args.{old_string,new_string}`` → 自算 diff → hunks。
    codex ``apply_patch`` 待 v1.5（v1 不接）。
    """
    path = str(args.get("file_path") or args.get("filePath") or args.get("path") or "")
    old = str(args.get("old_string", args.get("oldString", "")))
    new = str(args.get("new_string", args.get("newString", "")))
    hunks, added, deleted = _build_diff_hunks(old, new)
    return {"path": path, "hunks": hunks, "added": added, "deleted": deleted}


def _normalize_shell(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """shell payload（§5.2）。

    三家几乎同形：``{command}`` + result（output）。v1 不解析 exit_code（结果文本兜底）。
    """
    command = str(args.get("command") or "")
    output = result or ""
    return {"command": command, "output": output}


def _normalize_glob(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """glob payload（§5.2）。

    ``args.pattern`` + ``args.path``（可选）；matches 从 result 文本按行解析。
    """
    pattern = str(args.get("pattern") or "")
    matches = _parse_path_list(result)
    return {"pattern": pattern, "matches": matches}


def _normalize_grep(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """grep payload（§5.2）。

    ``args.pattern``；matches 按 ``file:n:text`` 简单解析（v1 容错：解析失败按行兜底）。
    """
    pattern = str(args.get("pattern") or "")
    matches = _parse_grep_result(result or "")
    return {"pattern": pattern, "matches": matches}


def _normalize_unknown(args: dict[str, Any], result: str | None) -> dict[str, Any]:
    """unknown payload（§5.2 / §12.9）。

    args JSON 美化（``json.dumps(indent=2)``）+ result 截断预览（兜底走老渲染）。
    """
    try:
        args_preview = json.dumps(args, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        args_preview = str(args)
    result_preview = (result or "")[:500]
    return {"tool_name": "", "args_preview": args_preview, "result_preview": result_preview}


_PAYLOAD_NORMALIZERS: dict[RenderToolKind, _PayloadNormalizer] = {
    "file_read": _normalize_file_read,
    "file_write": _normalize_file_write,
    "file_edit": _normalize_file_edit,
    "shell": _normalize_shell,
    "glob": _normalize_glob,
    "grep": _normalize_grep,
    "unknown": _normalize_unknown,
}


# ── title / subtitle 派生 ─────────────────────────────────────────────────────


def _make_title(kind: RenderToolKind, tool_name: str, payload: dict[str, Any]) -> str:
    """per-kind title（§8.1 视觉意图表的「头部 title」）。

    title 是一行摘要，用于 Panel header。subtitle 单独派生（如 ``+12 -3``）。
    """
    if kind in ("file_read", "file_write", "file_edit"):
        return str(payload.get("path") or tool_name)
    if kind == "shell":
        return f"$ {payload.get('command', '')}".rstrip()
    if kind == "glob":
        return f"pattern: {payload.get('pattern', '')}".rstrip()
    if kind == "grep":
        return f"pattern: {payload.get('pattern', '')}".rstrip()
    # unknown：title 用原始工具名（payload.tool_name 为空字符串，由 caller 透传 tool_name）
    return tool_name


def _make_subtitle(kind: RenderToolKind, payload: dict[str, Any]) -> str:
    """per-kind subtitle（§8.1 副标题，可选）。

    - file_edit：``+12 -3``
    - glob：``N matches``
    - file_read 目录：``N entries``
    - 其他：空串
    """
    if kind == "file_edit":
        return f"+{payload.get('added', 0)} -{payload.get('deleted', 0)}"
    if kind == "glob":
        n = len(payload.get("matches", []))
        return f"{n} match{'es' if n != 1 else ''}"
    if kind == "file_read" and payload.get("is_dir"):
        n = len(payload.get("entries", []))
        return f"{n} entries"
    return ""


# ── helpers ───────────────────────────────────────────────────────────────────


def _line_numbered(text: str) -> list[dict[str, Any]]:
    """文本 → ``[{n, text}, ...]`` 行号化（§5.2 content / file_write content）。

    保留空行（行号连续）；末尾换行不额外产生空行。
    """
    if not text:
        return []
    # splitlines 不区分 ``\n`` / ``\r\n``，但会丢末尾空行——对渲染 OK（视觉无影响）。
    lines = text.splitlines()
    return [{"n": i + 1, "text": line} for i, line in enumerate(lines)]


def _parse_opencode_dir_entries(text: str) -> list[str] | None:
    """opencode read 目录 XML → entries 列表（§6.3 实测 shape）。

    样本（``runs/demo_task-20260703-221337-c94151.jsonl``）::

        <path>/abs/path</path>
        <type>directory</type>
        <entries>
        .codegraph/
        .git/
        ...
        (17 entries)
        </entries>

    返回 ``None`` 表示解析失败（caller 决定降级策略）。
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # 退化：正则提取 ``<entries>...</entries>`` 内容（容错：opencode 加包装层时）
        m = re.search(r"<entries>(.*?)</entries>", text, re.DOTALL)
        if m is None:
            return None
        body = m.group(1)
    else:
        # ElementTree 把 ``<entries>`` 内的纯文本当作 root.text / tail；
        # opencode 的 entries 是 ``\n`` 分隔的纯文本，不是子标签。
        entries_elem = root.find("entries")
        if entries_elem is not None:
            body = entries_elem.text or ""
        else:
            body = root.text or ""

    # 解析行：去空白 + 去 ``(... entries)`` 尾注（opencode 自动生成）。
    raw_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    entries: list[str] = []
    for ln in raw_lines:
        if re.fullmatch(r"\(\d+ entries\)", ln):
            continue
        entries.append(ln)
    return entries


def _parse_path_list(result: str | None) -> list[str]:
    """glob/shell 路径列表解析：每行一个路径（容错：空 result 返回空列表）。

    v1 简单按行切；result 形态由 translator 保证为 str（已是 string）。
    """
    if not result:
        return []
    return [ln.strip() for ln in result.splitlines() if ln.strip()]


_GREP_LINE_RE = re.compile(r"^(?P<path>.+?):(?P<n>\d+):(?P<text>.*)$")


def _parse_grep_result(result: str) -> list[dict[str, Any]]:
    """grep result 按 ``file:n:text`` 解析，按文件分组（§5.2 grep payload）。

    v1 容错：解析失败的行作为单 ``text`` 行兜底（path="?"）。hit_start/hit_end 不解析
    （v2 加：需要知道 pattern 是否为正则，复杂度真高，延后）。
    """
    if not result:
        return []
    by_path: dict[str, list[dict[str, Any]]] = {}
    for ln in result.splitlines():
        m = _GREP_LINE_RE.match(ln)
        if m:
            path = m.group("path")
            n = int(m.group("n"))
            text = m.group("text")
        else:
            path = "?"
            n = 0
            text = ln
        by_path.setdefault(path, []).append({"n": n, "text": text})

    return [{"path": p, "lines": lines} for p, lines in by_path.items()]


def _build_diff_hunks(old: str, new: str) -> tuple[list[dict[str, Any]], int, int]:
    """``old_string`` + ``new_string`` → unified diff 风格的 hunks（§5.2 / §6.3）。

    返回 ``(hunks, added, deleted)``。hunks 形如::

        [{"start": 1, "lines": [
            {"type": "ctx", "text": "foo"},
            {"type": "del", "text": "old"},
            {"type": "add", "text": "new"},
        ]}]

    算法：``difflib.SequenceMatcher`` 按行对齐（确定性，两端实现一致是 snapshot 测试责任，
    spec §13 风险表已认视觉微差）。start 用 1-based 行号（与新 content 行号化一致）。
    """
    old_lines = old.splitlines() or [""]
    new_lines = new.splitlines() or [""]
    sm = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    hunks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    added = 0
    deleted = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # ctx 段：合并到当前 hunk（若有），否则按需开新 hunk（v1：ctx 也展示，
            # 帮用户看上下文；千行 diff 虚拟化延后 v2）。
            for k, line in enumerate(old_lines[i1:i2]):
                if cur is None:
                    cur = {"start": i1 + k + 1, "lines": []}
                cur["lines"].append({"type": "ctx", "text": line})
        elif tag == "replace":
            if cur is None:
                cur = {"start": i1 + 1, "lines": []}
            for line in old_lines[i1:i2]:
                cur["lines"].append({"type": "del", "text": line})
                deleted += 1
            for line in new_lines[j1:j2]:
                cur["lines"].append({"type": "add", "text": line})
                added += 1
        elif tag == "delete":
            if cur is None:
                cur = {"start": i1 + 1, "lines": []}
            for line in old_lines[i1:i2]:
                cur["lines"].append({"type": "del", "text": line})
                deleted += 1
        elif tag == "insert":
            if cur is None:
                cur = {"start": i1 + 1, "lines": []}
            for line in new_lines[j1:j2]:
                cur["lines"].append({"type": "add", "text": line})
                added += 1

        # hunk 收尾：当 ctx 段过长（>3 行）时，关闭当前 hunk（下一个 opcode 开新 hunk）。
        # 简单策略：每条 opcode 一个 hunk（v1 够用，v2 评估连续 ctx 合并）。
        if cur is not None and tag != "equal":
            # 段间分界：replace/delete/insert 后下一个 equal 视为新 hunk。
            pass
        if cur is not None and tag == "equal" and len(cur["lines"]) > 5:
            hunks.append(cur)
            cur = None

    if cur is not None:
        hunks.append(cur)

    return hunks, added, deleted


# ── 共享单行摘要（DRY 消除：log_stream + node_detail 调同一函数）─────────────
#
# 这两个 widget 都把工具事件拍成单行（``tool: Bash(...)`` / ``→ <result>``），
# 迁移前分别在 ``log_stream._describe`` 和 ``node_detail._format_stream_line``
# 重复实现；spec §7.3 第 1 步「先消 DRY 不改行为」把它们抽到 tool_render 同一函数。


def describe_tool_event(etype: str, data: dict[str, Any], *, detail: str) -> str:
    """工具事件的单行摘要（DRY 共享，log_stream + node_detail 调同一函数）。

    参数：
      etype：``agent_tool_call`` / ``agent_tool_result``
      data：Event.data
      detail：``"log"`` (log_stream 风格，更紧凑) / ``"stream"`` (node_detail 风格，
        带 kind_tag 前缀由 caller 加)

    返回：摘要字符串（不含 timestamp / kind_tag，由 caller 拼接）

    不调用 normalize_tool（摘要路径不构造完整 RenderItem，避免给日志路径加额外开销）。
    """
    if etype == "agent_tool_call":
        tool = str(data.get("tool", "?"))
        args = data.get("args", {})
        return f"tool: {tool}({_truncate_for_summary(args)})"
    if etype == "agent_tool_result":
        result = data.get("result", "")
        return f"→ {_truncate_for_summary(result)}"
    return ""


# 单行摘要的截断长度（与历史 log_stream._truncate 默认 60 / node_detail._truncate 80 对齐）。
_SUMMARY_LIMIT = 80


def _truncate_for_summary(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    """单行摘要截断（带 …）。

    兼容历史行为：log_stream 用 60 字符，node_detail 用 80；此处统一 80（更宽松），
    既有 snapshot 测试若依赖 60 限长，由 detail="log" 路径用更短 limit 兜底（v1 暂不分）。
    """
    s = str(value) if value is not None else ""
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


__all__ = [
    "NormalizeError",
    "normalize_tool",
    "describe_tool_event",
    "_resolve_kind",
    "_TOOL_KIND_MAP",
]
