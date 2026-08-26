# Release: nas-supernet progress.jsonl 契约校验闸门

> 2026-08-11。补 nas-supernet / nas-supernet-v2 的 supernet 训练/retrain 缺口:
> 生成的训练脚本若漏写 progress.jsonl(或格式错),训练照常 `executed` 但没有实时 web 图,
> 且全程不 fail loud。本次加**确定性契约闸门**(fail-loud + 接现有 HEAL-LOOP 自愈)
> + **静态早期防线**(生成期就挡,省一次 detach)。

## 问题(Context)

两个 workflow 的 supernet 训练/retrain 靠 `progress_watcher.py` tail `progress.jsonl`
的 `{"step":N,"metrics":{...}}` 行,经 chart daemon 推成实时多指标曲线。但「生成脚本必须
按 §3(b) 写 progress.jsonl」这条契约**没有任何强校验**:

- **生成侧**:smoke test 只测 component(不跑训练循环,不触写 progress.jsonl 的代码路径);
  37 条 checklist 没有 progress.jsonl 写入这条(item 10 只覆盖 tqdm/print,item 6 只查
  rank-gate 泛指)。
- **运行侧**:`warmup_poll.sh` 判 `WARMUP_OK` 只看 **log 里的 telemetry 行**(`EPOCH_CNT>=2`),
  **不看 progress.jsonl**;`progress_watcher.py` 是 fail-soft(缺文件/格式错静默 exit 0);
  progress.jsonl 不进训练完成判定(`status.sh` 只看 rc+进程+ckpt)。

后果:**生成的 train/retrain 脚本若漏写 progress.jsonl,训练 executed、下游照走、ckpt 有效,
唯独没有实时图,且全程不 fail loud**。已观察到 v1 真机 run 的 artifacts 下无 charts/ 目录,
与该缺口表现一致。

## 改动

### A. 运行时闸门(MUST,4 份镜像)

**A1. 新增 `check_progress_contract.py`**(train+retrain × v1+v2 共 4 份,字节相同):
参数化 `--progress <path>`,校验文件存在+非空+每行 `json.loads`+`step` 是 int+`metrics` 是
dict+每个 value 是 number(与 `progress_watcher._is_number` 同源,排除 bool;NaN/inf 当 number
接受——发散由 warmup 发散段单独管)。**fail-loud**:任一非法路径 `exit 1` + stderr 打印
行号+缺哪个键/值类型。与 progress_watcher 的根本区别:watcher fail-soft(绝不影响训练 rc),
本脚本是契约闸门(漏写是生成代码 bug,必须 fail loud 触发自愈)。

**A2. `warmup_poll.sh` 注入 + 收紧 WARMUP_OK**(4 份):在发散检查后、WARMUP_OK 判定(EPOCH_CNT>=2)
前调 `check_progress_contract.py --progress "$PROGRESS"`;收紧 `WARMUP_OK = telemetry≥2 AND 契约过`。
契约不过 → `WARMUP_FAIL reason=progress-contract`(+ log 尾部)→ agent Step 3b 进 HEAL-LOOP
→ edit `train_supernet.py`/`retrain.py` 补写入循环(白名单训练逻辑层,重触 fidelity)→ 重跑。
**零新机制,复用现有 HEAL-LOOP**。`EPOCH_CNT<2` 时不校验(走 WARMUP_RUNNING,给首 unit 慢的
训练留 grace)。

时序边界:契约 §3(b) 要求**每 progress unit 写一行**,故 telemetry≥2 时 JSONL 必≥1 行;
check 只要求「≥1 合法契约行」作最小闸门,不强制行数与 telemetry 精确匹配(eval/train metric
交错,行数关系不固定,过严会误杀)。

### B. 静态早期防线(SHOULD)— 生成期就挡,省一次 detach

v1/v2 静态机制不同,落地分两套:

- **B1. v2 双侧(扩展 check_*.sh 确定性 gate 族)**:
  `ns2_train_script/scripts/check_train_script.sh` + `ns2_retrain/scripts/check_retrain.sh`
  各加一段「Progress JSONL write contract」(grep 生成脚本含 `progress.jsonl` + `json.dumps`)。
  生成期 ns2_train_script/ns2_retrain agent 自调(确定性 gate),漏写直接 FAIL,不进 detach。
- **B2. v1 train + v2 双侧 checklist(companion checklist,workflow-verifier 活契约)**:
  `ns_train_script` + `ns2_train_script` 的 `references/workflow-checklists/train_supernet_script_generation.md`
  各加 `[CRITICAL] 38. Progress JSONL Write Loop` item。workflow-verifier subagent 自动发现
  companion checklist 并逐条查(`workflow-verifier.md` step1→step3),真执行——不是死文档。
