# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

**当前任务**：profiling-v2（三面融合：结构/特征/loss）——SPEC + 实现完成，**E2E 待有卡宿主**

- commit `5944b88`（83 文件）；SPEC `docs/specs/profiling-v2-spec.md`；release note `docs/releases/2026-09-11-profiling-v2-tri-facet.md`
- 流程留痕：SPEC（spec-reviewer 两轮对抗 + 闭环复核全闭环）→ 用户拍板跳过计划环节 → 双 coder-agent 并行（脚本层 3 轮内审 / 提示词层 2 轮内审）→ 编排者收三处跨层接缝（eval.sample_inputs/facet_builder 写入方、SPEC 同步、R4-R5 支配）
- 验证：test_pv2_* 81 green（onnx/torch 真跑）+ po 回归 146 green 零恶化（混序双验）+ tars validate 0 error 0 warning + 提示词开发残留 grep 零命中
- 核心语义：门不变（推理时延严格降 + gap≤预算）；facet 副本住 shadow 外（`facets/` + `variants/<vid>/facets/`，锁/枚举/断言/diff 零改动）；facet 能力 po_contract 机械证明（dry-run），不可用静默收窄但 contracts.json + report 披露；蒸馏禁止（distillation_free_ack）；history.md = 机械派生视图（curator/digest_stamp 退役）
- **E2E 待有卡宿主**：同 v8 坑（本机 WSL 无 NPU/CUDA，flatten fail loud 属合规）；建议 inputs 与 v8 E2E 同款（`docs/status/CHANGELOG.md` 2026-09-10 条），workflow 名换 profiling-v2

**v8 遗留（prof-opt，非阻塞）**：轮环 E2E 待有卡宿主（run `prof-opt-20260910-130812-1af1ea` flatten 即 fail loud 属合规；启动脚本 `.e2e_scratch/e2e_v8_launch.sh`，WSL 原生 claude `~/miniconda3/bin/claude`）

**挂账小项（非阻塞，下次顺手）**：
- profiling-v2：`check_report.py` 披露锚 token 三枚 vs 披露四行——facet 能力行未锚，未来加锚必须同步 po_report/agent.md "three anchor tokens"（跨层耦合，防单边漂移）
- profiling-v2 E2E 后回收两处无害超集裁决：render in_flight 块带时延段 / watch_variant 供给条件 features∨loss
- **in-session inline script 推 chart 自死锁**（跨平台既有）：架构级专项待立项
- **CC hooks Windows 静默死**：需 python 化 + `sys.executable` 绝对路径注册
- 三份 msvcrt 锁实现归并；Windows ScriptNode `$VAR` 不展开；前端 static 无强制重建机制（2026-09-11 已手动重建）
- `test_gate_node_sh_parses_after_quote_fix` 仍挂（HEAD 既有，v2 环境实测现绿）
- **测试卫生债**：部分 tests/iface/web 套件不隔离 ORCA_HOME
- 仓库根 `_e2e_playground` MSYS 破损 symlink 阻断 Windows 全量 pytest
