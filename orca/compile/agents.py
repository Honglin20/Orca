"""agents.py —— agent 池解析层（phase-14）。

回答「``node.agent: "analyzer"`` 怎么变成可执行的 prompt + 资源？」：``AgentResolver``
把 agent 引用名解析成 ``AgentHandle``（物化 prompt + frontmatter 元数据 + 资源目录绝对路径）。

**单一解析路径（铁律 1）**：agent 引用解析只在 compile 层做一次；render/run 层零文件 I/O。
替代旧的 ``compile/_load_prompts`` + ``exec/_load_agent_md`` 双加载债（前者用 yaml 父目录、
后者用 cwd，路径基不一致 → bug 源）。

agent 形态（每个 pool 目录内，文件夹优先）：
  - 文件夹：``<base>/<name>/agent.md``（含 frontmatter + 资源子目录 scripts/refs）
  - 单文件：``<base>/<name>.md``（兼容期，纯 prompt 或带 frontmatter）

查找顺序（first-wins）：
  1. ``<workflow_dir>/agents/``（workflow 同目录，project-local，最高优先）
  2. ``<cwd>/agents/``（cwd 下，跨 workflow 复用）
  3. ``extra_roots``（phase-15 多 pool；phase-14 恒空）

frontmatter（YAML 头 + markdown body，``---`` 分隔；精确算法见 ``_parse_frontmatter``）::

    ---
    description: 神经架构搜索优化器
    model: deepseek-v4-flash
    tools: [Bash, Read]
    ---
    # agent prompt body……

合并优先级（在 ``parser._resolve_agents`` 做，不在本模块）：node 内联 > agent frontmatter > schema 默认。

依赖单向：本模块只依赖 ``pathlib`` + ``yaml`` + ``dataclasses``，**零反向依赖**
（不 import schema/run/exec/iface）。为 phase-15 多 pool / 未来 registry 预留：
``AgentResolver`` 是 ``Protocol``，``node.agent`` 字符串 ``"analyzer"`` 未来扩展为
``"analyzer@source"``，``MultiPoolResolver`` 同接口实现，schema 不变。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from yaml.error import YAMLError

from orca.compile.validator import ConfigurationError

logger = logging.getLogger(__name__)

# agent 池目录名（每个查找 base 下的子目录约定）。
_POOL_DIR = "agents"
# 文件夹形态 agent 的入口文件名（非 index.md —— 语义明确，不是 web 首页索引）。
_FOLDER_ENTRY = "agent.md"


# ── 数据类 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentMeta:
    """agent frontmatter 元数据（全可选，与 ``AgentNode`` 字段对齐做默认值源）。

    合并时作为「agent 级默认」：``AgentNode`` 显式字段压 ``AgentMeta``，``AgentMeta`` 压
    schema 默认。未知字段在 ``_parse_frontmatter`` 构造时 fail loud（``TypeError``）。
    """

    description: str = ""
    model: str | None = None
    tools: list[str] | None = None
    executor: str | None = None
    # 预留扩展点（phase-15+）：workspace_instructions / capabilities / version


@dataclass(frozen=True)
class AgentHandle:
    """resolver 解析产物（compile 期由 ``_resolve_agents`` 物化进 ``AgentNode``）。

    - ``prompt``：frontmatter 之后的 body（**未** Jinja2 渲染，留待 exec/render 期渲染）。
    - ``meta``：frontmatter 解析出的元数据（无 frontmatter 则全默认）。
    - ``resources_root``：资源目录绝对路径。文件夹 agent = 入口文件父目录（含 scripts/refs
      子目录）；单文件 agent = md 所在目录。
    - ``is_folder``：agent 形态（C4：MCP ``has_resources`` 据此，不据 resources_root 非空）。
    - ``source``：解析来源描述（如 ``"local:examples/agents/analyzer/agent.md"``），错误归因用。
    """

    prompt: str
    meta: AgentMeta
    resources_root: Path
    is_folder: bool
    source: str


@dataclass(frozen=True)
class ResolveContext:
    """resolver 解析上下文（查找基准 + 未来 pool 配置）。

    ``extra_roots`` 为 phase-15 多 pool 预留（``~/.orca/pools.toml`` / env）；phase-14 恒空。
    frozen tuple（不可变，防 resolve 期被 mutate）。
    """

    workflow_dir: Path
    cwd: Path
    extra_roots: tuple[Path, ...] = ()


# ── 异常 ──────────────────────────────────────────────────────────────────────


class AgentNotFound(Exception):
    """agent 引用名在所有查找 base 都未命中。

    ``_resolve_agents`` 捕获后聚合（一次列全所有缺失名 + 搜过路径）→ ``ConfigurationError``。
    """

    def __init__(self, name: str, searched: list[str]):
        self.name = name
        self.searched = searched
        super().__init__(
            f"agent '{name}' 未找到；搜过路径：{', '.join(searched) or '(无)'}"
        )


# ── resolver 接口（Protocol，为 phase-15 多 pool / registry 预留）─────────────


class AgentResolver(Protocol):
    """agent 引用 → ``AgentHandle`` 解析器接口。

    实现者：
      - phase-14：``LocalPoolResolver``（本地多目录查找）。
      - phase-15：``MultiPoolResolver``（``name@source`` 拆分 + pools.toml）。
      - 未来：``RegistryResolver``（``name@registry#ref`` + 拉取/缓存/SHA 锁定）。

    所有实现遵守同一契约：``resolve(name, context) → AgentHandle``，缺失 → ``AgentNotFound``。
    **接口锁定**：未来不加方法（扩展靠新实现类，不改本 Protocol）。
    """

    def resolve(self, name: str, *, context: ResolveContext) -> AgentHandle: ...


# ── 默认实现：本地 pool resolver ──────────────────────────────────────────────


class LocalPoolResolver:
    """本地多目录 agent 池查找（phase-14 默认实现）。

    查找顺序（first-wins，``_search_bases``）：
      1. ``context.workflow_dir / agents``（workflow 同目录）
      2. ``context.cwd / agents``（cwd 下）
      3. ``context.extra_roots``（phase-15 配置的额外 pool root）

    每个 base 内：文件夹 ``<name>/agent.md`` 优先；单文件 ``<name>.md`` 兼容兜底。
    """

    def resolve(self, name: str, *, context: ResolveContext) -> AgentHandle:
        searched: list[str] = []
        for base in self._search_bases(context):
            folder_entry = base / name / _FOLDER_ENTRY
            single = base / f"{name}.md"
            if folder_entry.is_file():
                return self._read(folder_entry, is_folder=True)
            searched.append(str(folder_entry))
            if single.is_file():
                return self._read(single, is_folder=False)
            searched.append(str(single))
        raise AgentNotFound(name, searched)

    def discover(self, *, context: ResolveContext) -> list[tuple[str, bool]]:
        """列出所有可用 agent → ``[(name, is_folder), ...]``（按 name 排序）。

        给 MCP ``list_agents`` / CLI ``list --agents`` 用。去重：同一 name 在多个 base 出现时，
        **先见的优先**（与 ``resolve`` 的 first-wins 一致——workflow_dir 先于 cwd）。
        缺失目录静默跳过（discover 不对缺失目录 fail loud）。
        """
        seen: dict[str, bool] = {}
        for base in self._search_bases(context):
            if not base.is_dir():
                continue
            # 文件夹 agent（先扫，优先级高）
            try:
                children = sorted(base.iterdir())
            except OSError:
                continue
            for sub in children:
                if sub.is_dir() and (sub / _FOLDER_ENTRY).is_file():
                    seen.setdefault(sub.name, True)
            # 单文件 agent
            for f in sorted(base.glob("*.md")):
                seen.setdefault(f.stem, False)
        return sorted(seen.items())

    def _search_bases(self, context: ResolveContext) -> list[Path]:
        """查找 base 列表（first-wins 顺序）。"""
        return [
            context.workflow_dir / _POOL_DIR,
            context.cwd / _POOL_DIR,
            *context.extra_roots,
        ]

    def _read(self, entry_path: Path, *, is_folder: bool) -> AgentHandle:
        """读 agent 入口文件 → AgentHandle。文件读取/frontmatter 解析失败 fail loud。"""
        try:
            text = entry_path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigurationError(
                [f"读取 agent 文件 {entry_path} 失败：{e}"], []
            ) from e
        body, meta = _parse_frontmatter(text, source=str(entry_path))
        return AgentHandle(
            prompt=body,
            meta=meta,
            resources_root=entry_path.parent,
            is_folder=is_folder,
            source=f"local:{entry_path}",
        )


# ── frontmatter 解析（C6 精确算法）────────────────────────────────────────────


def _parse_frontmatter(text: str, *, source: str) -> tuple[str, AgentMeta]:
    """分离 frontmatter（YAML 头）与 prompt body。

    精确算法（C6：body 内的 ``---`` 水平线不再误判）：
      - 仅当**首行** ``strip() == "---"`` 时进入 frontmatter 解析。
      - 随后找**第一个**再独占整行（``strip() == "---"``）的行作为闭合。
      - 首尾 ``---`` 之间为 YAML 头（``yaml.safe_load`` → ``AgentMeta``）；之后为 body。
      - 首行 ``---`` 但无闭合 → fail loud（frontmatter 未闭合）。

    无 frontmatter（首行非 ``---``）→ 整文件当 body，``meta = AgentMeta()`` 全默认
    （向后兼容现有无头 md）。

    fail loud（ConfigurationError）：
      - frontmatter YAML 语法错 / 非 mapping / 未知字段 / 字段类型不兼容。
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        close_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                close_idx = i
                break
        if close_idx is None:
            raise ConfigurationError(
                [f"{source}: frontmatter 首行 '---' 无闭合 '---'（缺结束分隔符）"], []
            )
        fm_yaml = "\n".join(lines[1:close_idx])
        body = "\n".join(lines[close_idx + 1 :])
        meta = _parse_meta_yaml(fm_yaml, source=source)
    else:
        fm_yaml = None
        body = text
        meta = AgentMeta()
    return body, meta


