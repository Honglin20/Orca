# 洁净审查记录 — po_probe/references/probe_protocol.md

- **审查对象**：`D:\Projects\Orca\workflows\agents\po_probe\references\probe_protocol.md`（256 行，全文通读）
- **审查方法**：受众翻转通读（审查者视角 = po_probe 节点 agent，按 agent.md L41-42/L60 的 Lazy Loading 指引在 Step 1 全读本协议）+ 词表 grep + spec §4 逐条核对 + 交叉实证（stop_at_epoch.sh / metric_curve.py / history_lib.py / advance_round.py / render_run.sh / run_baseline_chain.sh / po_contract+po_full_train 协议同族对照）
- **判据**：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（本文件被 agent.md L42「read it at Step 1」+ L60 指示必读遵循 → prompt-adjacent prose，契约适用）+ `docs/specs/prof-opt-v4-spec.md` §4 L100（po_probe 行）+ `docs/specs/prof-opt-v4-design-draft.md` D-V4-2/D-V4-5/D-V4-18（L25/L30/L43/L111-122/L127/L142）
- **日期**：2026-08-25

## ① 逐段结论表（受众翻转）

| 行号 | 段落 | 结论 | 说明 |
|---|---|---|---|
| L1-9 | 标题 + 记法 | 通过 | workspace 基准（$ORCA_ARTIFACTS_DIR）、RRR 定义、占位符锚定 node prompt input anchors（`<project-root>`/`<accuracy-budget>` 与 agent.md L43/L72 的 `{{ inputs.* }}` 一致）——纯运行时记法 |
| L11-25 | Gate 参数 | 通过 | k = `proxy_budget.epochs`（D-V4-5）、E = `full_train_budget.epochs`、promote line = baseline_curve_at_k − 1.0×budget（1.0 常数与 agent.md L44-45 一致）、eval gate 键 `baseline/baseline_k_acc.json`；方向归一化 + 「comparison 一律 python 读 JSON、禁心算」 |
| L27-32 | Fairness invariant | 通过 | 同模板同 full 生效轮数同数据同 seed、变体仅差结构 + 被外部停在第 k 轮、「Never render a smaller epoch count, never tune a data/step cap」——**v3.5 proxy 短训渲染语义已彻底替换为 stop-at-k**（渲染恒 full，停止永远来自外部 kill） |
| L34-46 | GPU serial guard | 通过 | 守卫目标 = `baseline/finalizer.pid`（生存期 ⊇ 训练，理由一句话讲清）；四象限表逐格可执行：活 → bounded-wait（单调用 ≤480s + status message `do not call orca next`；**双期 30min 停滞**：训练活期 `train.attempt<N>.log` mtime + 曲线点数均冻结、finalizer 期 `finalizer.log` 冻结）/ 死+done 放行 / 死+failed error 路由（assessment 具名 `train_final.stage`）/ 死+缺失 fail loud（具名 `baseline/finalizer.log`）。`train.attempt<N>.log` 命名与 run_baseline_chain.sh:294 实证一致 |
| L48-68 | State derivation | **P-1**（L60 路径，见 ②） | 其余完备：guard 先行 / R = rounds/ 最大数字目录 / survivors = 最新行 round==R 且 latency_pass / 终态跳过 / proxy.json 先 reconcile / in-flight 恒 poll 禁重launch（L63-64 即 spec 要求的「stop_status 未出且组活 → 继续调」分支）/ probe_status.md + heal ledger 入口截断 |
| L70-76 | Reconciliation | 通过 | 「result files 是 payload、history 是 ledger、reconcile 后必须一致」——幂等语义一句话钉死，可机械执行 |
| L78-107 | Stop-at-k train（render + detach） | 通过（advisory A-1） | 模板 = `templates/run_full_finetune.template.sh`（po_contract/agent.md:333「the ONE training pipeline」，与 po_baseline 链、po_full_train 协议同一文件）；render 全键供齐——render_run.sh:77-78 **硬必填** shadow_dir/shadow_pkgs，L96-97 均已 `--set`；`--set epochs=<full_train_budget.epochs>`（full 生效值）；detach 组长自写 pid 不 exec、pid/rc 各有写者（与 spec po_baseline 行同款 wrapper 范式） |
| L108-145 | Bounded-poll + retry | 通过 | 间隔 ≤30s、单调用一次幂等检查、反复调至终态；waiting/killed/natural_done 三态及字段（stopped_at_epoch = 实际解析深度、rc、monitor_failed = N>k、stop_status 优先于 rc）与 stop_at_epoch.sh L16-37 头注释**逐字吻合**；重试预算 2 次 → 终态 probe_insufficient（proxy_acc=null、promote_gate=fail）+ max_retries_hit=true；heal 白名单 = 仅重渲染参数 + 只 wipe out-dir 下部分 ckpt 产物、控制文件必留；等待期 push_curves best-effort never fatal（`\|\| true`） |
| L146-155 | Curve extraction | 通过 | `--expected-epochs` = stop_status.json 的 `stopped_at_epoch`（**非假设 k**）；缺/重/断代 = hard failure，禁以末 eval 值顶替曲线——与 spec「extract --expected-epochs = stopped_at_epoch」一致 |
| L156-169 | Pinned compare | 通过 | compare 恒 `--at-epoch <k>` vs `baseline/baseline_metrics.jsonl`；任一曲缺 epoch k → fail loud 禁换深度；epoch_compare.json 落盘且 `at_epoch` 必须 == k、`baseline_path` 记锚——与 metric_curve.py:91-125 实现吻合 |
| L170-185 | k-ckpt eval + 降级矩阵 | 通过 | 可寻址 → 第 k ckpt（`train.ckpt_output_rule` 写序第 k 个匹配，v4 新字段）eval vs baseline_k_acc；**eval 加载失败 → 重派 1 次 → 仍败降级曲线单判**（eval_failed:true + eval_acc:null + history/assessment 双披露）；不可寻址 → 曲线单判 + `eval_skipped_no_epoch_ckpt: true`（披露、非错误）——spec 两条路径逐字落位 |
| L186-201 | Promote check | 通过（P-2 LOW） | 双门（eval 跑过时 BOTH、否则曲线单判）；curve_ok = `c['pass']`（权威判定取脚本产物）；ev/ba 为 None 时 eval_ok=True 的缺席语义与降级/不可寻址路径严格一致；outcome 二值映射清晰 |
| L203-233 | History row + results line | 通过 | `append_probe` kwargs 与 history_lib.py:161-166 签名**逐参吻合**（含四个可选注记字段）；`proxy_acc` 恒曲线@k（「always the curve value, never the eval value」，与 history_lib docstring/D-V4-18 同句）；results line 仅 fresh outcome 追加（先查 vid 防重） |
| L235-249 | Round-end advance | 通过 | `.round_advanced` marker 判据 + `advanced:false` no-op 语义 + `best_updated` 与 advance_round.py:37/99-101/151 实证一致；非零退出 → status=failed + 计数字段全 0 + assessment 承载 cause（与 agent.md Step 4 failed emit 承接） |
| L251-256 | Assessment marker | 通过 | `.po_probe_assessment.txt` 一行人类摘要喂 assessment 输出字段——operational，无残留 |

