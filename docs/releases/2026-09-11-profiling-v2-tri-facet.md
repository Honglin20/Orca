# profiling-v2 —— 三面融合（结构/特征/loss）+ 机械派生 history（2026-09-11）

## 任务

从 prof-opt（v8）隔离复制出 `workflows/profiling-v2/`，把搜索空间从「只有模型结构」扩为**结构 / 特征 / loss 三面**：一个候选 = selector 融合的恰一个有机设计；门不变（推理时延严格低于 origin 线 + 指标在预算内）；出口 shape/语义焊死、输入 shape 可变；蒸馏一切形式禁止。用户拍板：facet 能力不可用时**静默降级**（workflow 不退出，但必须披露）、历史层改为**机械派生的 history.md**（每轮 ≤2 句，取代 v8 的 LLM 叙事层）。

## SPEC

`docs/specs/profiling-v2-spec.md`。spec-reviewer 两轮对抗审查 + 闭环复核：6 BLOCKER / 6 MAJOR / 10 MINOR / 3 复核残留全闭环。审查抓出的关键设计修正：

- **facet 副本全程住 shadow 之外**（`facets/` + `variants/<vid>/facets/`）——shadow 内落点会炸掉锁 sha256 全量比较 / 影子包枚举 / assert / diff 四套既有机制
- **watchdog `render_eval` 补 `--set facet_dir`**（§8 唯一训练侧例外），渲染调用点全列七点——否则变体终局 eval 永不执行
- **「medium/high ⇒ 必带精度 facet」emit 门规则证伪删除**（会机械误杀纯结构高影响设计并逼出谎报），改为 selector 软指引 + `distillation_free_ack` 显式声明；归因纪律由五条纯一致性规则 R1-R5 承担

## 实现

- **脚本层**（coder-agent + 内部 code-reviewer 3 轮闭环，18 feedback 全 resolved）：新建 `render_history.py`（history.jsonl 机械渲染 base/history.md，in_flight 谓词 import 复用 frontier_snapshot）、`facet_check.py`（职责①出口 shape 无条件焊死 + 职责②features 可用时输入 shape/dtype 比对）；`history_lib`/`append_impl_row`/`check_propose_emit`（R1-R5 + 哨兵条件化 + render_stamp 新鲜度）/`run_latency_recheck`（facets 目录 diff 等值门）/`gate_node`（render 挂点 + stdout 纯净）/`check_contracts`（facets 指纹复用校验 + token 按消费面）/`watch_variant`/`push_curves`/`archive_round_shadow` 同步；删 `digest_stamp.py`
- **提示词层**（coder-agent + 内部 reviewer 2 轮闭环）：workflow.yaml description 重写（mfu「无静默降级」与 facet「静默收窄」两语分列）；po_propose 全量重写（三面融合 + facet_check 时序 + curator 段删除）；po_contract 增 facet 能力检测 Step 4c（定位→复制→参数化→dry-run 证明→token 回收→样本规格落盘）；po_baseline/po_probe/po_report 同步；新建 `feature-architect` subagent（哨兵 `[subagent:feature-architect v1 FAA8R2]`）；删 `history-curator`
- **接缝收口**（编排者）：`eval.sample_inputs` / `eval.facet_builder` 契约键补写入方（po_contract Step 4c item 7），与 facet_check 读取侧逐字对齐；SPEC 同步三处（facet_check 契约键 / impl 行落账移 Step 3 / R4-R5 支配关系）

## 验证

- `tars validate`：0 error 0 warning
- 新测试 `tests/test_pv2_history.py` + `tests/test_pv2_facets.py`：**81 green**（onnx/torch 真跑）
- prof-opt 回归（`test_po_scripts` + `test_po_v8_frontier` + `test_po_history_digest`）：**146 green 零恶化**（混序防污染双序验证）
- 洁净：14 个提示词文件开发残留 grep 零命中；受众翻转通读通过
- f47e5eb 帕累托图风格已同步 v2（push_curves 移植 + 改名验证）

## 遗留 / 挂账

- E2E 待有卡宿主（同 v8：本机 WSL 无 NPU/CUDA，flatten 即 fail loud 是预期合规路径）
- `check_report.py` 报告披露锚 token 三枚 vs 披露四行：facet 能力行暂未锚，未来加锚须同步 po_report/agent.md 的 "three anchor tokens"（跨层耦合已记录）
- `render_history` in_flight 块附带时延段（信息超集）；`watch_variant` facet_dir 供给条件为 features∨loss（契约逐字，无害超集）
