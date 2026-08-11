# SPEC: v2 审计 —— ③ expand 归因健壮性 + ④ resume attempt N + ⑤ ckpt 相对路径核验（S4b）

- 日期：2026-08-11
- 状态：**Reviewed-Conditional-Pass → 修订定稿**（spec-reviewer 主审+evaluator 双轮 14 项闭环：3 Blocker Q1/Q2/Q5-Q6 修订✓ + hardening Q3/Q4/Q7/Q8/Q9/Q10✓ + Q11 部分驳回✓ + Q12/Q13/Q14 驳回✓）
- 关联：CURRENT.md S4b（v2 审计 6 项中的 3 项 SDD 项；另 3 项 ⑧⑦⑥ 为 S4a 小直接改，无 SPEC）。
- 影响层：`workflows/agents/ns2_report/` + `ns2_expand_supernet/`（③）；`ns2_run_search/`（④）；`ns2_retrain/`（⑤，核验为主）；v1（`ns_*`）/ v3（`ns3_*`）镜像。

---

## 1. 三项根因（已勘察）

### ③ expand unsupported 归因依赖 LLM 子串（健壮性）
`ns2_report/agent.md` Step 1 python（`:136-139`）扫 `supernet_summary.md` 逐行 `if "No supported match" in line` → 命中则 `stage="expand", status="failed"`（unsupported）。但 `supernet_summary.md` 由 `ns2_expand_supernet` agent（LLM）按 prompt 写——prompt（`ns2_expand_supernet/agent.md:100,173`）说 Model Type 行写「`model_type.json` 标签 或 `No supported match`」，**但 LLM 不保证字面写这串**（可能写 "unsupported"、"no match"、带括号说明、或漏写）。LLM 漏写 → report 不命中 → 落默认 `stage="report"`（`report.py:142`）→ **归因错误**（明明 expand 阶段判 unsupported，却报 report 阶段失败）。

**根因**：终态归因依赖 LLM 生成的自由文本子串，非结构化信号。expand 的 `output_schema` 已有结构字段 `model_type_supported`（boolean，`ns2_expand_supernet/agent.md:105,220`）——这是确定性信号，但 report 不读它（report 零跨节点 output 引用铁律，`ns2_report/agent.md:42-46`）。

### ④ ns2_run_search resume 丢 attempt N
`ns2_run_search/agent.md` Step R（`:67-82`）：turn 到顶 fresh sub-agent 经 Step R 检测搜索在跑 → 直进 Step 2b 轮询。但 Step 2a/2b/2c 用 `${N}`（attempt 号）命名 log（`search.attempt${N}.stdout.log`）+ Step 2c self-heal `N++`。Step R **不重建 N**——fresh sub-agent 的 `N` 未定义（或回落 1）→ 轮询读错 log / 重 detach 覆盖既有 attempt log / attempt 计数从 1 重来（丢失「这是第几次尝试」语义，`.ns_run_search_healed.txt` 与 attempt 号脱钩）。

**根因**：Step R 续接重建状态时漏了 attempt 计数器；`N` 是 bash 变量，跨 turn（fresh sub-agent = 新 shell）不持久。**且 fresh sub-agent 未区分「搜索在跑 vs 搜索已死」两种语义下 N 的含义不同**（reviewer Q2 Blocker）：在跑时 N 应 = 当前在跑的 attempt 号（2b 立即用于 `tail` log）；已死时 N 应 = max+1（下次重 detach 不覆盖）。一刀切 max+1 会在历史死 attempt log 残留场景下 tail 不存在的 log → 误判假死 → `kill -- -<pgid>` 误杀在跑的搜索。

### ⑤ ns2_retrain ckpt 相对路径（核验 —— 疑已修）
CURRENT.md 记「status.sh 写相对 ckpt」。勘察：`ns2_retrain/scripts/status.sh` 已有绝对路径防御（`case "$CKPT" in /*) ;; *) CKPT="$ORCA_ARTIFACTS_DIR/$CKPT" ;; esac`），写 marker `.ns_retrain_ckpt_resolved.txt` 用绝对 `$CKPT`；`emit_result.py:83-86` 读 marker，回落 `os.path.join(ad, "runs/retrain/retrain_best.pth")`（ad 绝对 → 绝对）。**两路径都已绝对**。

**根因待定**：疑为**陈旧条目**（绝对路径防御后已不触发），或残留旁路写 marker（未定位）。属 verify-and-close：若无残留写相对路径的代码 → 直接 close（SPEC 记核验结论）；若有 → 收紧到与 status.sh 同款 case-block。

## 2. 设计