- **B3. v1 retrain(静态机制缺位的务实兜底)**:v1 无 check_*.sh 族、retrain 也无 companion
  checklist+verifier loop。在 `ns_retrain/agent.md` Step 3a(inline 契约段)加一句生成后 grep 自查
  bullet(零机制成本)。**硬保障仍是 A 的运行时闸门**——v1 retrain 静态弱可接受,运行时 fail loud
  已堵漏写。不为 v1 retrain 新建 verifier loop(成本不匹配收益)。

### C. 测试

新增 `tests/workflows/test_check_progress_contract.py`(沿用 `test_monitor_until_done.py`
范式:_REPO 路径常量 + class 组织 + parametrize 镜像 + subprocess 端到端):
- `TestContractValidation`:11 个 case(合法/缺文件/空/非JSON/缺step/metrics非dict/metrics空/
  bool/string/NaN-inf接受/4份镜像行为一致)。
- `TestWarmupGate`:4 份 warmup 含 check 调用 + 用 `$PROGRESS` 变量 + `bash -n` 过。
- `TestMirrorSync`:4 份 check_progress_contract.py 字节相同。
- `TestStaticChecks`:v2 check_*.sh 含 progress.jsonl 段 + `bash -n` 过;v1/v2 checklist 含 item 38。

## 验证

- **单测**:`pytest tests/workflows/test_check_progress_contract.py` → **34 passed**(wsl python3.14,
  `--noconftest` 绕开本地缺 pydantic 的根 conftest;含 review 后补的 float-step/list/nested 边界 + warmup 行序锁)。
- **回归**:`pytest tests/workflows/test_progress_watcher.py tests/workflows/test_monitor_until_done.py`
  → **39 passed**(未破坏现有镜像测试)。
- **自我 Review 闭环**(code-reviewer):0 MUST-FIX。2 SHOULD-FIX 全修:① `ns_retrain/agent.md` "v1 retrain"
  版本分类残留→"本节点无 check_*.sh 兜底"(prompt 洁净);② step `int`/`number` 契约-实现不一致→采纳
  放宽 `<number>`,对齐 `progress_watcher._is_number` 事实标准(避免"check 拒绝 float step 但 watcher 接受"分裂)。
  采纳 NIT #1(补 float-step/list/nested 边界测试)/#2(warmup 行序 gate<check<OK 锁)/#4(docstring 泛指 retrain);
  set -e 陷阱经实证证伪(`A && B` 左操作数排除在 set -e 外,与既有 `check_train_script.sh:36` 约定一致)。
- **bash -n**:6 个改动 .sh(4 warmup + 2 check_*.sh)全过。
- **环境依赖项(本地未跑,待 orca 环境)**:
  - `tars validate workflows/nas-supernet.yaml nas-supernet-v2.yaml`:本地 wsl 无 orca 依赖(pydantic
    缺)。本次不碰 yaml 结构(只改 agent.md/scripts/checklist),tars validate 非必须但建议在 orca 环境跑一次。
  - **真机 E2E**(playground `D:\Projects\playground\mnist_kd`,in-session headless):故意删掉
    train_supernet.py 的 progress.jsonl 写入循环 → 确认 warmup 报 `WARMUP_FAIL reason=progress-contract`
    → HEAL-LOOP 修回 → 实时图恢复;正常 run 确认 `WARMUP_OK` 不误杀;v2 确认 `check_train_script.sh`
    生成期静态挡。需 test-agent 在真机环境执行。

## 文件清单

| 文件 | 改动 |
|---|---|
| `workflows/agents/{ns_run_train,ns2_run_train,ns_retrain,ns2_retrain}/scripts/check_progress_contract.py` | 新增(4 份字节同) |
| 同上 4 目录 `scripts/warmup_poll.sh` | 注入 check 调用 + 收紧 WARMUP_OK + 头注释 |
| `workflows/agents/ns2_train_script/scripts/check_train_script.sh` | +静态 progress.jsonl 段 |
| `workflows/agents/ns2_retrain/scripts/check_retrain.sh` | +静态 progress.jsonl 段 |
| `workflows/agents/{ns_train_script,ns2_train_script}/references/workflow-checklists/train_supernet_script_generation.md` | +[CRITICAL] item 38 |
| `workflows/agents/ns_retrain/agent.md` | Step 3a +inline 自查 bullet |
| `tests/workflows/test_check_progress_contract.py` | 新增(30 测试) |
