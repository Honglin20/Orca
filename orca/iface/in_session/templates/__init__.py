"""templates —— 宿主侧哑传输模板（opencode plugin + cc nudge 脚本）。

**架构守门**（D-v7-1）：模板里的宿主侧代码**零 Orca 业务逻辑**——只 spawn CLI 子进程
+ parse JSON 顶层字段 / 注入提醒文本。advance/router/replay/tape 路径一律禁止（CI grep 守门）。

模板由 ``teams install --target <host>`` 落地，不在 Python 运行时加载：

- ``opencode/orca.ts`` —— opencode plugin（v5 §4.4 idle nudge hook）。**v5 §8 step 4**：transform
  marker 派发入口段 + 全部死代码（extractTaskOutput / spawnCli / spawnTopLevelCli / rewriteText
  / findLastUserTextPart / extractModel / buildCliArgs）已删——transform 是旧 A 路径第二入口，
  v5 入口统一切到 orca skill，保留 transform = 让 marker 绕过 skill 起第二入口，违反单一接口。
  本 plugin **仅保留 idle nudge hook**（opencode nudge 载体，绝不自动推进）。
- ``cc_nudge.sh`` —— Claude Code Stop hook（v5 §4.4 + DEFECT-1 修复：python3 fail-loud）。

v5 §8 step 2b：``cc_hooks.py``（CC 路 A 的 Stop/PostToolUse hook 脚本生成）已删——A 路径退场，
B 路径（主 session 自调 ``orca next``）统一。``start`` 命令同 commit 删除。

**v5 §8 step 4**：``_constants.py`` 整删——``MARKER_REGEX`` / ``MARKER_LITERAL`` 仅被已退场的
transform 段引用，transform 删后无消费者。spec 守门：grep ``MARKER_REGEX`` 全仓 = 0。
"""