### ③ 归因结构信号化（磁盘 marker，绕开零跨节点 output 铁律）
**不破坏** report「零跨节点 output 引用」铁律——改用**磁盘 marker**（report 已有的「从盘读判终态」模式同款）：
- **触发条件**（Q11）：仅 `ns2_expand_supernet` Step 1.5「Stop unsupported NAS branches」分支（`Model Type` 不是 `model_type.json` 标签之一）写 marker；supported 路径**不**写。
- **写入**（Q4）：`printf 'true' > "$ORCA_ARTIFACTS_DIR/.ns_expand_unsupported.flag"`（内容 `'true'`，与项目 fidelity flag 同款 DRY）。
- **清理协议**（Q5/Q6 Blocker）：expand Step 0 reuse-check 之前**必先** `rm -f "$ORCA_ARTIFACTS_DIR/.ns_expand_unsupported.flag"`（与 `ns_run_search` Step 1 rm 协议同款），无论 reuse 命中与否；仅 fresh classify 落 unsupported 分支再写。覆盖跨 run artifact 复用残留 + 跨 attempt 重派（attempt1 unsupported → attempt2 重判 supported）残留两个场景。
- **失败语义**（Q10）：marker 是 **best-effort 加速信号**，summary 子串 grep 是 ground truth 兜底。marker 落盘失败（disk full / 权限）仅 `echo "WARN: marker write failed" >&2`，**不阻塞** emit JSON（违反 fail-loud 的「必须给引擎 JSON」原则更糟；emit `model_type_supported=false` 是 fail-loud 主路径）。
- **report 读法**（Q4）：`read_text(os.path.join(ad, ".ns_expand_unsupported.flag")) == "true"`（**内容判定**，非存在判定，与 fidelity flag DRY 一致）。
- **双信号**：marker 命中 ⇔ unsupported；marker 缺 + summary 含 "No supported match" 子串 ⇔ unsupported（兜底，向后兼容在途 run）；两者皆无 + 无其他终态 ⇔ 落默认 stage=report（不误报）。
- **同步范围**（Q1 Blocker）：**仅 v2**（`ns2_expand_supernet` + `ns2_report`）+ **v3**（`ns3_expand_supernet` + `ns3_report`）。**v1 不纳入**——v1 流水线无终端 report 节点，unsupported 由 expand 自身 emit `model_type_supported=false` → 引擎直路由 `terminate_unsupported`，不存在归因依赖 LLM 子串的问题；给 v1 加 marker 是 dead code（无人读）。

### ④ Step R 重建 attempt N（两分支语义区分 —— Q2 Blocker）
fresh sub-agent 经 Step R 续接时，从盘重建 N。**两种 Step R 分支下 N 的语义和计算公式不同**（禁一刀切 max+1——会在死 attempt log 残留时 tail 不存在的 log → 误判假死 → `kill -- -<pgid>` 误杀在跑搜索）：

```bash
# Step R 续接（两分支各自算 N，不共享）
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
SPID="$(cat runs/search/.search_pid 2>/dev/null || echo '')"
if [ -n "$SPID" ] && kill -0 "$SPID" 2>/dev/null; then
  # ── 分支 A：RESUME_SEARCH（搜索在跑）── N = 当前在跑的 attempt 号（2b 立即用于 tail）
  # 用 latest-mtime 反推（死 attempt log 残留时 max 号 ≠ 当前在跑号）
  N=$(ls -t runs/search/search.attempt*.stdout.log 2>/dev/null | head -1 \
    | sed -n 's/.*attempt\([0-9]*\)\.stdout\.log/\1/p')
  N=${N:-1}
  echo "RESUME_SEARCH pid=$SPID attempt=$N 搜索在跑，直进 Step 2b 轮询"
else
  # ── 分支 B：RESUME_HEAL（搜索已死/未启）── N = max(既有号)+1（Step 2a 重 detach 不覆盖）
  LAST_N=$(ls runs/search/search.attempt*.stdout.log 2>/dev/null \
    | sed -n 's/.*attempt\([0-9]*\)\.stdout\.log/\1/p' | sort -n | tail -1)
  N=$(( ${LAST_N:-0} + 1 ))
  echo "RESUME_HEAL new_attempt=$N 搜索未在跑，正常 Step 0 → Step 1 → Step 2a 重 detach"
fi
```
- **分支 A (RESUME_SEARCH)**：N = latest-mtime log 号——跳 Step 0/1，直进 Step 2b，**2b 立即用此 N** `tail -8 search.attempt${N}.stdout.log`。
- **分支 B (RESUME_HEAL)**：N = max+1——正常 Step 0 → Step 1 → Step 2a `setsid` 写新 log 不覆盖既有；后续 2b/2c 用此 N。
- **marker 关系**：N 仅作 log 命名计数，**与 `.ns_run_search_healed.txt` / `.ns_run_search_fidelity.flag` 脱钩**（healed marker 是 self-heal 行为痕迹，N 是 attempt 计数，独立）。Step R 不清 healed marker（续接保留）；Step 0/1 走到时再清。
- **同步范围**：v1（`ns_run_search`）+ v2（`ns2_run_search`）+ v3（`ns3_run_search`）三版本同改。

