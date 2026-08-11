# Release: v2 审计 S4b —— expand 归因 marker + resume attempt N + retrain ckpt 核验

- 日期：2026-08-11
- Commit：`9f8c301`
- SPEC：[`docs/specs/2026-08-11-v2-audit-attribution-resume-ckpt.md`](../specs/2026-08-11-v2-audit-attribution-resume-ckpt.md)（spec-reviewer 双轮对抗 conditional-pass，14 项闭环：3 Blocker Q1/Q2/Q5-Q6 修订 ✓ + hardening Q3/Q4/Q7/Q8/Q9/Q10 ✓ + Q11 部分驳回 ✓ + Q12/Q13/Q14 驳回 ✓）
- 关联：超 S4a `d768879`（同次 v2 审计的 3 项小修）；本 release 是另 3 项 SDD 项 ③④⑤。

---

## 做了什么

v2 审计 6 项中的 3 项 SDD 项，全部按修订后 SPEC 落地。

### ③ expand unsupported 归因 marker 化（§2.3）

**根因**：终态归因依赖 LLM 生成的 `supernet_summary.md` 自由文本子串 `"No supported match"`——LLM 不保证字面写这串（可能写 "unsupported" / "no match" / 带括号说明 / 漏写）→ report 不命中 → 落默认 `stage=report` → 归因错误（明明 expand 阶段判 unsupported，却报 report 阶段失败）。

**修法**：引入**磁盘 marker**（绕开 report「零跨节点 output 引用」铁律——marker 是从盘读判终态，与 report 已有的 disk-read 模式同款）：

- `ns2_expand_supernet` / `ns3_expand_supernet` Step 0 reuse-check **之前** `rm -f .ns_expand_unsupported.flag`（Q5/Q6 rm 协议，与 `ns_run_search` Step 1 rm 同款，无论 reuse 命中与否都先清，防跨 run/attempt 残留——attempt1 unsupported → attempt2 重判 supported 时，旧 marker 会让 report 误归因）。
- Step 1.5 「Stop unsupported NAS branches」分支 `printf 'true' > .ns_expand_unsupported.flag`（Q4 内容 `'true'`；Q11 仅 unsupported 分支写，supported 不写；Q10 best-effort，`2>/dev/null || echo WARN >&2` 不阻塞 emit JSON——违反 fail-loud 的「必须给引擎 JSON」原则更糟）。
- `ns2_report` / `ns3_report` terminal-state python 双信号：`unsupported = (read_text(marker) == "true") or summary_has_substring`（marker 优先，summary 子串兜底，向后兼容在途 run）。删去旧 `model_type` 中转变量，命名直白。

**Q1 Blocker**：仅 v2 + v3 同步——v1 无终端 report 节点，unsupported 由 expand 自身 emit `model_type_supported=false` → 引擎直路由 `terminate_unsupported`，加 marker 是 dead code（无人读）。`ns_expand_supernet/agent.md` 不动，`test_v1_not_included` 钉此契约。

### ④ Step R 两分支重建 attempt N（§2.4）

**根因**：`ns_run_search` Step R 续接重建状态时漏了 attempt 计数器 N——fresh sub-agent（新 shell）的 `N` 未定义或回落 1 → 轮询读错 log / 重 detach 覆盖既有 attempt log / attempt 计数从 1 重来。**且未区分「搜索在跑 vs 搜索已死」两种语义下 N 的含义不同**（reviewer Q2 Blocker）：一刀切 max+1 会在死 attempt log 残留场景下 `tail` 不存在的 log → 误判假死 → `kill -- -<pgid>` 误杀在跑搜索。

**修法**：Step R 拆两分支（bash 伪码逐字实现自 SPEC §2.4）：

