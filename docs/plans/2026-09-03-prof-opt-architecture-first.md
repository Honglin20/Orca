# prof-opt architecture-first propose 实施计划

日期：2026-09-03

## 范围

聚焦 `po_propose`，同步改动 latency admission、`po_probe` 训练准入和 incumbent 晋升；不做端到端测试。

## 步骤

1. 新增三个候选架构 sub-agent、一个 selector 和 Ascend hardware reference。
2. 改写 `po_propose` dispatch：并行候选、selector 融合、单 proposal 实施。
3. 将 latency gate 改为相对当前 incumbent 的严格改善；target 仅披露。
4. 增加最小 incumbent promotion：精度通过的改善变体更新 base 源码/证据和 parent 指针。
5. 删除 proposal/declaration/recheck 的 `op_delta` 硬依赖，保留源码快照和 change_spec。
6. 更新 probe、gate、report、workflow 注释和契约测试；只运行静态检查与定向单测，不做 E2E。
7. 分配 review agent：洁净契约审查 + 方案逻辑完整性审查，修复全部 findings。

## 成功标准

- `tars validate workflows/prof-opt/workflow.yaml` 通过。
- shell/python 静态检查通过。
- latency 改善变体能进入 probe，即使没有达到 origin target。
- selector 只有一个融合结果交给 implementer/mfu-analyzer。
- promotion 后 origin target 不变、下一轮 parent/incumbent 指向正确。
- review agent 无未关闭的高严重度问题。
