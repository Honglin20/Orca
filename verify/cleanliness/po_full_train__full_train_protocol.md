# 洁净审查记录 — po_full_train/references/full_train_protocol.md

- **审查对象**：`D:\Projects\Orca\workflows\agents\po_full_train\references\full_train_protocol.md`（175 行，全文通读）
- **审查方法**：受众翻转通读（审查者视角 = po_full_train 节点 agent，先读 `po_full_train/agent.md` 再按 Lazy Loading 读本协议）+ 词表 grep + spec §4 逐条核对
- **判据**：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（本文件被 agent.md L43/L62 指示 Step 1 必读并遵循 → prompt-adjacent prose，契约适用）+ `docs/specs/prof-opt-v4-spec.md` §4 L102/L83 + `docs/specs/prof-opt-v4-design-draft.md` D-V4-11（L36/L182）
- **日期**：2026-08-25

## ① 逐段结论表（受众翻转）

| 行号 | 段落 | 结论 | 说明 |
|---|---|---|---|
| L1-8 | 标题 + 范围声明 | 通过 | 路径基准（$ORCA_ARTIFACTS_DIR）、WINNER 定义、train-from-scratch 不变量、共用 full_train_budget——全是 agent 执行时要恪守的运行时约束，非设计论证 |
| L10-41 | State derivation（5 步） | 通过 | 每步独立可执行：best.json 缺失即 failed / contracts.json 四项读取 / 锚只读解析 + 三分支（done / 缺失-failed / 指纹不符均 fail loud）/ 由 pid+rc+ckpt 推 stage（in-flight 恒 poll 禁重launch）/ 写 train_status.md。L26「a verdict without an honestly-produced anchor is not a verdict」与 L29「never silently compared」为一句话意图注脚，帮助归因措辞，非历史考古，可保留 |
| L43-77 | Launch（唯一 detach 点） | 通过 | 模板路径 + required tokens 声明 + 完整 render 命令 + 公平性注（L65-67 指纹不变量）+ setsid detach（组长自写 pid 不 exec）。L76-78「winner 即当前全局 shadow（round-end advance 已替换）」是跨节点运行时事实，解释 shadow_dir 指向，operational 非残留 |
| L79-92 | Bounded polling | 通过 | 单条短命令 + RUNNING/DONE/DEAD 三分支处理；「turn tops out → status message with `do not call orca next`」与 agent.md 执行模型一致 |
| L94-105 | Retry path | 通过 | 白名单 healing（仅重渲染参数）+ 禁改范围外文件即 failed + 2 次重试预算；`.po_full_train_healed.txt` 被 agent.md Step 5 healed_files 字段消费，operational |
| L107-125 | Symmetric final check | 通过 | metric_curve.py extract --expected-epochs = full 生效值；不符 → status=failed + 归因到 symmetric final check + 引用准入条款——条款从运行时 `contracts.json` `reason` 字段引用（产品说明书式指路，非 spec 引用）。L109-112 对称性动机一句注脚，简短可保留 |
| L127-165 | Final evaluation | 通过（1 条 LOW finding，见 ②） | ckpt 解析规则、eval 渲染命令、final_acc.json 字段规格（含 baseline_full_acc_source 恒 "baseline"）、budget 判定、onnx 拷贝（引用不重测）。L156-160 内联 python3 -c 判定脚本见 F-1 |
| L167-174 | Idempotency notes | 通过 | 三条与常驻机制（pid 检查 / 禁二次 detach / 覆盖安全）一致收口 |

## 残留 grep（命中 = 0）

- **任务词表**（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft，大小写不敏感）：**0 命中**
- **增补检查**（baseline/full_train/、full_train/、cach*、epoch-only、补训、懒、ref_acc、proxy、second pid、第二 pid 键）：**0 命中**
- **v3.5 自动补训路径删除确认**：无 `baseline/full_train/` 路径、无 `baseline_ref.json`、无缓存 epochs 披露措辞；锚解析显式「read-only; the finalizer owns its production」（L18）——锚由 baseline finalizer 产、本节点永不补训，与 v4 语义一致

