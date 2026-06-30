# 开发计划 —— 阶段 9c：iface/web DAG + tape replay

> **状态**：待执行（**phase 9b 实现完成后开工**）
> **SPEC**：[`docs/specs/phase-9c-web-dag-replay.md`](../specs/phase-9c-web-dag-replay.md)
> **前置**：phase 9b（store + hooks + RunDetailPage 容器）
> **commit 规范**：`feat(web):` 前缀，独立分支

---

## 0. 产出与执行顺序

```
orca/iface/web/frontend/src/
├── components/
│   ├── graph/
│   │   ├── WorkflowGraph.tsx       C1（主组件 + 增量更新）
│   │   ├── graph-layout.ts         C1（dagre + findBackEdges）
│   │   ├── AnimatedEdge.tsx        C1
│   │   ├── nodes/                  C1（5 种 node widget）
│   │   └── constants.ts            C1（NODE_STATUS_HEX）
│   ├── detail/
│   │   ├── NodeDetail.tsx          C3
│   │   └── LogStream.tsx           C3（虚拟滚动）
│   └── layout/
│       └── ReplayBar.tsx           C2
├── stores/
│   └── replay-actions.ts           C2（增量 apply + checkpoint）
└── hooks/
    └── use-replay.ts               C2（定时器推进）
+ frontend/test/ × 3
+ tests/iface/web/test_playwright_9c.py
```

执行顺序：C1 DAG → C2 replay → C3 Log/Detail → C4 playwright

---

## C1. DAG 可视化（ReactFlow + dagre + 回环边）

### C1.1 `graph-layout.ts`
- `applyDagreLayout(wf)`：构建 nodes/edges，dagre TB 排名
- `findBackEdges(wf)`：DFS 识别回环路由（reviewer→optimizer），反向喂 dagre，渲染保持原方向
- 节点位置计算

### C1.2 `nodes/*.tsx`（5 种 widget）
- AgentNodeWidget：状态色 + token 数 + spinner（running）
- ScriptNodeWidget / SetNodeWidget
- ForeachGroupWidget / ParallelGroupWidget：父节点 + branches 子节点 + 进度计数（1/2）
- `constants.ts`：NODE_STATUS_HEX（5 色）

### C1.3 `WorkflowGraph.tsx`
- Effect 1：wf 变化 → 全量 build + dagre 布局
- Effect 2：nodes 状态变化 → 只更新 data（不全量 rebuild）
- `AnimatedEdge`：route_taken 高亮

### C1.4 验收（C1）— `frontend/test/graph.test.tsx`
- [ ] 渲染节点数 == wf.nodes 长度（+ parallel 组）
- [ ] **回环边**：nas.yaml（reviewer→optimizer）布局不乱（节点 y 坐标合理，回环节点在上）
- [ ] **增量更新**：改一个 node 状态，只该 node 重渲染（mock profiler / 断言 setFlowNodes 只改一个）
- [ ] 状态颜色映射（5 色）
- [ ] parallel 组渲染（父+子+计数）

---

## C2. tape replay（增量 apply + checkpoint）

### C2.1 `stores/replay-actions.ts`（store 扩展）
- `setReplayPosition(pos)`：前进 apply N..M / 后退 checkpoint rollback
- checkpoint：每 20 事件存 snapshot
- `setReplayMode(bool)`：切换 live/replay

### C2.2 `hooks/use-replay.ts`
- 定时器：按事件时间戳间隔推进 setReplayPosition
- 速度控制（1×/5×/10×/20×）
- 播放/暂停

### C2.3 `ReplayBar.tsx`
- 滑块（Event N/M）+ 播放按钮 + 速度下拉 + 时间刻度

### C2.4 验收（C2）— `frontend/test/replay.test.ts`
- [ ] setReplayPosition(5)：nodes 状态 == events[0..5] fold 结果
- [ ] **增量前进**：pos 3→8，apply events[4..8]（断言只 apply 这几个）
- [ ] **增量后退**：pos 8→3，checkpoint rollback（断言用 checkpoint）
- [ ] checkpoint 每 20 存（100 事件有 5 个 checkpoint）
- [ ] **live==replay**：run 完成 → setReplayPosition(末尾) → nodes == live 末尾状态（断言 deep equal）
- [ ] 定时器推进（速度 1× 按时间戳间隔）