```bash
if [ -n "$SPID" ] && kill -0 "$SPID" 2>/dev/null; then
  # 分支 A：RESUME_SEARCH（搜索在跑）— N = latest-mtime log 号（2b 立即用于 tail）
  N=$(ls -t runs/search/search.attempt*.stdout.log 2>/dev/null | head -1 \
    | sed -n 's/.*attempt\([0-9]*\)\.stdout\.log/\1/p')
  N=${N:-1}
else
  # 分支 B：RESUME_HEAL（搜索已死/未启）— N = max(既有号)+1（Step 2a 重 detach 不覆盖既有 log）
  LAST_N=$(ls runs/search/search.attempt*.stdout.log 2>/dev/null \
    | sed -n 's/.*attempt\([0-9]*\)\.stdout\.log/\1/p' | sort -n | tail -1)
  N=$(( ${LAST_N:-0} + 1 ))
fi
```

- 分支 A (RESUME_SEARCH)：N = latest-mtime log 号——跳 Step 0/1 直进 Step 2b，2b 立即用此 N `tail -8 search.attempt${N}.stdout.log`。
- 分支 B (RESUME_HEAL)：N = max+1——正常 Step 0 → Step 1 → Step 2a 写新 log 不覆盖既有。
- N 仅作 log 命名计数，与 `.ns_run_search_healed.txt` / `.ns_run_search_fidelity.flag` 脱钩（healed marker 是 self-heal 行为痕迹，N 是 attempt 计数，独立）；Step R 不清 healed marker（续接保留），Step 0/1 走到时再清。

**同步范围**：v1 (`ns_run_search`) + v2 (`ns2_run_search`) + v3 (`ns3_run_search`) 三版本同改（v3 英文）。顺手把 Step R intro 的陈旧注释「nohup detach」改为「setsid detach」（Step 2a 实际用 setsid，a57190b 已切）。

### ⑤ retrain ckpt 相对路径核验（§2.5）—— verify-and-close

CURRENT.md 记「status.sh 写相对 ckpt」。勘察结论：**已防御，三版本同款**，零代码改动。grep 证据（Q7 模式 `\.ns_retrain_ckpt_resolved\.txt` / `retrain_best\.pth` / `CKPT=` / `os\.path\.join.*retrain`）：

| 版本 | marker 写点 | 写绝对/相对 | 防御机制 | 结论 |
|---|---|---|---|---|
| `ns_retrain` | `scripts/status.sh:41` `printf '%s' "$CKPT" > .ns_retrain_ckpt_resolved.txt` | 绝对 | `case "$CKPT" in /*) ;; *) CKPT="$ORCA_ARTIFACTS_DIR/$CKPT" ;; esac` 在 line 27-30 先绝对化，line 41 才写 | 已防御 ✓ |
| `ns2_retrain` | `scripts/status.sh:41` 同上 | 绝对 | 同款 case-block | 已防御 ✓ |
| `ns3_retrain` | `scripts/status.sh:41` 同上 | 绝对 | 同款 case-block | 已防御 ✓ |

`emit_result.py:83-86`（三版本同款）读 marker，回落 `os.path.join(ad, "runs/retrain/retrain_best.pth")`（ad 绝对 → 绝对）；`launch.sh:31`（三版本同款）只 `rm` 不写。**无旁路写相对路径的代码点**——CURRENT.md 的旧条目是 status.sh 加 case-block 之前遗留的陈旧记录，现已不触发。**close**。

---

## 测试覆盖

