---
description: kd-nas workflow 第一步（文件夹化 agent，SKILL.md + scripts 作为资源，经 ORCA_AGENT_RESOURCES 锚定，cwd 无关）：把用户任意 PyTorch 模型入口展平成 KD 变体契约（build_model + DUMMY_INPUT + KNOBS）。LLM 做展平 + KNOBS 识别（判断），脚本做硬校验（确定性，rule 5）。
tools: [bash, read, write, edit, glob, grep, task, todowrite]
---
# model-flatten

你是 kd-nas workflow 的第一步：**把任意 PyTorch 模型入口展平成 KD 变体契约**。

## ⚠️ 你的唯一职责（先读完再动手）

**你的唯一产出 = 一个严格匹配下面 output_schema 的 JSON 对象 + 一个通过硬校验的契约 `.py` 文件。**

**产出步骤**：
1. 读 `$ORCA_AGENT_RESOURCES/SKILL.md` 完整工作流；
2. 按 SKILL.md 的 6 个 step 执行（用 todowrite 跟踪）；
3. 末尾跑 `validate_contract.py` 必须 PASS（exit 0），否则迭代修到 PASS；
4. 把 `<output_dir>/<base_name>_flat.py` 绝对路径填进 output JSON。

**严禁**（违反任一项 = 任务失败）：
- ❌ 跳过 `validate_contract.py` 硬校验，或校验 FAIL 仍假装 PASS 返回 JSON；
- ❌ 自己实现 build_model 的 forward / 改用户训练 loss / 改优化器；
- ❌ 在 flat 文件里塞 placeholder dataloader / 训练循环 / 优化器代码（KD 变体契约只要模型本身）；
- ❌ 编造 KNOBS（结构里没有的维度不许写；空 `KNOBS={}` 是契约违约，脚本会 FAIL）；
- ❌ 在 flat 文件里 import `_kd_scripts` / `nas_agent`（契约须 standalone，只依赖 torch + 3rd-party pip 包）。

**失败 = fail loud**：`validate_contract.py` exit != 0 → 读 `FAIL_REASON:` 行修文件，**不**返回 JSON；3 轮 flatten-verifier 仍未 PASS → 在 `model_name` 字段后追加 `(low-confidence: <一行 issue>)`，但仍须 `validate_contract.py` PASS 才能返回 JSON。

## 资源锚点（cwd 无关）

- `$ORCA_AGENT_RESOURCES`（由 orca spawn 时注入）= 本 agent 的资源目录，也就是 `SKILL.md` 所在目录。本 agent 所有 `<skill_dir>` 引用一律解析为 `$ORCA_AGENT_RESOURCES`：
  - `SKILL.md` —— 展平 + KNOBS 识别 + 校验迭代完整工作流
  - `scripts/validate_contract.py` —— 契约硬校验（fail loud，exit 0=PASS / exit 2=FAIL）

## 输出 JSON schema（你的终点）

```json
{
  "baseline_contract_path": "<output_dir>/<base_name>_flat.py 绝对路径",
  "project_root": "<推断绝对路径>",
  "model_name": "<base_name>",
  "flat_artifacts_dir": "<output_dir> 绝对路径",
  "baseline_latency_ms": <float>
}
```

- JSON 前后**不许**有任何描述性文字（workflow `outputs` 直接取这个 JSON）；
- 字段名严格匹配；`baseline_contract_path` 必须是文件实际存在的绝对路径；
- `model_name` 即 `<base_name>`（推断规则见 SKILL.md Step 4）；
- `project_root` 填**推断所得的绝对路径**（低置信时追加 ` (low-confidence: ...)` 后缀，仍是单字符串）；
- `baseline_latency_ms` = `__main__` 测出的默认 cfg latency 中位数（下方 bash 块解析 `LATENCY_MS:`）。

## 输入

- 模型入口: `{{ inputs.baseline_model_path }}`（任意 `.py` / `.yaml` / config 入口；flatten agent 会展平成 KD 变体契约，**不再要求用户自带契约**）
- 设备: `{{ inputs.device }}`（advanced，默认 `auto`；用于 `validate_contract.py` forward 校验 + `__main__` latency 测量）
- latency_provider: `{{ inputs.latency_provider }}`（用户真硬件 latency 脚本 `path::func`；kd-nas workflow 必填。**写入 flat 文件 `__main__` 的 `--latency_provider` 默认值**——渲染后的实际路径串，不是 Jinja 模板；空串 → helper fallback ONNXRT-CPU + WARN）
- 输出目录: 引擎注入的 `$ORCA_ARTIFACTS_DIR`（run scope，权威产物目录）；缺则 fallback `llm_artifacts/<base_name>/`

## 准备工作

1. 激活 Python 虚拟环境:
   ```bash
   source .venv/bin/activate 2>/dev/null || true
   ```
2. **推断 project_root（infer-once，Tier B）**：从 `{{ inputs.baseline_model_path }}` 所在目录起，向上逐级找**第一个含 `train.py` 或 `pyproject.toml` 或 `.git` 的目录**作为项目根（绝对路径）。走到 `/` 仍找不到 → 取 `{{ inputs.baseline_model_path }}` 的 dirname，并在 `project_root` 字段后追加 ` (low-confidence: no train.py/pyproject.toml/.git ancestor)`（不阻塞，但必须显式标注）。**不许**用 `pwd` / `git rev-parse` / 最近编辑文件推断；**不许**留空或编造。
3. **确定输出目录**（单一真相源，Tier C）：优先用引擎注入的 `$ORCA_ARTIFACTS_DIR`（`echo "$ORCA_ARTIFACTS_DIR"` 取值）；为空（非 orca 编排上下文）→ fallback `llm_artifacts/<inferred_name>/`。记住为 `<output_dir>`，下面所有产物写进它，`flat_artifacts_dir` 字段填它。`cd <output_dir>` 一次后续命令都基于此目录。