### C2.5 测试骨架
```typescript
test('live equals replay at end', () => {
  const events = loadFixture('demo_mixed_events.json');  // 100 事件
  events.forEach(e => store.processEvent(e));  // live 全跑
  const liveFinal = snapshot(store.nodes);
  store.setReplayMode(true);
  store.setReplayPosition(0);
  store.setReplayPosition(events.length - 1);  // replay 到末尾
  expect(snapshot(store.nodes)).toEqual(liveFinal);  // byte-identical
});

test('incremental forward', () => {
  store.setReplayPosition(3);
  const spy = jest.spyOn(store, 'processEvent');
  store.setReplayPosition(8);
  expect(spy).toHaveBeenCalledTimes(5);  // 只 apply 4..8 共 5 个
});
```

---

## C3. Log Stream + Detail Panel

### C3.1 `LogStream.tsx`
- react-window 虚拟滚动
- 格式 `HH:MM:SS [session] <desc>`
- 按 session_id 分组
- replay 模式只显示 events[0..replayPos]

### C3.2 `NodeDetail.tsx`
- 选中节点的状态/token/output/tools/thinking
- replay 模式显示 replayPos 时快照

### C3.3 验收（C3）
- [ ] Log 虚拟滚动（1000 条 DOM 节点 < 50，react-window）
- [ ] 按 session 分组
- [ ] replay 同步（只显示到 replayPos）
- [ ] NodeDetail 显示选中节点 + replay 快照

---

## C4. playwright 验收（AI 自动测）

### C4.1 `tests/iface/web/test_playwright_9c.py`（@pytest.mark.integration）
- [ ] **DAG 渲染**：playwright 截图，断言 `.react-flow__node` 数量 == workflow node 数
- [ ] **回环边**：截图 nas.yaml DAG，断言节点布局合理（回环节点 reviewer/optimizer 在合理位置，不重叠）
- [ ] **状态颜色**：playwright 读节点背景色，断言 done=绿/running=蓝
- [ ] **replay 拖动**：playwright 拖 ReplayBar 滑块到中间，断言某节点状态变化（如 done→running）
- [ ] **live==replay**：run 完成 → replay 拖到末尾 → 断言节点状态 == live 末尾（playwright 读 DOM 对比）
- [ ] **增量不卡**：100 事件 run，拖滑块，measure 响应时间 < 100ms
- [ ] **历史 replay**：点 Done run → 断言 ReplayBar 显示 + 进 replay 模式

### C4.2 测试骨架
```python
@pytest.mark.integration
async def test_dag_render_count(live_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{live_server.url}/runs/{run_id}")
        await page.wait_for_selector(".react-flow__node")
        count = await page.locator(".react-flow__node").count()
        assert count == expected_node_count  # == workflow node 数
        await browser.close()

@pytest.mark.integration
async def test_replay_scrub(live_server):
    async with async_playwright() as p:
        # ... 打开完成的 run
        # 拖滑块到中间
        await page.locator("[data-testid=replay-slider]").fill("50%")
        # 断言某节点状态变化
        node_status = await page.locator("[data-testid=node-A-status]").text_content()
        assert node_status == "running"  # 中间时刻该节点在运行
```

---

## 5. 总验收（Definition of Done）

### 5.1 单元测试（vitest）
- [ ] C1 DAG（回环边/增量更新/5 色）
- [ ] C2 replay（增量 apply/checkpoint/live==replay）
- [ ] C3 Log/Detail

### 5.2 playwright（关键）
- [ ] C4 DAG 渲染 + 回环边 + replay 拖动 + live==replay + 不卡

### 5.3 5 条铁律（SPEC §0.1）
- [ ] live/replay 同一 fold（grep 1 个 handler 表）
- [ ] live==replay 末尾（断言）
- [ ] 增量 apply（不全量重放，有测试）
- [ ] 回环边正确（playwright 截图）
- [ ] DAG 增量更新（不全量 rebuild）

### 5.4 构建
- [ ] `npm run build` 含 graph/replay 组件

### 5.5 交付物
- [ ] graph/ + detail/ + replay 组件
- [ ] tests + playwright
- [ ] **commit `feat(web):` 前缀，独立分支**

---

## 6. 不做（边界，SPEC §7）

gate 弹窗/chart（9d）· 后端/store 骨架（9a/9b）· 第二套 replay fold（红线）
