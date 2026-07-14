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
# 铁律：本脚本全篇**零反引号**——REASON 是双引号 bash 字符串，双引号内反引号 = 命令替换，
# 会误执行 orca next 退化 A 路径。命令名一律纯文本提及（提醒模型去调，非脚本执行）。
set -euo pipefail

STATE="runs/.orca-nudge-cc"
NOW=$(date +%s)

# 节流：60s 窗口内放行。
if [ -f "$STATE" ]; then
    LAST=$(cat "$STATE" 2>/dev/null || echo 0)
    case "$LAST" in *[!0-9]*) LAST=0 ;; esac
    if [ $((NOW - LAST)) -lt 60 ]; then exit 0; fi
fi

# 扫活跃 marker（nullglob：无匹配时循环体不执行）。
shopt -s nullglob
IDS=""
for m in runs/orca-*.json; do
    RID=$(jq -r '.run_id // empty' "$m" 2>/dev/null || true)
    [ -n "$RID" ] || continue
    if [ -z "$IDS" ]; then IDS="$RID"; else IDS="$IDS, $RID"; fi
done

# 无活跃 run → 放行（不 block）。
[ -z "$IDS" ] && exit 0

# 记节流时间戳 + 发 block 提醒。
mkdir -p runs
echo "$NOW" > "$STATE"
REASON="你还有活跃的 Orca run：${IDS}。若上一个节点的子代理已完成，请把它的产出作为 --output 调 orca next --run-id <run_id> --output '<产出>' 推进；若 workflow 已结束或要中止，先 orca stop <run_id>。（Orca nudge：提醒，Orca 不会自动推进。）"
jq -n --arg r "$REASON" '{decision:"block", reason:$r}'
