"""metrics_tail.py —— KD-NAS 可配置模板 metrics 摘取 sidecar（SPEC §9）。

读 ``--template`` JSON（``inputs.metrics_template`` 经 Jinja 渲染传入，可空）→
tail ``source_log`` → 按 metrics 列表的 regex（named group）抽字段 → ``render_chart`` 推图。

schema（SPEC §9）::

    {
      "source_log": "<train log/jsonl 路径>",
      "metrics": [
        {"name": "nmse",
         "regex": "nmse=(?P<val>[0-9.]+)",
         "chart_type": "line",
         "x": "epoch",
         "y": "val"}
      ]
    }

模板空 → 默认行为：扫 ``source_log``（``--source_log`` CLI 入参）里的
``loss_avg=`` / ``kd_loss_avg=`` 行，推一张 loss line（与 train_pipeline 内置
``_make_live_push`` 同源；post-hoc 兜底，避免 live push 失败时图缺失）。

纪律（sidecar，与 viz_kd.py / viz_kd_stage.py 同源）：
  - 仅用 ``orca.chart.render_chart``；不输出 HTML。
  - ``source_log`` 缺 / 无效行 → 该图跳过（stderr WARN，不阻断）。
  - regex 无 named group / 匹配 0 行 → skip + WARN。
  - 不在 Orca 子进程内 → 整体跳过 + stderr 提示。
  - 单 metric 异常不影响其他 metric。
  - ``_main`` 兜底永远 emit 合法 JSON（agent dumb copy 进 viz_status，**必填字段**）。

与 ``train_pipeline._make_live_push`` 的分工（SPEC §6.6 / §9）：
  - ``_make_live_push``：训练循环内 live push loss（per-epoch；web 实时刷新）。
  - ``metrics_tail``：post-hoc 摘 log，模板可自定义（loss 之外如 nmse/snr 等）。
  两者**互补**：live push 需 ``--env_anchor`` 自举 ORCA env，metrics_tail 是
  post-hoc 兜底 + 多字段。

CLI::
    metrics_tail.py \\
      [--template '<SPEC §9 JSON>'] \\
      --source_log '<train log path>' \\
      --variant_id '<id>' \\
      --mode teacher|distill \\
      [--env_anchor '<per-run artifacts dir>']

stdout JSON（dumb copy 进 agent.viz_status）::
    {"viz_env_status": "<ok|env_loaded_from_file|env_missing|import_failed|generic>",
     "charts": {"<metric name>": {"pushed": <bool>, "reason": "<str>"}, ...}}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_LABEL = "kd-nas"
_orca_render_chart: Callable | None = None

# 锚定 train_pipeline stdout 行前缀（防误匹配用户 log 里偶发的 epoch=X loss=Y 行）。
# 契约：train_pipeline run_teacher_mode / run_distill_mode 末尾 print
#   "[train_pipeline:teacher] epoch=0 loss_avg=0.123456"
#   "[train_pipeline:distill] epoch=0 kd_loss_avg=0.654321"
# 改前缀 / 字段名 → 此处同步，否则 metrics_tail 静默 0 match + WARN。
_LOSS_LINE_RE = re.compile(
    r"\[train_pipeline:(?P<mode>teacher|distill)\]\s+epoch=(?P<epoch>\d+)\s+"
    r"(?P<key>loss_avg|kd_loss_avg)=(?P<val>[0-9.eE+-]+)"
)


# ── env bootstrap ──────────────────────────────────────────────────────────


def _bootstrap_render_chart(env_anchor: str) -> str:
    global _orca_render_chart
    env_status = "env_missing"
    if env_anchor:
        try:
            from orca.chart._env import load_run_env_from_artifacts  # type: ignore
            load_run_env_from_artifacts(env_anchor)
            env_status = "env_loaded_from_file"
        except Exception as e:  # noqa: BLE001
            print(
                f"[metrics_tail] WARN: env_anchor 自举失败：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
    else:
        env_status = "ok" if any(k.startswith("ORCA_") for k in __import__("os").environ) else "env_missing"
    try:
        from orca.chart import render_chart  # type: ignore
        _orca_render_chart = render_chart
        if env_status != "env_loaded_from_file":
            env_status = "ok" if _orca_render_chart is not None else "import_failed"
    except Exception as e:  # noqa: BLE001
        print(
            f"[metrics_tail] WARN: orca.chart import 失败：{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        env_status = "import_failed"
        _orca_render_chart = None
    return env_status


def _read_log_tail(path: str, max_bytes: int = 2 * 1024 * 1024) -> str:
    """读 log 全文（>2MB 取末 2MB；train log 通常 <1MB，2MB 兜底防 OOM）。"""
    p = Path(path)
    if not p.is_file():
        return ""
    size = p.stat().st_size
    if size <= max_bytes:
        return p.read_text(encoding="utf-8", errors="replace")
    with p.open("rb") as f:
        f.seek(-max_bytes, 2)
        return f.read().decode("utf-8", errors="replace")


# ── 模板 metric 推送 ──────────────────────────────────────────────────────────


def _push_template_metric(
    metric: dict[str, Any],
    log_text: str,
) -> tuple[bool, str]:
    name = str(metric.get("name", "")).strip()
    regex = str(metric.get("regex", ""))
    chart_type = str(metric.get("chart_type", "line")).strip() or "line"
    x_field = str(metric.get("x", "")).strip()
    y_field = str(metric.get("y", "")).strip()
    if not name or not regex or not y_field:
        return False, f"metric 缺字段（name/regex/y 必填）：{sorted(metric)}"
    try:
        rx = re.compile(regex)
    except re.error as e:
        return False, f"regex 编译失败：{type(e).__name__}: {e}"
    group_names = set(rx.groupindex.keys())
    if y_field not in group_names:
        return False, f"y={y_field!r} 不是 regex 的 named group（现有：{sorted(group_names)}）"
    if x_field and x_field not in group_names:
        return False, f"x={x_field!r} 不是 regex 的 named group（现有：{sorted(group_names)}）"

    points: list[dict[str, Any]] = []
    for m in rx.finditer(log_text):
        try:
            row: dict[str, Any] = {}
            for gn, gv in m.groupdict().items():
                # 数值化所有 named group（坐标轴需数值；非数值保留为字符串）
                try:
                    row[gn] = float(gv) if gv is not None else None
                except (TypeError, ValueError):
                    row[gn] = gv
            points.append(row)
        except (TypeError, ValueError):
            continue
    if not points:
        return False, "regex 匹配 0 行（log 未生成 / regex 不对？）"
    assert _orca_render_chart is not None
    title = f"Template Metric — {name}"
    kwargs: dict[str, Any] = {
        "chart_type": chart_type,
        "data": points,
        "label": _LABEL,
        "title": title,
        "y": y_field,
        "y_label": metric.get("y_label", name),
        "caption": (
            f"metrics_tail post-hoc 摘 source_log 推图；regex={regex!r}；"
            f"matched {len(points)} points。"
        ),
    }
    if x_field:
        kwargs["x"] = x_field
        kwargs["x_label"] = metric.get("x_label", x_field)
    _orca_render_chart(**kwargs)
    return True, f"ok ({len(points)} points)"


# ── 默认 loss 推送（无模板）────────────────────────────────────────────────


def _push_default_loss(log_text: str, variant_id: str, mode: str) -> tuple[bool, str]:
    """无 template 时，扫 train_pipeline stdout 的 loss_avg= / kd_loss_avg= 行。"""
    points: list[dict[str, Any]] = []
    for m in _LOSS_LINE_RE.finditer(log_text):
        try:
            ep = int(m.group("epoch"))
            val = float(m.group("val"))
        except (TypeError, ValueError):
            continue
        points.append({"epoch": ep, "loss": val, "mode": m.group("key")})
    if not points:
        return False, "log 无 loss_avg=/kd_loss_avg= 行（live push 已推？训练未起？）"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="line",
        data=points,
        label=_LABEL,
        title=f"{mode} Training Loss (post-hoc tail) — {variant_id}",
        x="epoch",
        y="loss",
        hue="mode",
        x_label="epoch",
        y_label="loss（越低越好）",
        caption=(
            f"metrics_tail 默认 loss 推送（无 template）：扫 train_pipeline stdout "
            f"loss_avg=/kd_loss_avg= 行；variant={variant_id} mode={mode}。"
            "与 _make_live_push 互补（live 失败时此为兜底）。"
        ),
    )
    return True, f"ok ({len(points)} points)"


# ── 主入口 ─────────────────────────────────────────────────────────────────


def render_metrics(
    *,
    template: str,
    source_log: str,
    variant_id: str,
    mode: str,
    env_anchor: str,
) -> dict[str, Any]:
    env_status = _bootstrap_render_chart(env_anchor)
    result: dict[str, Any] = {"viz_env_status": env_status, "charts": {}}
    if _orca_render_chart is None:
        print(
            "[metrics_tail] WARN: orca.chart 不可用，跳过全部 web push（非 Orca 子进程？）",
            file=sys.stderr,
        )
        return result
    if not source_log or not Path(source_log).is_file():
        # sidecar：log 未生成不阻断；标 visible WARN 让 operator 看见。
        print(
            f"[metrics_tail] WARN: source_log 不存在：{source_log!r}（训练未起？跳过）",
            file=sys.stderr,
        )
        result["charts"]["_source_log_missing"] = {
            "pushed": False,
            "reason": f"source_log missing: {source_log!r}",
        }
        return result

    log_text = _read_log_tail(source_log)

    def _run(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, reason = fn()
        except Exception as e:  # noqa: BLE001
            print(
                f"[metrics_tail] WARN: 推送 {name} 异常，跳过：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            ok, reason = False, f"generic:{type(e).__name__}:{e}"
        result["charts"][name] = {"pushed": bool(ok), "reason": reason}

    # 解析模板：空 / 非合法 JSON / 非 object → 默认 loss 推送（不阻断）。
    tpl: dict[str, Any] | None = None
    if template and template.strip():
        try:
            parsed = json.loads(template)
            if isinstance(parsed, dict):
                tpl = parsed
            else:
                print(
                    f"[metrics_tail] WARN: template 非 object（{type(parsed).__name__}）→ 走默认 loss",
                    file=sys.stderr,
                )
        except json.JSONDecodeError as e:
            print(
                f"[metrics_tail] WARN: template 非合法 JSON：{e} → 走默认 loss",
                file=sys.stderr,
            )

    if tpl is None:
        _run("default_loss", lambda: _push_default_loss(log_text, variant_id, mode))
        return result

    # 有模板：source_log 取 tpl['source_log']（CLI --source_log fallback）。
    src = str(tpl.get("source_log") or "") or source_log
    if src != source_log:
        log_text = _read_log_tail(src)
    metrics = tpl.get("metrics") or []
    if not isinstance(metrics, list) or not metrics:
        print(
            "[metrics_tail] WARN: template.metrics 非列表或为空 → 走默认 loss",
            file=sys.stderr,
        )
        _run("default_loss", lambda: _push_default_loss(log_text, variant_id, mode))
        return result

    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name", "")).strip() or "unnamed_metric"
        _run(name, lambda m=m: _push_template_metric(m, log_text))
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="KD-NAS 可配置模板 metrics 摘取 sidecar（SPEC §9）"
    )
    parser.add_argument("--template", default="", help="SPEC §9 JSON 串（可空→走默认 loss）")
    parser.add_argument("--source_log", required=True, help="train log 路径")
    parser.add_argument("--variant_id", default="model", help="变体 id（图 title 用）")
    parser.add_argument(
        "--mode", default="teacher", choices=["teacher", "distill"], help="训练模式"
    )
    parser.add_argument("--env_anchor", default="", help="per-run $ORCA_ARTIFACTS_DIR 锚点")
    args = parser.parse_args()

    try:
        result = render_metrics(
            template=args.template,
            source_log=args.source_log,
            variant_id=args.variant_id,
            mode=args.mode,
            env_anchor=args.env_anchor,
        )
    except Exception as e:  # noqa: BLE001
        # _main 兜底：永远 emit 合法 JSON。
        print(f"[metrics_tail] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result = {
            "viz_env_status": "generic",
            "charts": {
                "_tail_failed": {"pushed": False, "reason": f"generic:{type(e).__name__}:{e}"}
            },
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