## 残留 grep（命中 = 0）

- **任务词表**（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft，大小写不敏感）：**0 命中**
- **增补检查**（v3 / v4 版本串、run id、issue/plan/SPEC 编号、迁移出处词、Orca 源码路径、内部 examples 路径、测试项目名、事故复盘叙事、懒补训措辞）：**0 命中**
- **v3.5 已删机制残留确认**：无任何「渲染 epochs=k 的 proxy 短训」路径（渲染恒 full 生效值 + 外部 stop）；无 proxy 短训锚（比较锚 = 基线**完整**曲线@k + baseline_k_acc，D-V4 草稿 L142 语义）；无 baseline_ref / run_verify / playbook / ref-input / auto-trained / 懒补训字样。**stop-at-k 语义替换彻底**

## ③ 契约一致性核对（spec §4 L100 po_probe 行，逐条）

| spec 条目 | 协议落点 | 一致 |
|---|---|---|
| GPU 串行守卫四象限（探测目标 = finalizer.pid 生存期 ⊇ 训练；活 → bounded-wait 单调用 ≤480s + status 续驱 + 双期 30min 停滞；死+done 放行；死+failed error；死+缺失 fail loud） | L34-46 四象限表逐格对应；停滞判据训练活期 = train.attempt<N>.log mtime + 曲线点数双冻结 ≥30min、finalizer 期 = finalizer.log 冻结 ≥30min | ✓ |
| 渲染同模板 + `--set epochs=<full 生效值>` + shadow_dir → 变体影子 | L88-100：epochs=full_train_budget.epochs、shadow_dir=variants/<VID>/shadow、shadow_pkgs 供齐（render_run.sh 硬必填）——`--out` 路径与 spec 行文差一层 `/train`，见 A-1 | ✓ |
| detach → bounded-poll（间隔 ≤30s）反复调 `stop_at_epoch.sh --stop-epoch k --contract contracts.json` | L101-117；stop_at_epoch.sh 参数面（--log/--contract/--stop-epoch/--pid-file）与三态输出实证吻合 | ✓ |
| State derivation 增「stop_status 未出且组活 → 继续调」分支 | L63-64（pid 活 → poll、never re-launch）分支在；但同节 L60 的 stop_status.json 存在性检查**路径缺 /train**（P-1），re-entry 场景该分支被笔误架空 | ✓（分支在；路径 bug 见 P-1） |
| 曲线 extract `--expected-epochs` = stopped_at_epoch | L146-153 | ✓ |
| `metric_curve compare --at-epoch k` vs baseline_metrics.jsonl | L156-169 + fail loud + epoch_compare.json{at_epoch==k, baseline_path} | ✓ |
| ckpt 可寻址 → 第 k ckpt eval vs baseline_k_acc 双过才 promote；eval 加载失败重派 1 次仍败 → eval_failed:true + eval_acc=null + 曲线单判 + 披露 | L19-21 参数 + L170-182 eval 失败矩阵 + L186-187 双门 | ✓ |
| 不可寻址 → 曲线单判 + eval_skipped_no_epoch_ckpt:true | L183-185 | ✓ |
| natural_done 且轮数 > k → monitor_failed:true | L124-127（kill 未中披露进 assessment + history）+ L214 | ✓ |
| probe 行 proxy_acc 恒曲线@k、eval 值置 eval_acc | L209/L211/L223 + history_lib.append_probe docstring 同义（D-V4-18） | ✓ |
| 等待循环内 push_curves sidecar | L140-145（best-effort never fatal） | ✓ |
| advance_round / 禁二次 detach / probe_status.md 继承 | L235-249 / L61-62+L64 / L66-68 | ✓（P-1 修复前「禁二次 detach」在 re-entry 路径被 L60 笔误架空） |

