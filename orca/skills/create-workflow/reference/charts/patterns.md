# 3 种集成模式 + 代码样板

## 模式选择

```
数据是脚本自身产的？ ─────────────────────────────────────┐
  │                                                         │
  ├─ 是，且脚本有 Python 源码可改 ──> 【模式 A: inline】     │
  │                                                         │
  ├─ 是，但脚本属外部项目不可改（如第三方仓库的训练脚本）       │
  │       └─> 【模式 B: sidecar 轮询】                       │
  │                                                         │
  └─ 否，数据是 shell 命令跑的（bash train.sh 写 jsonl）     │
          └─> 【模式 B: sidecar 轮询】                       │
                                                             │
数据是终态报告（跑完一次、不再更新）？                        │
  └─> 【模式 C: finalize 推图】— 在最终节点调一次即可        │
```

---

## 模式 A: Inline — 脚本内直接推图

**适用**：你能改的 Python 脚本，数据在脚本生命周期内产生。

**做法**：脚本末尾，数据写盘之后，加 `render_chart(...)`。不改主逻辑。

**关键约束**：
- chart 调用外层 `try/except`，失败只 stderr
- 放在 `if __name__ == "__main__":` 块内或函数 return 之前
- 别插在循环体内（性能）——循环结束后汇总数据一次推

**代码骨架**：

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from orca.chart import render_chart  # type: ignore
except Exception:
    sys.stderr.write("[my_script] 不在 Orca run 上下文中，跳过 chart 推送\n")
    render_chart = None


def main() -> int:
    output_dir = Path(sys.argv[1])  # 或 argparse

    # === 核心逻辑（不改） ===
    results: list[dict] = do_work(output_dir)
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(results, indent=2))

    # === chart 推送（新增，放在最后） ===
    if render_chart is not None:
        try:
            render_chart(
                chart_type="line",
                data=results,
                label="my_workflow/main",
                title="Key Metric Over Time",
                x="step",
                y="metric",
                x_label="步数",
                y_label="指标",
                caption="核心指标随时间变化。",
            )
        except Exception as e:
            sys.stderr.write(f"[my_script] chart 推送失败（不阻断）：{e}\n")
    return 0
```

**完整例子**：`../../examples/charts/inline-pattern.py`。

---

## 模式 B: Sidecar — 独立轮询脚本

**适用**：数据由外部进程产（agent 跑 bash 脚本 / 第三方 CLI），或脚本不可改。

**做法**：
1. 新建/修改一个独立的 Python 脚本（如 `push_charts.py`），负责读数据文件 → 推图
2. 在 agent.md 的 bash 块里加一句周期调用：`while ...; do python3 push_charts.py; sleep 30; done`
3. 脚本是**幂等**的——每次跑读全量文件，重推同 label+title（前端替换 = 实时更新）

**关键约束**：
- 脚本幂等：每次跑推同 label+title → 前端自然替换，不需增量逻辑
- 文件不存在 → 静默退出 0（不是错误），下次轮询再试
- jsonl 半写尾巴行 → `json.JSONDecodeError` → 跳过该行

**代码骨架**：

```python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from orca.chart import render_chart  # type: ignore
except Exception:
    sys.stderr.write("[push_charts] 不在 Orca run 上下文中\n")
    sys.exit(2)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    output_dir = Path(args.output_dir)

    records = _read_jsonl(output_dir / "metrics.jsonl")
    if not records:
        return 0  # 文件还没出现，静默跳过

    # 验证 schema + 推图
    # ...
    return 0
```

**agent.md 里的调用方式**：
```markdown
## 执行

```bash
OUTPUT_DIR="{{ setup.output.output_dir }}"

# 启动主任务 + sidecar 并行
python3 train.py --output_dir "$OUTPUT_DIR" &
TRAIN_PID=$!

while kill -0 $TRAIN_PID 2>/dev/null; do
    python3 "$ORCA_AGENT_RESOURCES/scripts/push_charts.py" --output_dir "$OUTPUT_DIR" || true
    sleep 30
done

# 最终再跑一次确保尾数不漏
python3 "$ORCA_AGENT_RESOURCES/scripts/push_charts.py" --output_dir "$OUTPUT_DIR" || true
```
```

**完整例子**：`../../examples/charts/sidecar-pattern.py`。

---

## 模式 C: Finalize — 终态一次性推图

**适用**：workflow 跑完后一次性出终态报告图（如最终帕累托前沿、选择漏斗）。

**做法**：
1. 在最后一个节点（如 `select`）的脚本里，读全部历史数据
2. 在脚本末尾推终态图
3. 同 label 但 title 带 `(final)` 后缀，与 live 图区分

**关键约束**：
- 终态图离线算（如自算全局非支配前沿，不依赖 per-gen pareto 标志）
- 终态图 title 与 live 图不同（如 "Pareto Front (live)" vs "Pareto Front (final)"）
- 同 label，不同 title → 两张独立的图
- 如果源文件不存在 → skip 不报错

**代码骨架**：

```python
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    output_dir = Path(args.output_dir)

    summary_path = output_dir / "summary.json"
    if not summary_path.is_file():
        sys.stderr.write(f"[push_final] {summary_path} 不存在，跳过\n")
        return 0

    summary = json.loads(summary_path.read_text("utf-8"))

    # 离线计算终态数据
    stages = [
        ("input", summary.get("num_input", 0)),
        ("selected", summary.get("num_selected", 0)),
    ]
    data = [{"stage": name, "count": cnt} for name, cnt in stages]

    try:
        render_chart(
            chart_type="bar",
            data=data,
            label="my_workflow/selection",
            title="Selection Funnel (final)",
            x="stage",
            y="count",
            x_label="阶段",
            y_label="数量",
            caption="全流程收敛漏斗。",
        )
    except Exception as e:
        sys.stderr.write(f"[push_final] chart 推送失败（不阻断）：{e}\n")

    return 0
```

**完整例子**：`../../examples/charts/finalize-pattern.py`。

---

## 模式外：agent.md 最小改动

chart 脚本的调用只需一行 shell，除此以外不碰 agent.md 的其他内容：

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/push_charts.py" --output_dir "$OUTPUT_DIR" || true
```

三种模式对 agent.md 的影响：
- **inline**：chart 调用已在主脚本内 → agent.md 不需改
- **sidecar**：见上方模式 B 的 `while kill -0` 示例（并行启动主任务 + 轮询）
- **finalize**：chart 调用在最终脚本内 → agent.md 不需改（新建的脚本需加一行上述 bash 调用）
