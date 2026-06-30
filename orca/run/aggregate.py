"""aggregate.py —— failure_mode 三态决策（parallel / foreach 共享，DRY）。

回答「并行分支 / foreach item 失败时怎么办？」：按 ``failure_mode`` 决定是否抛、
抛什么。parallel 组和 foreach 共用此决策逻辑（结果容器形状不同，但失败判定相同）。

failure_mode 三态（SPEC §4.4 / §4.5）：
  - ``fail_fast``：首个失败立即抛（不等其余；但 asyncio.gather 已等所有完成 —— 此处
    仅决定 gather 全部返回后是否抛 / 抛哪个）。
  - ``continue_on_error``：仅全部失败才抛；部分成功则聚合（errors 记录失败项）。
  - ``all_or_nothing``：任一失败即抛（全或无）。

设计：
  - ``decide_failure(failures, success_count, total, failure_mode, group_name)``
    返回 ``None``（不抛）或 ``FailureDecision``（带要抛的 Exception + aggregated payload）。
  - 结果容器（outputs/errors 形状）由 parallel / foreach 各自构造，本模块只决策。
  - 失败项的 value 是 ``Exception`` 实例（asyncio.gather(return_exceptions=True) 的产物）。

依赖单向：本模块不依赖 orca 子模块（纯逻辑）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FailureMode = Literal["fail_fast", "continue_on_error", "all_or_nothing"]


@dataclass
class FailureDecision:
    """``decide_failure`` 决定抛错时的产物。

    ``aggregated`` 是给 ``ctx.outputs[group]`` 的聚合 dict（含 outputs / errors / count），
    即便决定抛错也产出（事件流可见失败项的聚合结果）。
    """

    exception: Exception  # 要抛的异常（已含诊断信息）
    aggregated: dict[str, Any]  # 聚合输出（{outputs, errors, count, ...}）


def decide_failure(
    failures: list[tuple[Any, Exception]],
    success_count: int,
    total: int,
    failure_mode: FailureMode,
    *,
    group_name: str,
    aggregated: dict[str, Any],
) -> FailureDecision | None:
    """按 failure_mode 决定是否抛错（parallel / foreach 共用）。

    Args:
        failures: 失败项列表 ``[(key, exc), ...]``（key 为 branch 名 / foreach index）。
        success_count: 成功项数。
        total: 总项数。
        failure_mode: 三态。
        group_name: parallel 组名 / foreach node 名（诊断信息）。
        aggregated: 已构造的聚合 dict（决定抛错时附带返回）。

    Returns:
        ``None`` = 不抛（部分或全部成功）；``FailureDecision`` = 抛（携带聚合结果）。

    语义说明（SPEC §4.4 / §4.5）：
      - ``fail_fast``：首个失败即抛。**注意**：由于上层用 ``asyncio.gather(return_exceptions=True)``
        等全部完成（让其余 branch/item 跑完再统一决策），本模式在并行场景退化为
        「全部完成后抛首个失败」—— 即与 ``all_or_nothing`` 行为等价。真正的「不等其余」
        需用 ``asyncio.wait(FIRST_EXCEPTION)`` + 取消其余，超出本阶段范围（后续如需再改）。
      - ``all_or_nothing``：任一失败即抛（全或无）。
      - ``continue_on_error``：仅全部失败才抛；部分成功则聚合 errors 不抛。
    """
    if not failures:
        return None  # 全成功

    first_key, first_exc = failures[0]

    # fail_fast 与 all_or_nothing 在 gather(return_exceptions=True) 语义下等价（任一失败即抛）
    if failure_mode in ("fail_fast", "all_or_nothing"):
        return FailureDecision(
            exception=_wrap(first_exc, group_name, first_key),
            aggregated=aggregated,
        )

    # continue_on_error：仅全失败才抛
    if success_count == 0:
        return FailureDecision(
            exception=_wrap(first_exc, group_name, first_key),
            aggregated=aggregated,
        )
    return None  # 部分成功 → 不抛，聚合 errors


def _wrap(exc: Exception, group_name: str, key: Any) -> Exception:
    """把底层异常包一层诊断上下文（不吞原始 exception 链，保留 ``__cause__``）。"""
    if isinstance(exc, GroupFailure):
        return exc  # 已包装过，避免双层
    wrapped = GroupFailure(
        f"组 {group_name!r} 的分支/项 {key!r} 失败：{exc}",
        group_name=group_name,
        key=key,
    )
    wrapped.__cause__ = exc
    return wrapped


class GroupFailure(Exception):
    """parallel / foreach 内部分支失败的统一外壳（携带 group 名 + key 便于诊断）。

    orchestrator 捕获后映射到 ``workflow_failed``（error_type=``GroupFailure``）。
    底层异常通过 ``__cause__`` 保留（不吞）。
    """

    def __init__(self, message: str, *, group_name: str, key: Any):
        self.group_name = group_name
        self.key = key
        super().__init__(message)