### ⑤ 核验 + 收紧（若需 —— Q7 修笔误 + 显式范围）
- **核验范围**：`ns2_retrain` + `ns_retrain` + `ns3_retrain` 三版本，各覆盖 `scripts/*` + `*.py`（尤其 `emit_result.py`）+ `agent.md` body 内 heredoc/inline bash。
- **grep 模式**（显式）：`\.ns_retrain_ckpt_resolved\.txt`、`retrain_best\.pth`、`CKPT=`、`os\.path\.join.*retrain`。
- **结论模板**：列每个写 marker 的代码点 × 写绝对/相对 × 是否已防御（case-block / `os.path.join`）× 决定（close / 收紧点 + 修法）。
- **初判**：基线已 grep，三版本 `status.sh` 都有 case-block 绝对化、`emit_result.py` 都有 `os.path.join(ad, ...)`、`launch.sh` 只 `rm` 不写——**三版本同款已防御**。属 verify-and-close，release note 记核验结论 + grep 输出。若发现旁路写相对路径的代码点 → 统一加 `case ... /*) ;; *) absolutize ;; esac`。

## 3. AC（可证伪）

- **AC-③1**：`ns2_expand_supernet` Step 1.5 unsupported 分支 → marker 落盘且内容 `'true'`（agent.md 指令 + fixture 模拟 unsupported classify）；supported 分支禁写（Step 0 rm 后不落）。
- **AC-③2**：双信号 4-case 矩阵（测试参数化）：
  | marker 内容 | summary 含 "No supported match" | 期望 stage / status |
  |---|---|---|
  | `'true'` | 是 | expand / failed |
  | `'true'` | 否 | expand / failed |
  | 缺/非 `'true'` | 是 | expand / failed（兜底） |
  | 缺/非 `'true'` | 否 | 落默认 stage=report（不误报） |
- **AC-③3**（Q1）：仅 v2（`ns2_expand_supernet` + `ns2_report`）+ v3（`ns3_expand_supernet` + `ns3_report`）同步。**v1 不纳入**（无终端 report 节点读 marker，加它是 dead code）。
- **AC-④1**（Q2 两分支）：(a) RESUME_SEARCH——attempt1/2 log 都在（attempt1 mtime 旧=死，attempt2 mtime 新=在跑）+ `.search_pid` 在 → 重建 **N=2**（不是 3）；(b) RESUME_HEAL——attempt1/2/3 log 都在 + `.search_pid` 不在 → 重建 **N=4**；(c) 无既有 log + `.search_pid` 不在 → **N=1**。
- **AC-④2**（Q8 fixture）：HEAL 重 detach 用 RESUME_HEAL 重建的 N——fixture 记录已有 attempt1/2/3 log 的 mtime+内容快照 → 触发 detach 写 attempt4 → assert attempt1/2/3 mtime/内容未变（不覆盖）。
- **AC-⑤1**：核验报告——列 `ns2_retrain` + `ns_retrain` + `ns3_retrain` 三版本所有写 ckpt 路径 marker 的代码点（`scripts/*` + `*.py` + `agent.md` body）× 绝对/相对 × 是否已防御。结论：close（已防御）或收紧（具体修法）。

## 4. 测试

- ③：`tests/workflows/` 新增——expand fixture（模拟 Step 1.5 unsupported classify）写 marker（内容 `'true'`）+ rm 协议验证；report python（提取 heredoc）判 stage；**AC-③2 4-case 参数化矩阵**（marker × summary 子串）。
- ④：bash unit——Step R 两分支片段在 mock `runs/search/` 不同 attempt log 组合（mtime + `.search_pid` 存否）下重建 N（AC-④1 a/b/c）；AC-④2 fixture 记录既有 log 快照 → 触发 detach → assert 不覆盖。
- ⑤：核验脚本/grep（三版本 `scripts/*` + `*.py` + `agent.md` body）+ 结论记 release note。

## 5. 依赖铁律自检
- ③ marker 仅作 report 单向读（expand 写、report 读），非跨节点 output——不违 report 零跨节点 output 铁律。✓
- ④ 两分支 N 公式独立计算（latest-mtime / max+1），不引入跨分支状态依赖。✓
- ⑤ 核验脚本只读 scripts/py/agent.md，无副作用。✓
- 无 schema/cli/orchestrator 改动（与 S3 InputDef.enum 正交）。✓

## 6. 不做
- 不改 expand 的 summary 写法自由度（LLM 仍写 summary 供人读；marker 是机器信号）。
- 不为 ④ 引入持久化 attempt 状态文件（latest-mtime 反推仍是 log 文件名反推，KISS）。
- 不为 ③ 让 marker 写失败阻塞 emit JSON（best-effort；违反 fail-loud 的「必须给引擎 JSON」原则更糟，Q10）。
- ⑤ 若已修则不强造改动（fail-loud 不等于制造改动）。
- v1 不加 ③ marker（dead code，Q1）。