| AC | 测试文件 | 用例数 |
|---|---|---|
| AC-③1（rm 协议 + unsupported 写 marker）| `tests/workflows/test_v2_audit_attribution.py::TestExpandMarkerProtocol` | 7（v2/v3 × step0/step1.5 + v1 不纳入 + Q11 单写点反证 v2/v3） |
| AC-③2（双信号 4-case 矩阵）| 同文件 `test_ac_3_2_double_signal_matrix` | 8（v2/v3 × 4 case） |
| AC-③2 边界（非 'true' 内容不触发）| 同文件 `test_non_true_marker_content_does_not_fire` | 2（v2/v3） |
| AC-③2 跨分支优先级（unsupported > stale retrain_rc）| 同文件 `test_unsupported_wins_over_stale_retrain_rc` | 2（v2/v3） |
| AC-③3（v1 不纳入）| 同文件 `test_v1_not_included` | 1 |
| AC-④1 a/b/c（两分支 N 重建）| `tests/workflows/test_v2_audit_resume_attempt.py::TestACFourOneBranchNReconstruction` | 9（v1/v2/v3 × 3 case） |
| AC-④1 边界（`.search_pid` 空文件 → RESUME_HEAL）| 同文件 `test_b_empty_search_pid_file_routes_to_resume_heal` | 3（v1/v2/v3） |
| AC-④2（HEAL 重 detach 不覆盖）| 同文件 `TestACFourTwoNoOverwrite` | 3（v1/v2/v3） |
| AC-⑤1（核验报告）| 本 release note（grep 证据，无单测——SPEC §4 明定） | — |

新增 36 测试全过；回归 `test_ns_chart_scripts.py` 81 passed（确认 ④ 改 Step R 没碰坏搜索图表）+ `test_direction_coverage.py` + `test_struct_kd_p7.py` + `test_nas_supernet_enum_gate.py` 58 passed / 2 skipped（agent.md 结构契约不破）。`bash -n` 验证所有改动 agent.md 内 bash 片段语法 OK；`py_compile` 验证 report heredoc 解析 OK；ruff 干净。

测试质量（Rule 9）：所有行为测试**提取 agent.md 真 bash/python 源码**（regex 抽 Step R bash fenced block / report `PYEOF` heredoc），非手抄——agent.md 改了测试自动跟随。

### code-reviewer 闭环

派 code-reviewer 自审（依赖铁律 / Q1 v1 是否误纳入 / Q2 两分支 / Q5 rm 协议 / marker 内容 / fail-loud）。结论：**🔴 Blocker 0**（SPEC §3 全部 AC 逐条满足，file:line 证据闭合）；**🟡 建议 2**（跨分支优先级测试 + 空 `.search_pid` case）**全部补上**；**🟢 可选 3**——补 Q11 单写点反证（trivial），另两项 🟢（畸形文件名 / AC-④2 setsid 模拟）surface conflict 跳过：

- **🟢 畸形文件名兜底测试**（跳过）：`search.attemptabc.stdout.log` 不可能产生——Step 2a 模板 `search.attempt${N}.stdout.log` 中 `${N}` 恒为正整数（bash `$((...))`），YAGNI。
- **🟢 AC-④2 改用 setsid 跑 mock `run_search_supernet.sh`**（跳过）：当前属性测试（filename 不冲突 → 不覆盖）已 pin 本质，reviewer 自评「复杂度收益不划算」，KISS。

---

## 偏差

无。3 Blocker（Q1 v1 不纳入 / Q2 两分支 N / Q5-Q6 rm 协议）+ 5 hardening（Q3 矩阵测试参数化 / Q4 内容判定 / Q7 核验范围 / Q10 best-effort / Q11 仅 unsupported 写）逐字落地，零偏移。

---

## 依赖铁律自检

- ③ marker 仅作 report 单向读（expand 写、report 读），非跨节点 output——不违 report 零跨节点 output 铁律。✓
- ④ 两分支 N 公式独立计算（latest-mtime / max+1），不引入跨分支状态依赖。✓
- ⑤ 核验脚本只读 scripts/py/agent.md，无副作用。✓
- 无 schema / cli / orchestrator / event 改动（与 S3 InputDef.enum / resume-WIP 完全正交）。✓

---

## 工作树隔离

S4b 只改 7 份 `workflows/agents/*/agent.md` + 2 份 `tests/workflows/test_v2_audit_*` 新增 + 3 份状态文档。**零重叠** with 工作树他人 resume-WIP（`orca/` 框架：cli.py / orchestrator.py / step.py / workflow.py / replay.py + 相关 tests + 2 skills 文件）。提交用显式 `git add` 列文件（不 `git add -A`），WIP 未污染。