## ② Findings

- **P-1（MEDIUM，正确性 / re-entry，非残留类）** `probe_protocol.md:60` —— State derivation 写 `variants/<vid>/stop_status.json`，**缺 `/train` 段**。实证链：本协议 L116 传 `--pid-file .../variants/<VID>/train/train.pid`；stop_at_epoch.sh:70 `STATUS_FILE="$(dirname "$PID_FILE")/stop_status.json"` → 实际落盘 `variants/<VID>/train/stop_status.json`；全仓 grep 证实 stop_status.json 唯一写入方即该脚本、恒在 pid 文件旁；同节 L63 的 pid 分支正确写 `variants/<vid>/train/` → 属笔误非另构布局。**影响**：训练已终、曲线未抽的 re-entry（恰是 State derivation 的存在理由），该分支按字面检查必 miss → pid 组已死不命中 in-flight 分支 → 落到 L65「otherwise → start at the train step」→ 重渲染 + **二次 detach 同一 out-dir**，直接违反本协议 L61「never re-detach」与 agent.md 铁律 2（二次 detach 腐蚀 ckpt）。**建议**：L60 改为 `variants/<vid>/train/stop_status.json`（一行修复）。（交叉备案：`po_report/references/report_format.md:230` 存在同款缺 `/train` 写法，属该文件审查范围，提请其 reviewer 核对。）
- **P-2（LOW，可执行性）** `probe_protocol.md:191` —— promote one-liner 中 `b = <baseline curve metric at k>` 为自由占位符，取值来源未指明，与 L24-25「comparison 一律 python 读 JSON、禁心算」存在张力（`b` 从哪读没有交代）。epoch_compare.json 已记 `baseline_metric`（metric_curve.py:120）。**建议**：改 `b = c['baseline_metric']`，消除来源歧义。promote 正确性不受影响（curve_ok 权威取 `c['pass']`，`b`/`line` 仅进打印诊断）。
- **A-1（advisory，spec 行文 vs 协议记法差，零功能影响）** `probe_protocol.md:91` —— spec §4 L100 写 `--out variants/<vid>/train.rendered.sh`，协议实际 `--out .../variants/<VID>/train/train.rendered.sh`（多一层 train/，out_dir 与控制文件同置该目录）。协议自洽且 train/ 子目录承担 retry-wipe 的 out-dir 隔离（与 full_train 的 `final/train.rendered.sh` 扁平式不同但同为自洽布局）——判定为 spec 行简写丢层，非缺陷。建议二选一：回改 spec 行文补 `/train`，或在协议加半句说明 out 落 train/ 子目录。
- **A-2（LOW borderline，契约 §4 风格项——与 po_full_train 审查 F-1 同款同判）** `probe_protocol.md:189-199` —— promote 判定以内联 `python3 -c` 多行（含三元分支）呈现，按契约 §4 字面命中「python3 -c 分支逻辑应沉 scripts/」；但 spec §1 交付树 po_probe 无 scripts 目录、同族协议（full_train L154-160）同款内联风格、且实参含 agent 代入占位符。建议沉 `_po_scripts/promote_check.py`（与 gate_decide.py 同范式，部署后一行调用）或显式 waive。**非 spec 不一致，仅契约风格项**。
- 其余检查显式**零 finding**：任务词表 0 命中；v3.5 已删机制（proxy 短训渲染 / epoch-only proxy / 懒补训 / baseline_ref / run_verify / playbook 等）删净；spec §4 十二项逐条落位；无开发期残留（plan/issue/SPEC 编号、迁移出处、Orca 源码路径、测试项目名、事故复盘叙事均无）；产品说明书式成立（每段指令式、自包含、零历史负担）。

