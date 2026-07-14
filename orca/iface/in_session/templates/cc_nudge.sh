#!/usr/bin/env bash
# Orca CC nudge —— Claude Code Stop hook（v5 §4.4 / step 2b(7)）。
#
# 主 session 试图结束其 turn 时触发：若有活跃 Orca run（marker 存在）→ 发 decision:block
# 注入「请调 orca next 推进」的提醒。**绝不调 orca next**（B 路径铁律：主 session
# 自调 next；hook 自动调 next = 退化 A 路径）。
#
# 判定只看 marker 存在（runs/orca-<run_id>.json）——不用 tape 超时（会误报）。marker
# 在终态由 CLI 清掉，故「有 marker」≡「run 还活着」。
#
# 节流：60s 内不重复 block（防 Stop 反复触发刷屏）。block 后写时间戳，窗口内再次 Stop 直
# 接放行（让模型能停下来等用户/子代理）；窗口外再 block。
#
# 由 'teams install --target cc' 落到 <cc_root>/hooks/orca-nudge.sh 并在
# .claude/settings.json 的 hooks.Stop 声明引用。
#
# 实现：python3（DEFECT-1 修复；orca 本就依赖 python，跨环境可靠——WSL conda orca 等环境
# 不一定有 jq）。**fail loud**：marker 文件不可读 / 非合法 JSON → 写 stderr + exit 2，绝不
# 静默吞错（旧版用 jq 加 2>/dev/null 加 || true 在缺 jq 时静默失败 → nudge 永不触发且无报错，
# 违反 fail-loud；用户看不到任何信号）。
#
# 铁律：本脚本全篇**零反引号**——REASON 是双引号 bash 字符串，双引号内反引号 = 命令替换，
# 会误执行 orca next 退化 A 路径。命令名一律纯文本提及（提醒模型去调，非脚本执行）。
set -euo pipefail

# CC Stop hook 的 cwd = 用户项目根；python heredoc 在该 cwd 下跑，相对路径 runs/... 即项目
# runs/ 目录（marker 落点 = runs/orca-<run_id>.json）。
exec python3 - <<'PYEOF'
import glob
import json
import os
import sys
import time

STATE = "runs/.orca-nudge-cc"
THROTTLE_SEC = 60


def _read_throttle_timestamp() -> int:
    """读上次 block 的时间戳。文件不存在 / 损坏 / 不可读 → 0（视作可再次 block）。

    throttle 是 hook 本地 best-effort 态（非 orca 真相源），任何读取异常都不该阻断
    主流程——视作「无节流记录，可再次 block」。与 marker 路径的 fail loud 设计对称区别：
    marker 是 orca CLI 经 atomic_write_json 写出的真相源，损坏 = orca 状态已乱，必须报。
    """
    try:
        with open(STATE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _scan_active_run_ids() -> list[str]:
    """扫 runs/orca-*.json 取 run_id。

    marker 文件由 orca CLI 经 sidecar_io.atomic_write_json 写出，合法即合法 JSON。
    读失败 / JSON 非法 → **fail loud**（stderr + exit 2，详见脚本头注释 DEFECT-1 段）。
    """
    # sorted：让 REASON 中 run_id 列举顺序确定，便于断言 / 复现（旧版 bash glob 顺序 FS 相关）。
    ids: list[str] = []
    for path in sorted(glob.glob("runs/orca-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(
                f"orca-nudge: marker {path} 不可读 / 非合法 JSON：{e}\n"
            )
            sys.exit(2)
        rid = data.get("run_id")
        if rid:
            ids.append(str(rid))
    return ids


def main() -> int:
    now = int(time.time())
    # 节流：60s 窗口内放行（让模型能停下来等用户 / 子代理）。
    if now - _read_throttle_timestamp() < THROTTLE_SEC:
        return 0

    ids = _scan_active_run_ids()
    # 无活跃 run → 放行（不 block）。
    if not ids:
        return 0

    # 记节流时间戳 + 发 block 提醒。
    os.makedirs("runs", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        f.write(str(now))

    reason = (
        f"你还有活跃的 Orca run：{', '.join(ids)}。"
        "若上一个节点的子代理已完成，请把它的产出作为 --output 调 "
        "orca next --run-id <run_id> --output '<产出>' 推进；"
        "若 workflow 已结束或要中止，先 orca stop <run_id>。"
        "（Orca nudge：提醒，Orca 不会自动推进。）"
    )
    # 输出 decision:block JSON（CC Stop hook 协议：block = force continuation）。
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
PYEOF
