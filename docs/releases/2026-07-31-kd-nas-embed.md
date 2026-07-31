# 2026-07-31 KD-NAS v4 嵌入：teacher-gen + train-script-gen 串进 DAG

把 ``teacher-gen`` + ``train-script-gen`` 两 folder-agent 从独立阶段嵌入 kd-nas workflow DAG，
完成 KD-NAS 重构的统一收口。DAG 从 4 节点（``flatten → setup → gate → train``）升级为 6 节点
（``flatten → teacher_gen → train_script_gen → setup → gate → train``）。

## 改了什么

### DAG + input（``workflows/kd-nas.yaml``）
- 新增 ``teacher_gen`` 节点（agent: teacher-gen）：读 ``flatten.output.baseline_contract_path``，
  纯调参派生 teacher wrapper（深度×3/宽度×2），产出 ``teacher_model_path`` / ``teacher_latency_ms`` /
  ``project_root`` / ``depth_axis`` / ``width_axis``。
- 新增 ``train_script_gen`` 节点（agent: kd-train-script）：读 flatten 契约 + teacher-gen teacher 路径
  + ``inputs.user_train_script``，生成自包含 ``train_pipeline.py``（teacher+distill 两模式），
  产出 ``train_pipeline_path``。
- **节点名用下划线**（``teacher_gen`` / ``train_script_gen``）：Jinja2 把 ``teacher-gen`` 解析为减法，
  节点名必须 ``[A-Za-z_]\w*`` 才能进 ``{{ node.output.X }}`` 表达式。``agent:`` 字段仍指连字符 folder 名。
- input 改名：``teacher_train_command`` → ``user_train_script``（用户原 train.py 路径，给 train-script-gen 读）。

### kd-setup/agent.md（核心改造）
- **step3**：teacher 校验从「repo 写死 ``_kd_scripts/teacher_model.py``」改为「透传 ``teacher_gen.output.teacher_model_path``」。
- **step5**：teacher 训练从「跑 ``teacher_train_command`` + ``setup_helpers find-teacher-ckpt`` 解析产物」
  改为「调 ``train_pipeline.py --mode teacher`` 固定 ``--out_ckpt``」+ teacher_setup 读 ``--teacher_latency_ms``
  透传自 teacher-gen（不再自测 latency）。加 exit-code 检查（reviewer MAJOR-1）。
- **step6（grep-user-train）删除**：loss/dataloader/optimizer 适配下沉给 train-script-gen（生成 train_pipeline.py 时搬入）。
- output schema 删 ``user_train_import`` / ``user_loss_fn``（不再 setup 产出）。
- step7/8 → step6/7 重编号；find 锚点从 ``teacher_model.py`` 改 ``kd_common.py``（reviewer MINOR-5）。

### kd-train/agent.md + train_pool.py
- ``train_pool._train_one`` 的 worker 从调 ``train_adapter_template.py`` 改为调 ``ctx["train_pipeline_path"] --mode distill``。
- ``--train_pipeline_path`` 新增 required CLI arg；``--user_train_import`` / ``--user_loss_fn`` 删除
  （train_pipeline.py 生成时已自包含搬用户 loss/dataloader）。
- ``_main`` 注入 ``os.environ["ORCA_KD_SCRIPTS_DIR"]``（生成物落盘 per-run artifacts，需此 env 才能 import kd.*）。
- **device 归一化**：device_plan 的 ``""``（fail-soft）对 ``train_pipeline._resolve_device`` 是非法值
  （``torch.device("")`` raise）→ ``device = device or "cpu"``（E2E 暴露，加测试守护）。

### teacher_setup.py
- 新增 ``--teacher_latency_ms``（optional，优先于 ``--latency_provider``）；``--latency_provider`` 改 optional。
- 三分支：透传 > provider 自测 > 双空 fail loud。

### train_adapter_template.py 退役
- 移到 ``_kd_scripts/_deprecated/``（被 ``train_pipeline.py`` 取代）；``_deprecated/README.md`` 记录原因。

### CONTRACTS.md
- DAG / §0 目录布局 / §3 CLI（加 train_pipeline.py + teacher_setup --teacher_latency_ms + train_pool --train_pipeline_path）
  / §4 节点 I/O 全部更新到 v4。

## 偏离 task boundary（Rule 7 surface-and-decide）

Task 边界写「不改三件套 agent 内部（model-flatten / kd-train-script / teacher-gen 的 SKILL/agent.md/scripts）」，
但实现时发现 ``teacher-gen/agent.md`` 和 ``kd-train-script/agent.md`` **按原样无法作 workflow 节点**：

