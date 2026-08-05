"""templates —— 宿主侧哑传输模板（opencode plugin + cc nudge 脚本）。

**架构守门**（D-v7-1）：模板里的宿主侧代码**零 Orca 业务逻辑**——只 spawn CLI 子进程
+ parse JSON 顶层字段 / 注入提醒文本。advance/router/replay/tape 路径一律禁止（CI grep 守门）。

模板由 ``tars install --target <host>`` 落地，不在 Python 运行时加载：

- ``opencode/orca.ts`` —— opencode plugin（v5 §4.4 idle nudge hook + SPEC posttooluse-rogue-guard
  §8 ``tool.execute.after`` guard hook）。**v5 §8 step 4**：transform marker 派发入口段 + 全部
  死代码（extractTaskOutput / spawnCli / spawnTopLevelCli / rewriteText / findLastUserTextPart
  / extractModel / buildCliArgs）已删——transform 是旧 A 路径第二入口，v5 入口统一切到 orca
  skill，保留 transform = 让 marker 绕过 skill 起第二入口，违反单一接口。本 plugin 保留 idle
  nudge hook（opencode nudge 载体，绝不自动推进）+ tool.execute.after guard hook（PostToolUse
  事后告警，pure hint 不阻止 / 不推进 / 不捕 output）。
- ``cc_nudge.sh`` —— Claude Code Stop + PostToolUse 双事件 hook（v5 §4.4 + SPEC posttooluse-rogue-guard
  §7 + DEFECT-1 修复：python3 fail-loud）。按 stdin JSON 的 ``hook_event_name`` 分支：Stop 走原
  decision:block 提醒（v5 §4.4 不变）；PostToolUse 走 additionalContext 事后告警（pure hint）。
- ``tool-classification.json`` —— PostToolUse 事后告警的工具分类单一真相源（SPEC
  posttooluse-rogue-guard §5）。``cc_nudge.sh`` 的 PostToolUse 分支 + ``orca.ts`` 的
  ``tool.execute.after`` 钩子启动时各 read 一次，做传输层分类（决定是否注入提示文本），非编排
  状态机判断（D-v7-1 不禁；判例 §3 注脚 P2）。install 时拷到 cc 家族 ``<root>/hooks/`` 与
  opencode 家族 ``<root>/plugins/`` 下（与脚本/plugin 同目录）。

v5 §8 step 2b：``cc_hooks.py``（CC 路 A 的 Stop/PostToolUse hook 脚本生成）已删——A 路径退场，
B 路径（主 session 自调 ``orca next``）统一。``start`` 命令同 commit 删除。**注**：现行
PostToolUse 守卫（SPEC posttooluse-rogue-guard）**不是** A 路径复活——A 路径捕 Task output
驱动 advance；本守卫只做 hint，不捕 output、不推进（详见 SPEC §3 对比表）。

**v5 §8 step 4**：``_constants.py`` 整删——``MARKER_REGEX`` / ``MARKER_LITERAL`` 仅被已退场的
transform 段引用，transform 删后无消费者。spec 守门：grep ``MARKER_REGEX`` 全仓 = 0。
"""