## ③ 契约一致性核对（spec §4 L102 + L83 + draft D-V4-11）

| spec 条目 | 协议实现 | 一致 |
|---|---|---|
| 删 baseline/full_train/ 路径与第二 pid 键 | grep 0 命中；唯一 pid 键 = `final/.train_pid`（L36、L72） | ✓ |
| 锚 = baseline_full_acc.json + 指纹逐字段校验 + 防御性 train_final=done 检查 | L19-23：train_final.json status:done **AND** baseline_full_acc.json 存在 **AND** 其 full_train_budget 与 contracts.json **field-for-field** 相等 → 锚 resolved | ✓ |
| 锚缺失/failed/指纹不符 → status=failed 归因 | L24-29 两分支均 fail loud 且 cause 具名（missing/failed baseline terminal state / stale anchor） | ✓ |
| winner 同模板 `--out final/train.rendered.sh` + full_train_budget 同指纹 | L52-57 `--out $ORCA_ARTIFACTS_DIR/final/train.rendered.sh`；模板 = `templates/run_full_finetune.template.sh`，与 po_baseline 链（run_baseline_chain.sh:281）、po_probe 同一文件（po_contract check_contracts.sh 断言单一模板）；L65-67 渲染值 = 基线链/全部变体同一 full_train_budget | ✓ |
| winner 训练终了对称终检实跑 == full（不符 → status=failed 归因） | L114-125：extract --expected-epochs = full_train_budget.epochs；非零（count mismatch / unparsable）→ status=failed，归因 symmetric final check + 引准入条款 | ✓ |
| baseline_full_acc_source 恒 "baseline"（failed null；enum `["baseline", null]`） | 协议只在锚 resolved 后写 final_acc.json 且 source 恒 "baseline"（L23、L151）；failed 路径不写 final_acc.json，null 由 agent.md Step 5 failed emit（L151）承接 | ✓ |
| 常驻机制继承（pid-检查-禁二次-detach） | L33-39 stage 推导含 pid/rc 检查；L36-37「training in flight: poll (never re-launch)」；L167-174 idempotency notes | ✓ |

## ② Findings

- **F-1（LOW / borderline，非残留类）** `full_train_protocol.md:154-160` — budget 判定以内联 `python3 -c` 多行片段呈现（`ok = f >= b - budget if d == 'higher_better' else ...`，含分支逻辑）。契约 §4「确定性代码内联」点名模式 =「多行 bash / `python3 -c` 的循环·**分支**·assert 逻辑应抽 `scripts/`」；允许清单是「单行 operational 命令（jq / ruff / **python \<file\>**）」。本片段是单次判定调用、无循环/assert，但按字面命中"python3 -c + 分支 + 多行"。**建议**：沉到 `_po_scripts`（如 budget 判定脚本，实参传 f/b/direction/budget——与 gate_decide.py 同范式），协议只留一行调用；或显式 waive（理由：单条判定、输入占位符需 agent 代入，透明度优先）。注意 spec §4 po_full_train 行未强制此脚本化，故**非 spec 不一致**，仅契约风格项。
- 其余检查显式**零 finding**：词表 0 命中；v3.5 自动补训路径删净；spec §4 六项逐条一致；无开发期残留（plan/issue/SPEC 编号、迁移出处、Orca 源码路径、测试项目名、事故复盘叙事均无）。

**零严重度观察（不计 finding，仅备案）**：a) L146 全预算 eval 的 detach+poll 未像训练步那样给显式命令与 pid/rc 文件名（"pid/rc pair under `final/`"）——按类比可独立执行，state derivation 只键于 .train_pid/.train_rc，无碰撞风险；b) L46-47「some templates also declare `vid`」而 render 命令无条件 --set vid/shadow_dir 等——与 po_probe 协议同款约定，若 render_run.sh 对未声明 token 的额外 --set 报错需上游兜底，非本文件洁净问题。

---

VERDICT: PASS（词表残留 0、v3.5 补训路径删净、spec §4 六项一致；1 条 LOW borderline finding F-1 属契约 §4 确定性代码内联风格项，建议 sink-to-script 或显式 waive，不阻断）
