# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 状态（2026-07-20）

- ✅ **量化 workflow 路线图 W1–W4 全部完结**（本日 W3+W4 落地，commit `e6646cf`+`da609ac`）。4 个量化 workflow 全部 in-session 可用，全 mxint 基。
- ✅ **文档交付**：`docs/in-session-usage.md`（安装+使用简述）+ `docs/workflows/` ×7（3 NAS + 4 量化，每篇由浅入深：激活→原理→结果+截图占位）+ README 索引。
- ts_quant 已 editable 装入 conda orca env（实测可用）；待正式加进 orca pyproject 依赖。
- 本地领先 origin 多 commit（push 待用户手动）。

## 量化 workflow 路线图（W1–W4，全部 ✅）

- ✅ **W1 敏感层分析** `quant-sensitivity`（`ca6bb60`）
- ✅ **W2 粗粒度 PTQ 扫描** `quant-ptq-sweep`（`d356979`）
- ✅ **W3 位宽-精度曲线** `quant-bit-curve`（`e6646cf`）：混合精度 Pareto（INT8/W4A8/INT4/MX4/MX8），`search_mix_precision(m0_pareto)`，line+bar+table + bake 最佳混合模型。
- ✅ **W4 QAT** `quant-qat`（`e6646cf`）：rtn/duquantpp 双方案 + CAGE 后校正，teacher-student label-free QAT，收敛+恢复可视化 + bake。

## 待确认（收尾，非阻塞）

- ts_quant 正式进 orca pyproject 依赖（落实"装 orca 即装 ts_quant"）
- W3/W4 真机 in-session E2E（`orca <wf> --inputs` → `next` 循环到 done，经 opencode headless + tars）——脚本级 + tars validate + schema 已证，next-loop 由用户交互式跑
- 各 workflow 文档里 📊 截图占位 → 跑一次由 `orca open` 截真图替换

## 并行：in-session 加固（orca 引擎，可穿插）

P5（F1 resume）done。候选 P2（marker 三态）/ P4（失败兜底）/ P6（contract-test），待用户选定。既有 debt/follow-up 全量见 CHANGELOG，SPEC `docs/specs/2026-07-19-in-session-hardening-and-perf.md` v4.1。

## 必读文件（开工前按需）

- `docs/workflows/README.md`（7 workflow 索引 + 量化 pipeline 顺序）
- `docs/in-session-usage.md`（in-session 安装与使用）
- W1/W2 范本：`workflows/quant-sensitivity.yaml` + `agents/sensitivity-analyzer/`；`workflows/quant-ptq-sweep.yaml` + `agents/ptq-sweeper/`
- W3/W4 范本：`workflows/quant-bit-curve.yaml` + `agents/bit-curve-searcher/`；`workflows/quant-qat.yaml` + `agents/qat-trainer/`
- [CHANGELOG](CHANGELOG.md)
