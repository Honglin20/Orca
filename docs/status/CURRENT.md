# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle workflow —— mnist_trf 脚本级 E2E PASS，待 in-session + target

**任务**：实现 puzzle workflow（Bercovich 2025 decomposed NAS）并在 playground 两项目(mnist_trf / target)上 E2E 通过(ACC≤0.5 / LAT≥2×)。

**状态**：**mnist_trf 脚本级全链 PASS 双 AC;workflow orca bootstrap 通过;target 待适配**。

### mnist_trf 脚本级 E2E(已 PASS 2026-08-12)
- baseline: acc=0.927, latency=0.928ms(d=96/4-block transformer, 455K params)
- Puzzle 后: **acc=0.958**(Δ=0.031≤0.5 ✓)、**latency=0.453ms**(ratio=0.488≤0.5 ✓)
- gate_status=pass / both-met。全链 expand→bld(48 variant)→score→latency→mip→build→gkd(4ep/1876step 真实数据)→gate

### 让 AC 可达的关键修复(集成期发现 + 修)
1. **预训练 father 贯穿**:expand load_state_dict + 存 father_state_dict.pt;bld/score/build/gkd/latency 用 load_father_model(原全链随机 init)
2. **latency 模型**:standalone 单块(加性)+ **实测 floor**(全 block→_ZeroBlock 整模 latency, latency_floor.json);mip: selected = floor + Σ chosen_block ≤ target
3. **attention no_op**(_ZeroBlock 零输出真删块)+ 默认候选加入
4. **GKD 真实训练数据**(--train_loader_fn)+ CE hard-label(原合成 calib batch=2 只 6 step)
5. **100 reps 稳定测量**(expand/latency/gate)
6. workflow 加 inputs: pretrained_ckpt / train_loader_fn;agent.md 透传 --father_state / --train_loader_fn

### orca in-session bootstrap(通过)
`orca puzzle --inputs '{...}'` 解析+启动 run+chart daemon+web UI+entry=pz_expand 全 OK(test run 已 stop 清理)。

### 待办
- [ ] mnist_trf **in-session 全驱动**(orca next 逐节点,验 agent.md 编排;脚本级已证 AC)
- [ ] **target 适配**:multi-input 模型(4 输入,dummy/latency/forward 需扩多输入)+ InfoNCE cosine 打分/GKD + model.py 补 build_model/DUMMY_INPUT/eval_model_acc(model)->float
- [ ] code-reviewer delta 终审(进行中)+ 修反馈
- [ ] 用户确认后 commit(global 规则)

**必读**：SPEC `docs/specs/phase-puzzle-impl.md`；设计草稿 `docs/specs/puzzle-design-draft.md`；`workflows/puzzle.yaml`；fixture `/mnt/d/Projects/playground/mnist_trf/`。

---

## 遗留（nas-supernet，跨任务未决）

- [ ] 真机 E2E(in-session headless `latency_unit: us` + 用户 script → 4 图 label=us / compare 真测量 / `subnet_structure.md` / A6 fail-loud)——属 test-agent 范围。
- ℹ️ v3 P0 已修（`0ca1b3b`）；Task 2 enum 已提交（`7b120ee`+`131b294`）；v2 P1/P1（`a57190b`）；S4a（`d768879`）；S4b SDD 三项已提交。详见各 release note + CHANGELOG。

---

> 历史任务记录见 `CHANGELOG.md`（索引）+ 各 `docs/releases/*` release note。本文件仅保留当前任务快照。
