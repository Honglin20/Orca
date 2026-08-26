# Release: 节点记忆(Node Memory)—— AgentNode 跨 run 记忆

**日期**:2026-07-18
**范围**:in-session shell 节点级记忆(写确定性 / 读注入 agent 判断)
**commit**:`29c70b3`

---

## 背景

in-session workflow 此前**无跨 run 讘忆**:每次 `orca <wf> --inputs` 生成新 run_id、新 tape,新 run 看不到旧 run 的节点产出。对「prompt 确定、上游输出只变一点点」的反复跑场景,同一节点每次都从头执行,无法跳过。

用户诉求:让部分节点有记忆,已执行过的任务可跳过,按**项目路径**区分;参考 Claude Code `~/.claude/projects/<enc>/memory/`。

## 设计取舍(关键决策)

讨论中**否决了「确定性指纹缓存」**(fingerprint / TTL / index / 新事件类型):agent 节点不是纯函数,输入相同不代表输出该复用,指纹判定对 agent 编排收益薄、改动大。

最终方案把「必然性」和「智能判断」解耦:

- **写记忆 = 引擎确定性**。节点完成必然把 output 覆盖写到 MD,**不靠执行节点的子 agent 自觉**。
- **读记忆 + 跳过判断 = agent**。prompt 注入上轮 output,agent 自己决定本轮输入是否值得重跑,走**正常推进路径**(产出 output → `orca next`),引擎**零 skip 分支**。
- **记忆内容 = 上一轮 output 原文快照**,不做 agent 提炼(output 本就是「给下游的信息」)。覆盖式写天然单份 + 天然过期清除。

参考 [design draft / 准 SPEC](../specs/node-memory-design-draft.md)。

## 为什么写入是确定性的

1. **触发点必然发生**:写记忆挂在 `apply_step_result` 的 `emit_batch` **成功之后**——只要 `node_completed` 落 tape,紧接着必然执行写循环。写记忆不是执行节点的子 agent 干的,子 agent 再"偷懒"也会被引擎替写。
2. **判定是纯字段比较**:`emit.type == "node_completed"` + `node.memory == True` + `not no_memory`,无语义判断。
3. **覆盖式写 = 过期自动清除**:`tmp + os.replace` 整份重写,旧 output 被新 output 整体替换,单份不堆积。"过期清除"是代码行为副产物,不靠 agent 记得删。
4. **失败不破坏语义**:写失败 `best-effort warn`(不阻断 run)。MD 是**派生缓存**,`tape.node_completed.data.output` 才是唯一真相源;MD 坏了 = 当首跑重做,正确性不受影响。

## 改动

### 源码

- **`orca/schema/workflow.py`**:`AgentNode` 加 `memory: bool = False`(opt-in;仅 AgentNode,ScriptNode output 复用价值低 YAGNI;foreach body 在 `orca run` scope 外)。`extra="forbid"` 不变。
- **`orca/run/memory.py`(新)**:三个 helper——`write_node_memory`(覆盖写 + frontmatter 4 字段 + best-effort warn)/ `read_node_memory_body`(strip frontmatter 取 body,损坏返 None)/ `inject_memory_prompt`(拼「上一轮记忆 + 复用协议」段)。零 events/tape/reducer 依赖,run 层单向被 iface 调。
- **`orca/run/step.py`**:`_deliver` 在 `render_prompt` 后、`_write_prompt_file` 前加注入分支;`_deliver` + `advance_step` 加 `wf/project_root/no_memory` kwargs(默认值保 `prompts_dir=None` inline 单测路径不破),三处 `_deliver` 调用全透传。`render_prompt`(exec 层)零文件 I/O 契约不动。
- **`orca/iface/in_session/_step_io.py`**:`apply_step_result` 加 4 kwargs(`wf/run_id/no_memory/project_root`) + 抽 `_write_memories_for_emits` helper(cli/daemon 单一真相源);在 `emit_batch` 成功后遍历 emits,对 `node_completed` + `memory=True` 调 `write_node_memory`。
- **`orca/iface/in_session/cli.py`**:`bootstrap` / `next` 加 `--no-memory` flag;全链路透传 `project_root=Path.cwd()` / `no_memory`。
- **`orca/iface/in_session/daemon.py`**:daemon.next 的 `advance_step` 补 `project_root=Path.cwd()`(修 cli/daemon 写读不对称),`apply_step_result` 同步。
- **`.gitignore`**:append `.orca/`(存在则 append,不存在 skip)。

### MD 格式

```
<cwd>/.orca/memory/<wf.name>/<node.name>.md
```
frontmatter 4 字段(`run_id/timestamp/workflow/node`)+ body(`output_schema=None` → output 原文;非 None → `json.dumps(parsed, ensure_ascii=False, indent=2)`;空 output → 空 body)。

### 不碰

`EventType` / reducer / tape 格式 / `advance_step` 决策分支 / `Status` 语义 / `render_prompt` / chart_sock / sidechain_daemon / host_session binding。scope = in-session only(`orca run` drive_loop 不吃)。

## 验证

- **spec-reviewer**:conditional-pass → 5 P0(B1 写记忆位置 / B2 注入点 / B4 字段位置 / N1 output 序列化 / N3 名称来源 / N4 `--no-memory`)+ 3 决策点全收敛。修正 draft 两处事实错误(`apply_step_result` 在 `_step_io.py` 非 `cli.py`;tape 是用户级 `~/.orca/runs` 故 project_root 须 `Path.cwd()`)。
- **code-reviewer**:2 🔴(损坏 MD 路径零测试 / `--no-memory` 透传链零端到端测试)+ 6 🟡(daemon 写读不对称 / 空 body 注入 / 跨 project 双向隔离 / lazy import 提顶层 等)全修。
- **单测**:`tests/iface/in_session/test_node_memory.py` 22 个(SPEC §8 全覆盖 + 边界守门);全 in-session + schema + run 回归 **515 passed** 无回归。
- **test-agent 真机 E2E**:5 场景全过——① 首跑写 MD(frontmatter 4 字段 + body 原文)② 二跑 prompt 含「上一轮记忆」+ 上轮 body + 复用协议 ③ `--no-memory` MD 字节级不动(md5 同)④ 跨 cwd 隔离互不可见 ⑤ 写失败(chmod 0500)`orca next` 不崩、tape `node_completed` 正常落、warn 含 `event=memory_write_failed`。

## 边界

- **副作用节点不应开 `memory`**(写代码/改文件节点光读 MD 不能复现副作用;文档 warn 不强制)。
- **超大 output 臃肿**:大段 dump 节点不适合;提炼版 YAGNI 暂不做。
- **worktree 隔离**:项目内 `.orca/memory/` 每个 worktree 各一份(通常合理)。
- **重命名孤立**:`wf.name`/`node.name` 改后旧 MD 不再读写,手动 `rm`(`orca memory clear` CLI 延后 YAGNI)。
- **跨 host_session 共享**:MD 故意跨 host_session(项目级沉淀,与 tape host_session 维度正交)。

## Follow-up

- `orca memory clear [<workflow> [<node>]]` CLI(清理孤立 MD)。
- frontmatter YAML 注入面(`wf.name`/`node.name` 含 `:`/换行)——compile 层字符集校验。
