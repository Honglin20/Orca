# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 待 commit（2026-08-04，本轮 kd-nas 三件事，均未提交）

### A. gpu_probe teacher_cache 可选（device-only）+ setup step3 grep bug
- `gpu_probe.py`：`--teacher_cache` 改可选 + 新增 `_probe_device_only`；VRAM/device-only 两路。
  根因：setup 在 teacher 训练前跑 gpu_probe，旧版传 baseline `.py` 当 teacher_cache → `torch.load` 崩。
- `kd-setup/agent.md` step3：删 `--teacher_cache`（device-only）+ 修 grep bug（`^DEVICE:`→`^RESOLVED_DEVICE:`）。
- 详见 [gpu_probe release](docs/releases/2026-08-04-kd-nas-gpu-probe-teacher-cache-optional.md)。

### B. flatten 产物落项目 artifacts 根 + 删 baseline latency bar + review R1 闭环
- flatten `<output_dir>` → `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/`（与 setup 同根）；删 baseline bar。
- R1（code-reviewer）：step3 去后缀落定确定性 python 片段（与 setup 逐字对齐）。
- 详见 [flatten release](docs/releases/2026-08-04-kd-nas-flatten-artifacts-dir-and-drop-baseline-bar.md)。

### C. 前序：kd-train-script 重写（2026-08-03，待确认 commit）
- 模板占位符 → 强制特化；`fidelity_check.py` + 四层校验。code-review 两轮闭环。

### 验证
- `tests/workflows/` **421 passed / 3 skipped / 5 failed**——5 个全为 HEAD 预存失败（finalize_kd/teacher_setup/kd_setup），
  本轮三件事 **0 新红**。

## 待办
- [用户] 确认后 commit A / B / C（均未提交）。
