# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle workflow —— 双项目脚本级 E2E PASS，in-session 部分验证

**任务**：实现 puzzle workflow（Bercovich 2025 decomposed NAS）并在 playground 两项目(mnist_trf / target)上 E2E 通过(ACC≤0.5 / LAT≥2×),in-session 模式可执行。

### ✅ 已完成

- **workflow 完整实现**(`workflows/puzzle.yaml` + 6 agent `pz_*` + 4 terminate + 9 算法脚本 `_puzzle_scripts/` + 3 verifier 体 + checklist)。对齐 nas-supernet v1 in-session 契约。`tars validate` 0/0;19 单测过;code-reviewer 闭环。
- **mnist_trf 脚本级 E2E PASS 双 AC**:acc 0.927→**0.958**(Δ0.031≤0.5)、latency 0.928→**0.453ms**(降 51%)。fixture 在 `/mnt/d/Projects/playground/mnist_trf/`(d=96/4-block transformer,分类)。
- **target 脚本级 E2E PASS 双 AC(workflow 自适应,target 源码零改)**:coder 产 wrapper adapter `target/artifacts/puzzle/target_flat.py`(4 输入打包单 tensor,state_dict 与 pre_trained.pth 零 missing);acc 0.085→**0.09**(Δ0.005≤0.5)、latency 0.448→**0.197ms**(降 56%)。eval_kind=embedding(cosine 打分/GKD)。
- **in-session 机制验证**:bootstrap(chart daemon + web UI)→ pz_expand 子代理执行 emit JSON → `orca next` 接受 + 路由 pz_build_library → 闭环跑通。
- **关键修复**:① 预训练 father 贯穿(load_father_model);② latency 模型(standalone 单块 + 实测 floor);③ attention no_op(_ZeroBlock);④ GKD 真实训练数据 + CE/cosine;⑤ 100 reps 稳定测量;⑥ `_KwargPassthrough` 适配异构 forward 签名(target attention_mask)。
- **commits**(`in-session-unified-backend`):`3a657d7`(workflow+mnist) / `6834dec`(KwargPassthrough+target) / `fcc4a12`+`95201e1`+`70fa724`(docs)。

### ⏳ 未完成

- [ ] **mnist_trf in-session 全驱动**:仅驱动到 pz_expand→pz_build_library 路由(机制已证);BLD/GKD 长跑节点跨轮 bounded-polling 未驱动(token 重),且 mnist block_library 是 wrapping 前旧产物需重建。
- [ ] **pz_expand 自适应重构(用户愿景,未实现)**:目前 target 用手写 adapter;真正 LLM-flatten(镜像 ns_expand Step 1:读项目→产 self-contained flat + manifest[eval 入口/ckpt/train/data/paradigm]→ fidelity smoke[load 预训练 ckpt 进 flat 验证]+ eval smoke + 不便跑派 verifier)未做。设计已落 `docs/specs/puzzle-design-draft.md` §9-10。
- [ ] **输入契约对齐 nas-supernet(未做)**:现仍要求 build_fn/eval_fn/eval_kind/train_loader_fn/pretrained_ckpt 为 [ask];应收缩到 project_root/model_path/target_latency/latency_unit/latency_script_path/accuracy_tolerance/seed,其余 agent 发现写 manifest(design-draft §9)。
- [ ] mnist_trf fixture + target adapter **纳入 orca 仓**(tests/e2e_puzzle/fixtures/)保可复现(现仅在 playground/ 仓外)。
- [ ] 用户确认后 commit 余下(global 规则:未问不动——已 commit 的不含上述未完成项)。

**必读**：SPEC `docs/specs/phase-puzzle-impl.md`；设计草稿 `docs/specs/puzzle-design-draft.md`(§5.2.2 自适应路径 / §9 输入对齐 / §10 pz_expand 重构)；`workflows/puzzle.yaml`；fixture `/mnt/d/Projects/playground/mnist_trf/` + `/mnt/d/Projects/playground/target/artifacts/puzzle/target_flat.py`。

---

## 遗留（nas-supernet，跨任务未决）

- [ ] 真机 E2E(in-session headless `latency_unit: us` + 用户 script → 4 图 label=us / compare 真测量 / `subnet_structure.md` / A6 fail-loud)——属 test-agent 范围。
- ℹ️ v3 P0 已修（`0ca1b3b`）；Task 2 enum 已提交（`7b120ee`+`131b294`）；v2 P1/P1（`a57190b`）；S4a（`d768879`）；S4b SDD 三项已提交。详见各 release note + CHANGELOG。

---

> 历史任务记录见 `CHANGELOG.md`（索引）+ 各 `docs/releases/*` release note。本文件仅保留当前任务快照。