**零严重度观察（不计 finding，仅备案）**：
a) L81-83「read it once and note exactly which tokens it declares」而 render 命令无条件 `--set` 全键——已实证 render_run.sh 对额外 `--set` 键仅存 SETMAP 不报错（仅模板内 token 未替换才 FATAL，L10-11），无条件传键安全（full_train 审查备案 b 的悬问在此闭环）。
b) L131-132「the heal whitelist」= agent.md 铁律 1 的「re-render with corrected parameter values (path/argument alignment)」白名单，同句括号已内联定义，可独立执行。
c) L43 停滞判据的「curve point count」在基线期唯一曲线 = baseline_metrics.jsonl（finalizer 每 poll 周期增量 extract 的产物），语义清楚可判。

---

VERDICT: NEEDS-FIX — 洁净维度全部通过（任务词表 0 命中、v4 已删机制删净、无开发期残留、产品说明书式、spec §4 十二项落位），但 P-1（L60 stop_status.json 路径缺 /train，re-entry 会触发协议自己明令禁止的二次 detach）为实质正确性 finding，一行修复后复审即 PASS；P-2/A-1/A-2 为低危/风格项不阻断。

## 复验（commit `24eb711`，2026-08-26）

- **复验范围**：`git diff 2de195e..24eb711 -- workflows/agents/po_probe/ workflows/agents/_po_scripts/verdict_decide.py`（另核连带 report_format.md 与 spec §4 行文改动，及 tests/test_po_scripts.py 覆盖）
- **P-1（MEDIUM）已修** ✓ — `probe_protocol.md:61` 现为 `variants/<vid>/train/stop_status.json`，与 stop_at_epoch.sh:70 落盘位置（pid 文件 `variants/<VID>/train/train.pid` 旁）一致；re-entry 误判 → 二次 detach 的路径已闭合。连带 `po_report/references/report_format.md:234` 同款路径同步修正（属该文件自身审查范围，此处仅确认连带项在位）。
- **P-2（LOW）已修** ✓ — promote 检查（L189-192）改为 `python3 "$ORCA_ARTIFACTS_DIR/scripts/verdict_decide.py" promote --artifacts ... --vid <VID> --budget "<accuracy-budget>"`；promote line 从 `epoch_compare.json` **记录值** `baseline_metric` 重算、direction 取 `contracts.json`，自由占位符 `b = <...>` 消除。L24-25 一致性句同步改为「computed by the shared verdict script reading the recorded JSON values」。
- **A-1（advisory）已闭环** ✓ — 采用「回改 spec 行文」方案：spec §4 po_probe 行现写 `--out variants/<vid>/train/train.rendered.sh`，与协议路径逐字一致。
- **A-2（LOW borderline）已修** ✓ — 内联多行 `python3 -c` 判定沉入 `_po_scripts/verdict_decide.py`（172 行），协议只留一行调用 + 输出键说明，契约 §4「确定性代码内联」张力消除。**语义等价性逐项核对旧内联**：`line = baseline_metric ∓ slack`（slack = 1.0×budget）✓；`eval_ok` 缺席语义（ev/ba 任一 None → True → 曲线单判）✓；`promoted = curve_ok and eval_ok`、curve_ok 取 `epoch_compare.json` `pass` ✓；且**更严**——present-but-malformed 的 eval/anchor 文件 fail loud（L79-86，exit 2，注释明示「不得静默降级曲线单判」），优于旧占位符语义。部署链实证：deploy_scripts.sh:18 拷贝 `_po_scripts/*.py` 全量 → `$ART/scripts/`，协议调用的 `$ORCA_ARTIFACTS_DIR/scripts/verdict_decide.py` 在位。
- **测试覆盖** — tests/test_po_scripts.py:2720-2905 增 8 个 promote 用例（双过 / eval 拦截 / 文件缺席曲线单判 / 非对称单门分支 / present-but-malformed fail-loud / lower_better 方向 / 坏 compare fail-loud）+ final-budget 用例，意图级覆盖。
- **无新残留** — 新增 prose/代码仅含 operational 串（verdict_decide.py / epoch_compare.json / baseline_metric / baseline_k_acc.json / contracts.json），任务词表复扫 0 命中；协议其余段落未被本 commit 触及，原审逐段结论维持。

**未解决项**：无（P-1/P-2/A-1/A-2 四项全部闭环，连带项 report_format.md:234 已修）。

---

VERDICT: PASS — 四项 findings 全部闭环且经实证（P-1 路径修正在位、P-2/A-2 判定沉脚本且语义等价 + fail-loud 更严、A-1 spec 行文对齐），测试意图级覆盖到位，无新残留；原审洁净维度结论（词表 0 命中、v4 已删机制删净、产品说明书式、spec §4 十二项落位）全部维持。
