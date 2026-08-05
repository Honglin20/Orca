# Release: kd-nas LLM 语义 fidelity 审计 + ID 化收敛环

**Date**: 2026-08-05
**SPEC**: [`docs/specs/2026-08-05-kd-nas-fidelity-audit-spec.md`](../specs/2026-08-05-kd-nas-fidelity-audit-spec.md)
**Scope**: kd-nas workflow 的训练脚本生成/校验阶段（`gen_train_script` /
`train_script_verify`）。

## What was done

补 L3 确定性层之上的**语义静态比对审计**（B1）+ **ID 化 Resumed Re-Check
收敛环**（B2），抓 L3 实证盲区（helper 体外 look-alike 替换 / data transform
内容差异 / optim kwargs 漂移 / 控制流重排）。

### 改动

1. **新建 `workflows/subagents/kd-nas/project-fidelity-verifier-kd.md`**（B1，
   独立副本，O2 决定）：
   - frontmatter `subagent: project-fidelity-verifier-kd` / `version: 1` /
     `sentinel: KDPFV01`（独立 sentinel，不复用 nas-supernet 版 `PF8LK3`）。
   - KD 化改编（N6）：删 RL environment fidelity / auxiliary networks /
     reward formula / rollout / discriminators / subnet sampling 段；
     Out-of-scope 换成「KD 引擎（`_kd_scripts/train_pipeline.py` + `kd/`）+
     student 变体契约（`build_model` / `DUMMY_INPUT` / `KNOBS`）不审」。
   - Intended behavior KD 专属（叶子逐字搬运 / distillation loss 在引擎侧 /
     kind 方向已由 L3 校验）。
   - Audit procedure 加「展开 module-level helper 比对 / transform 内容 /
     optim kwargs」补 L3 盲区。
   - Output 加 STATUS 契约（`STATUS: closed|open|accepted` 机械可解析）+
     Runtime Fidelity `not verified` KD 预期说明（O4）。

2. **改 `kd-train-script/agent.md` + `SKILL.md` Step 4**：在 L3 与 L4-mechanical
   之间插 **L4-semantic 收敛环**。层命名 L4-semantic / L4-mechanical（M2）。
   SKILL.md 加 `## L4-semantic — project-fidelity-verifier spawn` 段（first-run
   + resume 两模板，经 `{{ subagents_root }}/project-fidelity-verifier-kd.md`
   point-to-file）。循环逻辑：MAX_TURNS=3（O3）、verifier 崩 fail loud +
   ask-user、ID 范围防御、STATUS 机械解析、reaffirm≥2 → ask-user、Unresolved →
   ask-user、apply fixes 后重跑 L1+L3、fix 改坏 L1/L3 → ask-user。

3. **改 `train-script-verify/agent.md`**：加 step 3.5（一次性 spawn
   fidelity-verifier，**report-only**——D1 决定）；step 4 workflow-verifier
   spawn prompt 也加 `do not modify artifacts; report only` + 显式带 Accepted IDs
   （N8）。spawn 崩 / Unresolved / Static Fidelity 非 pass → verified=false 退非零。

4. **改 `kd-nas.yaml`**：两节点 description 注释同步说明 fidelity-verifier 层；
   output_schema **不变**（fidelity-verifier 是过程不进 output）。

5. **新建 `examples/mnist_kd_adversarial/`**（D3）：复制 `mnist_kd`，仅在
   `user/optim.py::build_optimizer` 注入一处「同类名 `Adam` 但 `weight_decay=1e-3`
   vs 用户原 `0`」的偏差——L3 `OPT_TYPE_OK` 只比类名 → PASS；B1 比 kwargs →
   命中。README 说明这是 adversarial 测试 fixture。

## Deviations from plan

无（按 SPEC §3 / §4 / §5 / §6 逐字实现）。

**一处 SPEC 内部不一致的处理**（非偏离 SPEC，是 SPEC prose typo 的强制修复）：
SPEC §3.1 line 54 / 106 写文件路径 `project-fidelity-verifier.md`，但 line 60
写 frontmatter `subagent: project-fidelity-verifier-kd`。validator.py:1139 铁律
`fm["subagent"] == md.stem` 不允许这种不一致。处理 = 文件命名为
`project-fidelity-verifier-kd.md`（与 frontmatter 一致、过 validator），
agent.md / SKILL.md / train-script-verify/agent.md 所有 spawn 引用统一用
`{{ subagents_root }}/project-fidelity-verifier-kd.md`。
SPEC line 54 / 106 是 prose typo，下次 SPEC 修订应同步修。

## Code review

`code-reviewer` 一轮闭环：0 must-fix / 3 should-fix（已全部采纳）：
1. SKILL.md 伪代码缺 `id_stash.update(...)` 显式步骤 → 已加。
2. `fixed_ids` 覆写语义注释 → 已加。
3. `kd-nas.yaml:208` 注释「补 L3 盲区」气味词 → 改为「覆盖 L3 不展开 helper /
   不比 transform 内容 / 不比 optim kwargs 的语义层」。

## Verification

- `tars validate workflows/kd-nas.yaml`：0 errors / 0 warnings（含
  `_check_subagents_md` strict frontmatter regex + `_check_prompt_dev_residue`
  扩扫子 agent md）。
- `_check_subagents_md` frontmatter 三键 (`subagent`/`version`/`sentinel`)
  strict regex PASS；`fm["subagent"] == md.stem` PASS（`-kd` 后缀双匹配）；
  `{{ subagents_root }}` 引用节点 tools 含 `read` PASS。
- 洁净契约（SPEC §6）：所有改动 .md body 受众翻转通读，0 SPEC/plan/issue
  编号、0 Orca 源码路径、0 `mnist_kd` 硬编码（adversarial fixture README 除外，
  那是测试 fixture 自描述）、0 过程推理气味词（`借鉴` / `补 L3 盲区` /
  `D1 决定` 等）。

## Remaining work (next independent step, not in this commit)

SPEC §4 验收 A7–A9 的真机 E2E 三条路径（收敛环 / Unresolved→ask-user /
reaffirm 防呆），使用 D3 fixture 跑生成节点 L4-semantic 循环验证。本 commit
仅落地静态契约 + adversarial fixture，真机 E2E 是独立的下一步。

## Commit

见 git log（本次实现单一 commit）。