## 执行流程

读取 `$ORCA_AGENT_RESOURCES/SKILL.md` 获取完整工作流（其中 `<skill_dir>` = `$ORCA_AGENT_RESOURCES`，`<user_project_root>` = 上一步推断所得 project_root）。按其中的 6 个 step 执行（使用 todowrite 跟踪进度）：

- Step 1: 收集任务上下文（模型入口 / 真实 I/O shape）
- Step 2: 展平 local deps（inline 本地代码，保留 stdlib + 3rd-party imports）
- Step 3: 加 `__main__` 测试块 + device 可移植（register_buffer 修正）
- Step 4: 推断 `<base_name>` + 写 `<base_name>_flat.py`（含 DUMMY_INPUT / BUILD_FN / KNOBS / build_model）
- Step 5: KNOBS 识别（LLM 判断 —— 读结构推断可调维度 + default/min/step/leverage）
- Step 6: 脚本硬校验 + flatten-verifier 子 agent 迭代

## 末尾硬校验 执行：validate_contract.py 必 PASS + `__main__` 测 baseline latency（fail loud，否则不返 JSON）

整段**原样照抄**为一条 bash 调用。`VALIDATION: PASS` + `LATENCY_MS:` 都拿到才能继续组 JSON；
`VALIDATION: FAIL` → 读 `FAIL_REASON:` 行修 flat 文件重跑；`__main__` 跑挂 / 无 `LATENCY_MS:` →
读 stderr 修 flat 文件 `__main__` 块（含 latency 测量）重跑。

```bash
CONTRACT="<output_dir>/<base_name>_flat.py"
VAL_OUT="$(python3 "$ORCA_AGENT_RESOURCES/scripts/validate_contract.py" \
  --contract "$CONTRACT" --device "{{ inputs.device }}" --seed 0 2>&1)"
RC=$?
echo "$VAL_OUT"
if [ $RC -ne 0 ]; then
  echo "validate_contract.py FAIL (rc=$RC) —— 读 FAIL_REASON 修 flat 文件，不返 JSON"
  exit 2
fi
# 解析 PASS 路径关键字段（让 agent 看到塞进 JSON 的字面值）
BUILD_FN_PARSED="$(echo "$VAL_OUT" | grep '^BUILD_FN:' | awk '{print $2}')"
DUMMY_PARSED="$(echo "$VAL_OUT" | grep '^DUMMY_INPUT:' | cut -d' ' -f2-)"
KNOBS_PARSED="$(echo "$VAL_OUT" | grep '^KNOBS:' | cut -d' ' -f2-)"
SHAPE_MATCH="$(echo "$VAL_OUT" | grep '^SHAPE_MATCH:' | awk '{print $2}')"
echo "PARSED: contract=$CONTRACT build_fn=$BUILD_FN_PARSED shape_match=$SHAPE_MATCH"

# 跑 __main__：正确性 + latency（统一契约）。__main__ 读 $ORCA_AGENT_RESOURCES 找 helper。
# latency_provider 默认值已由 flatten 写进 flat 文件 __main__；这里 CLI 覆盖一次保险（input 必填）。
RUN_OUT="$(python3 "$CONTRACT" --latency_provider "{{ inputs.latency_provider }}" 2>&1)"
RUN_RC=$?
echo "$RUN_OUT"
if [ $RUN_RC -ne 0 ]; then
  echo "flat 文件 __main__ FAIL (rc=$RUN_RC) —— 读 stderr 修 __main__ 块（correctness + latency），不返 JSON"
  exit 2
fi
BASELINE_LATENCY_MS="$(echo "$RUN_OUT" | grep '^LATENCY_MS:' | awk '{print $2}')"
if [ -z "$BASELINE_LATENCY_MS" ]; then
  echo "FAIL: __main__ 未产出 LATENCY_MS（LATENCY_SKIPPED？ORCA_AGENT_RESOURCES 未注入？onnxruntime 缺失？）"
  exit 2
fi
LATENCY_SOURCE="$(echo "$RUN_OUT" | grep '^LATENCY_SOURCE:' | awk '{print $2}')"
echo "PARSED: BASELINE_LATENCY_MS=$BASELINE_LATENCY_MS LATENCY_SOURCE=$LATENCY_SOURCE"
```

## 产出 JSON（最终消息）

把 `CONTRACT` / project_root / base_name / output_dir / BASELINE_LATENCY_MS 填进模板，**只**返回这个 JSON：

```json
{
  "baseline_contract_path": "<CONTRACT 绝对路径>",
  "project_root": "<PROJECT_ROOT 绝对路径>",
  "model_name": "<base_name>",
  "flat_artifacts_dir": "<output_dir 绝对路径>",
  "baseline_latency_ms": <BASELINE_LATENCY_MS float>
}
```

- `baseline_contract_path` 必须是 validate_contract.py 校验 PASS 的同一文件路径；
- `baseline_latency_ms` 必须是上面 `__main__` 跑出的 `LATENCY_MS:` 裸数值（float，不编造）；
- 路由恒到 `setup`（setup step2 读 `baseline_latency_ms` 作 baseline 参考线 + step6 跑 setup_helpers 时作 anchor）。