1. **teacher-gen/agent.md** 用 ``{{ inputs.baseline_contract_path }}``——workflow 里 baseline 来自 ``flatten.output``，
   不是 inputs。validator 的 ``check_no_undeclared_input_refs`` 会拦。改 5 处 Jinja ref 为 ``flatten.output.baseline_contract_path``。
2. **kd-train-script/agent.md** 无 Jinja 输入模板（散文 placeholder）+ 输出是 stdout ``KEY: value``（非 JSON）
   ——workflow 节点要求 Jinja 渲染注入上游 output + 最终消息是 JSON。重写输入 section（加 Jinja）+ 输出改 JSON ``{train_pipeline_path}``。

**两处 agent.md 的 SKILL.md / scripts / templates 未动**（git diff 可验）。裁定：这是「workflow 节点契约」
的硬要求（Jinja ref 必须指向真实上游 output；节点最终消息必须单一 JSON），不修则无法嵌入。两 agent.md 自身的
旧文案也明确写「当前不嵌入 workflow yaml（独立阶段）」「留到统一阶段」——本次正是那个统一阶段。

## 验证

### 单测（WSL ``.venv``，``tests/workflows/``）
- ``test_kd_redesign`` + ``test_kd_train_script`` + ``test_struct_kd_p7`` + ``test_teacher_gen`` + ``test_model_flatten``：
  **230 passed**（含新增 4 测试：teacher_setup latency 三分支 + ORCA_KD_SCRIPTS_DIR env 注入 + device/mode argv 守门）。
- workflow compile：``load_workflow`` 通过；contract 5 项 check（input refs / schema chain / hardware inputs /
  chart labels / prohibition present）全绿。

### 脚本级 E2E（``orca``/``tars`` CLI 未装，用等价脚本链验证）
1. ``train_pipeline.py --mode teacher``（setup step5 模拟）→ teacher ckpt（278KB）。
2. ``teacher_setup.py --teacher_latency_ms 4.2``→ ``teacher_meta.teacher_latency_ms = 4.2``（从 param，**未测** ONNX）。
3. ``train_pool.py --train_pipeline_path <template>``（real distill worker）→ student ckpt + ledger ``status=SUCCESS``，
   ``acc=1.15``（真 NMSE，measure_student 跑 demo test_student.py）。

### 两路 code-reviewer 审查
- 设计审查：0 BLOCKER；3 MAJOR（kd-train-script preamble 过时 / setup step5 exit-code / setup_helpers legacy 标记）
  + 5 MINOR——**全修**。
- 测试覆盖审查：2 🔴（teacher_setup latency 三分支零覆盖 / _train_one argv 仅子串匹配）+ 3 🟡——**全修**
  （加 TestTeacherSetupLatencySource 3 case + argv 结构断言含 ``--mode distill`` + env 注入测试 + setup schema 反向守门）。

## Open Questions

1. **``orca``/``tars`` CLI 驱动的全链路 E2E 未跑**：本分支 ``orca`` / ``tars`` 控制台脚本未注册进 venv
   （``pyproject.toml`` 的 ``[project.scripts]`` 定义了但 ``pip install -e .`` 未跑），``tests/e2e_redesign/``
   的 ``test_tars_harness_walk`` 全 21 项在 HEAD 上就 fail（``FileNotFoundError``，非本次引入）。本次用
   「脚本级等价 E2E」替代（上述验证 §脚本级 E2E）。**真 LLM 驱动的全链路（flatten/teacher-gen/train-script-gen
   三 folder-agent 经 deepseek-v4-flash 跑）需在装好 ``orca`` CLI + API key 的环境复跑**。
2. **``test_no_fabrication[kd-nas]`` 预存失败**：``gpu_probe.py:127`` + ``teacher_model.py:206`` 的 ``torch.randn``
   被 fabrication check 误报（非 smoke/dummy/proxy 上下文）——在 HEAD（git stash 验证）上就红，非本次引入。
   建议后续把这两个文件的 randn 包进明确 dummy-context 函数或加 ``# noqa`` 注释。
3. **``setup_helpers.py`` 半退役**：其 ``find-teacher-ckpt`` / ``grep-user-train`` CLI 子命令不再被 active path
   调用（CONTRACTS.md + 文件 docstring 已标 ``[DEPRECATED v4]``），但文件 + 7 个单测仍留在主路径。
   未来若无外部消费者，可整体移到 ``_deprecated/``（YAGNI）。
4. **``teacher-gen``/``kd-train-script`` agent.md 偏离 boundary**：见上「偏离 task boundary」节。
   SKILL.md / scripts / templates 未动；仅最小接口调整（Jinja 输入 + JSON 输出）。

## Commit
本次任务按边界要求**未 commit / 未 push**（待用户确认）。CHANGELOG 索引不带 SHA。
