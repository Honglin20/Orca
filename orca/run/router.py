"""router.py —— routes first-match-wins 求值（纯函数，SPEC §3）。

回答「节点完成后下一步去哪？」：``resolve(routes, output, ctx) -> target``。
纯函数、无副作用、无 I/O —— 同输入永远同输出（铁律 5）。

求值规则（SPEC §3.1）：
  - 顺序遍历 ``routes``，第一个 ``when`` 为真（或 ``when=None`` 兜底）的 route 命中。
  - ``when`` 是 Jinja2 表达式，求值后 truthy 判定（非空串 / 非零 / 非空集合 / True）。
  - 全部 ``when`` 不匹配且无兜底 route → ``RouteError``（fail loud，铁律 4 → workflow_failed）。

变量命名（SPEC §3.2，与 render.py ``_namespace`` 同一摊开规则）：
  - ``output``：本节点刚完成的输出（裸引用，如 ``output.exit_code``）
  - ``inputs``：workflow 输入（``inputs.iterations``）
  - ``<node_name>``：任意已完成 node 的输出（``optimizer.output.structure``，
    因 ``ctx.outputs[node] = {"output": raw}`` 包装）
  - ``<parallel_group>.outputs``：parallel 组聚合（``ctx.outputs[group]`` 存聚合 dict）

依赖单向：本模块依赖 ``orca.schema``（Route）+ ``orca.exec.context``（RunContext 类型）；
不依赖 events.bus / tape / exec 子模块。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import Environment, StrictUndefined, TemplateError

from orca.schema import Route

if TYPE_CHECKING:
    from orca.exec.context import RunContext


class RouteError(Exception):
    """路由死锁：所有 ``when`` 不匹配且无兜底 route（SPEC §3.4 / 铁律 4）。

    触发场景：节点完成后没有任何 route 可走（编译期校验只保证 route.to 合法，
    不保证运行时一定命中）。本异常上抛 → orchestrator 捕获 → emit ``workflow_failed``
    （error_type=``NoRouteMatch``）。
    """

    def __init__(self, message: str, *, node: str | None = None, output: Any = None):
        self.node = node  # 卡在哪个 node（用于 workflow_failed payload）
        self.output = output  # 导致死锁的 output（诊断用）
        super().__init__(message)


# 复用 render.py 的 Jinja2 Environment 约定：StrictUndefined 让未定义变量 fail loud。
# 独立单例（避免与 render 模块共享可变状态；配置完全一致）。
_ENV = Environment(
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # when 表达式非 HTML
)


def resolve(routes: list[Route], output: Any, ctx: RunContext) -> str:
    """first-match-wins 求值路由（SPEC §3.1）。

    Args:
        routes: 该 node / parallel 组的出边列表（顺序敏感）。
        output: 本节点刚完成的 raw output（executor 返回，未 ``{"output": raw}`` 包装）。
          phase 11 §9.2：``output is None`` 表示该 node 被 SKIP（``outputs_acc[node] =
          {"output": None, "skipped": True}``）。下游 ``when`` 引用 ``output.field``
          会 UndefinedError → 见下方「skip 容错」。
        ctx: 当前 RunContext（``ctx.outputs`` 已含历史 node 的 ``{"output": raw}`` 包装）。

    Returns:
        target（node 名 / parallel 组名 / ``"$end"``）。

    Raises:
        RouteError: 全部 ``when`` 不匹配且无兜底 route（fail loud）。

    纯函数：不触碰 ``ctx`` / ``output``，无 I/O，同输入两次调用结果一致（铁律 5）。

    **skip 容错（SPEC §9.2）**：当 ``output is None``（被 SKIP 的 node）时，``when`` 表达式
    引用 ``output.field`` 会 raise（UndefinedError / None AttributeError）。本函数把这类
    「因 skipped node 的 None output 导致的 when 求值失败」视为 **该 route 不匹配**（继续
    尝试下一条），最终落到 ``when=None`` 兜底 route。**仅当 ``output is None`` 时启用此容错**
    ——非 skip 路径的 when 求值失败仍 fail loud（RouteError），避免静默吞真错。
    """
    eval_ctx = _build_route_eval_context(output, ctx)
    # SPEC §9.2：skipped node（output is None）的 when 求值失败 = 该 route 不匹配（非 fail loud）。
    # 让 resolve 继续往下找兜底 route（when=None），避免 NoRouteMatch 崩溃。
    #
    # **隐式契约**：``output is None`` 当前等价于「该 node 被 SKIP」——由 orchestrator
    # ``_drive_loop`` skip 分支设 ``outputs_acc[node] = {"output": None, "skipped": True}``，
    # 且非 skip 路径的 executor 返回值经 ``{"output": raw}`` 包装（即便 raw 是 None 也是
    # dict 而非裸 None），故 router 拿到的 output 只在 skip 时为裸 None。未来若有 executor
    # 直接返回裸 None（未包装），此容错会误触发——届时需改用更显式的 ``skipped: bool`` 信号。
    skip_tolerant = output is None
    for route in routes:
        if route.when is None:
            return route.to  # 兜底（catch-all），SPEC §3.1
        try:
            matched = _eval_jinja2_bool(route.when, eval_ctx)
        except RouteError:
            if not skip_tolerant:
                raise  # 非 skip 路径：when 求值失败 fail loud（既定契约不变）
            # skip 路径：when 引用 None output 的字段失败 → 视为不匹配，继续找兜底 route
            continue
        if matched:
            return route.to
    raise RouteError(
        f"无 route 匹配（output={output!r}，已评估 {len(routes)} 条 when 均不命中且无兜底）",
        node=None,  # 由 orchestrator 调用处补充 node 名
        output=output,
    )


def _build_route_eval_context(output: Any, ctx: RunContext) -> dict[str, Any]:
    """构造 when 表达式的 Jinja2 顶层命名空间（SPEC §3.2）。

    - ``output``：本节点 raw output（裸引用 ``output.exit_code``）
    - ``inputs``：workflow 输入
    - 其余顶层 key：``ctx.outputs`` 展开（已完成 node 的 ``{"output": raw}``），
      支持 ``{{ optimizer.output.structure }}`` 点路径（与 render 同形）
    """
    ns: dict[str, Any] = {"output": output, "inputs": dict(ctx.inputs)}
    ns.update(ctx.outputs)
    return ns


def _eval_jinja2_bool(expr: str, ctx_dict: dict[str, Any]) -> bool:
    """渲染 when 表达式 → truthy 判定（SPEC §3.1）。

    Jinja2 渲染后取 truthy：非空串 / 非零数字 / 非空集合 / ``True``。
    渲染失败（未定义变量 / 语法错 / 类型不兼容的比较）raise ``RouteError``
    （fail loud，铁律 4 —— 不让 Jinja2 的 TypeError / UndefinedError 漏到 orchestrator）。

    注意：Jinja2 的 ``{{ ... }}`` 输出是字符串，故布尔 / 数字表达式会被 stringify。
    本函数对 stringify 结果做 truthy 判定（空串 / "0" / "False" / "none" → False），
    以贴近 SPEC「求值为 bool」的意图。

    建议：涉及类型比较的 when（如 ``output.n >= 3``）显式 ``| int`` / ``| float``
    强转，避免 set 节点产出 str 数字时比较报错。
    """
    try:
        tpl = _ENV.from_string("{{ " + expr + " }}")
        rendered = tpl.render(**ctx_dict)
    except TemplateError as e:
        raise RouteError(
            f"路由 when 表达式 {expr!r} 求值失败：{e.__class__.__name__}: {e}",
        ) from e
    except Exception as e:
        # Jinja2 渲染期抛非 TemplateError（如 str 与 int 比较的 TypeError）—— 同样视为
        # 路由表达式错误，fail loud 包成 RouteError（不漏原始异常破坏 orchestrator 控制流）。
        raise RouteError(
            f"路由 when 表达式 {expr!r} 求值失败：{e.__class__.__name__}: {e}",
        ) from e
    return _truthy(rendered)


def _truthy(rendered: str) -> bool:
    """Jinja2 渲染结果的 truthy 判定（贴近 SPEC「求值为 bool」语义）。

    - 空串 → False
    - "False" / "false" / "none" / "0" / "[]" / "{}" → False（常见 falsy 字面量）
    - 其余非空串 → True
    """
    stripped = rendered.strip()
    if not stripped:
        return False
    return stripped.lower() not in {"false", "none", "0", "[]", "{}", "null"}