def _parse_meta_yaml(fm_yaml: str, *, source: str) -> AgentMeta:
    """frontmatter YAML → AgentMeta。fail loud：语法错 / 非 mapping / 未知字段。"""
    if not fm_yaml.strip():
        return AgentMeta()  # 空 frontmatter（---\n---）= 全默认
    try:
        data = yaml.safe_load(fm_yaml)
    except YAMLError as e:
        raise ConfigurationError(
            [f"{source}: frontmatter YAML 解析失败：{e}"], []
        ) from e
    if data is None:
        return AgentMeta()  # 空内容
    if not isinstance(data, dict):
        raise ConfigurationError(
            [
                f"{source}: frontmatter 必须是 YAML mapping（得到 {type(data).__name__}）"
            ],
            [],
        )
    # AgentMeta 是 dataclass：未知字段 → TypeError（fail loud，防拼写错误静默忽略）。
    # 字段类型校验：显式检查已知字段的类型，给清晰错误（dataclass 本身不校验类型）。
    _validate_meta_field_types(data, source=source)
    try:
        return AgentMeta(**data)
    except TypeError as e:
        raise ConfigurationError(
            [f"{source}: frontmatter 字段错误：{e}"], []
        ) from e


# AgentMeta 已知字段 → 期望类型（用于 frontmatter 类型校验，给清晰错误）。
_META_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "description": str,
    "model": str,
    "executor": str,
    "tools": list,
}


def _validate_meta_field_types(data: dict[str, Any], *, source: str) -> None:
    """frontmatter 已知字段的类型校验（dataclass 不校验，这里补 fail loud）。"""
    for key, value in data.items():
        expected = _META_FIELD_TYPES.get(key)
        if expected is None:
            continue  # 未知字段交给 AgentMeta(**data) 的 TypeError 处理
        if value is None:
            continue  # None 允许（可缺省）
        if not isinstance(value, expected):
            raise ConfigurationError(
                [
                    f"{source}: frontmatter 字段 '{key}' 类型错误"
                    f"（期望 {expected.__name__ if isinstance(expected, type) else expected}，"
                    f"得到 {type(value).__name__}）"
                ],
                [],
            )
