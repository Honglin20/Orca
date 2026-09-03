# prof-opt profiling 产物单出口收敛

日期：2026-09-03  
状态：实现完成  
实现 commit：`2c4b3e4`

## 用户决策

- 删除 `analyze.py`、`mfu_adapter.py`，不增加 `mfu_result.py` 等替代脚本。
- `mfu-analyzer` 的唯一语义产物是 `mfu_bottleneck_report.md`；原始分析文件路径写入报告的 `### 分析源文件`。
- latency gate 直接读取唯一 `<profile_dir>/<onnx_stem>/schedule_result.json.parallel_cycles`。
- structure-proposer 以 `shadow/` 模型源码为设计对象；ONNX 只在实现后由 `diff_check.py --layer graph` 机械校验。

## 改动

- 删除 profiling adapter/analyzer/predictor 旧链及其 contract，baseline/variant 链不再生成四件套和 `bottleneck_report.json`。
- baseline chain、origin freeze、latency recheck 统一直接校验原始 `schedule_result.json`。
- baseline/propose/analyst/reporter prompts 统一读取 `mfu_bottleneck_report.md`，按需下钻报告列出的源文件。
- `build_sig.py` 仅保留稳定 change signature 构造职责；dashboard/curve baseline 时延统一读取 `origin_anchor.json`。
- structure-proposer 与 structural-levers 改为 source-first，移除 proposal 阶段读取 `base/model.onnx` 和 predictor 依赖。
- 测试夹具与当前 v7/web SPEC 同步新产物契约。

## 验证

- `git diff --check` 通过。
- 修改过的 shell 脚本均通过 `bash -n`；修改过的 Python 文件均通过 `py_compile`。
- active `workflows/prof-opt` 与相关 tests 中无 `mfu_result.py`、`mfu_adapter.py`、`analyze.py`、`predict_delta.py`、`bottleneck_report.json`、`profile_summary.json` 残留引用。
- 合并 pytest 首次收集被 WSL 环境缺少 `httpx` 阻塞；随后用户中止测试，因此未继续运行测试套件。
- review-agent 工具不可用，改为本地职责/依赖/残留引用自审，未发现阻塞项。

## 未触碰

- `.claude/`、`.e2e_po/`、`.e2e_spe2e/`、`.e2e_perfver/`、`.e2e_scratch/`。
