# Plan: `orca executor` CLI 扩展 —— 命令唯一真相源 + 完整参数可改

> 日期：2026-07-07 ｜ 状态：实施中 ｜ 关联：phase-14 遗留的半成品 `resolve_flags` 通道接通

## 1. 背景与目标

**痛点**：换平台（claude → opencode → nga …）时 flags 不同（如 `nga` 无 `--dangerously-skip-permissions`），当前只能改 profile 源码。`orca executor set` 只能改 binary，flags/prompt_channel 无法 CLI 改、也无法整体看到生效命令。

**目标**：给"backend 最终拼出什么命令"建立**唯一真相源**——能完整看到、能任意改 binary/flags/prompt_channel。

**scope（用户确认）**：只做 `binary` + `flags` + `prompt_channel` 三维。`env_overlay_prefixes` / `mcp_flag_template` / 协议字段（translator/terminal/stream_format/capabilities）**不动**——后者改了会破坏解析契约，走既有 `.orca/profiles/<name>.py` 整份覆盖。

## 2. 现状（半成品证据）

- `resolve_cli_path()`（binary）：env > config > default —— ✅ 通，有 `executor set`
- `resolve_flags()`（flags）：env > default —— ❌ **死代码**。builtin profile 未设 `flags_env`；`executor set-flags` 仅存于 `base.py:73` 注释，命令不存在
- `prompt_channel`：无 resolve 机制，frozen dataclass 直读

## 3. 接口契约（唯一一套）

### 3.1 命令面

```
orca executor show [profile]          # 唯一真相源：完整生效 argv + 每字段来源标注
orca executor set <profile> \         # 唯一写入入口：任意组合，写完回打生效 argv
    [--binary <path>] [--flags "<s>"] [--prompt-channel <stdin|argv>]
    [--scope project|user]            # 默认 project（写到 .orca/config.json）
orca executor unset <profile> [field] # field ∈ binary|flags|prompt_channel|all（默认 all）
    [--scope project|user]
orca executor list                    # 列 profile + 标 * 哪些被 override
orca executor test <profile>          # 健康检查（保留）
```

**破坏性变更**：`set` 的 binary 从位置参数改为 `--binary` flag（三维护一）。旧 `set claude "ccr code"` → 新 `set claude --binary "ccr code"`。更新对应测试。

### 3.2 config schema（flat，向后兼容）

```json
{
  "binaries":       {"opencode": "nga"},
  "flags":          {"opencode": "run --format json"},
  "prompt_channel": {"opencode": "argv"}
}
```

旧 `{binaries: {...}}` 继续生效（flags/prompt_channel 缺失 = 该维无 override）。

### 3.3 优先级（per-profile per-field，多 fallback 生效只一份）

```
shell env (ORCA_<PROFILE>_CLI/_FLAGS/_PROMPT_CHANNEL)
  >  .orca/config.json   （项目，cwd 下）
  >  ~/.orca/config.json （用户）
  >  profile default
```

合并语义：**per-field project 覆盖 user**（不是整份替换）。实现：merge 成一份 cfg 再 `apply_config_env` 一次 `setdefault`（project 值已覆盖 user，shell env 仍最高）。

### 3.4 env 注入桥（保分层：profiles 不 import config）

每个 builtin profile 设三个 env 通道字段：

| 维度 | profile 字段 | env 名约定 | resolve 方法 |
|---|---|---|---|
| binary | `cli_path_env`（已有） | `ORCA_OPENCODE_CLI` | `resolve_cli_path()`（已有）|
| flags | `flags_env`（已有字段，接通） | `ORCA_OPENCODE_FLAGS` | `resolve_flags()`（已有）|
| prompt_channel | `prompt_channel_env`（**新增**） | `ORCA_OPENCODE_PROMPT_CHANNEL` | `resolve_prompt_channel()`（**新增**）|

`apply_config_env` 把 merged config 的三 dict 分别 `setdefault` 进对应 env。`resolve_*()` 运行时读 env（与现有 binary 机制同构）。

### 3.5 `show` 输出格式（唯一真相源）

```
$ orca executor show opencode
Profile: opencode | 配置：.orca/config.json (项目) + ~/.orca/config.json (用户)

  binary          nga                        ← 项目
  flags           run --format json          ← 项目   (default: run --format json --dangerously-skip-permissions)
  prompt_channel  argv                       ← default
  model           <node.model；None=不传 --model>

▶ 生效命令（唯一真相源）:
  nga run --format json "<prompt>" [--model <node.model>]
  （运行时另按 node.tools / mcp server 追加 --allowed-tools / --mcp-config）
```

- 来源标注：`← env` / `← 项目` / `← 用户` / `← default`（effective≠default 才标 default 对照）
- prompt_channel=stdin 时：argv 不含 prompt，另注 `# prompt 经 stdin 传入`
- model：节点级动态，show 时不可知 → 占位 `[--model <node.model>]`

## 4. 实施步骤

1. `profiles/base.py`：+`prompt_channel_env: str = ""` 字段 + `resolve_prompt_channel()`（含 stdin/argv 校验，非法 warn + 回落 default）
2. `profiles/builtin/{claude,opencode,ccr}.py`：各 +`flags_env` +`prompt_channel_env`
3. `iface/cli/config.py`：
   - `load_config(path=None)` / `save_config(cfg, path=None)` 支持自定义 path
   - `project_config_path()` → `<cwd>/.orca/config.json`
   - `load_merged_config()` → per-field project 覆盖 user
   - `apply_config_env` 扩：注入 flags + prompt_channel（gated on profile 有该通道字段）
   - `bootstrap_config()` 改用 merged
4. 4 调用点 surgical：`.flags`→`.resolve_flags()`、`.prompt_channel`→`.resolve_prompt_channel()`
   - `gates/dialog.py:302,306`、`exec/validator.py:234,238`、`exec/claude/executor.py:367`、`iface/cli/executor_cmds.py:253,255`
5. `iface/cli/executor_cmds.py`：重写 `show`（生效 argv + 来源）+ 扩 `set`（三 flag + scope）+ 扩 `unset`（field + scope）
6. 测试：改 `test_executor_cmds.py` / `test_executor_e2e.py` 适配新 `set` 签名；加 flags/prompt_channel override 往返、项目 config 优先级、show 完整输出；`test_registry.py` 加 `resolve_prompt_channel`

## 5. 风险

- 项目 config 优先级合并语义（per-field）—— 测试锁住
- 向后兼容旧 `{binaries}`—— 保留，新字段并行
- `resolve_prompt_channel` 非法值 —— 注入层 + resolve 层双校验回落
- `set` 签名破坏性变更 —— 更新 3-4 个既有测试

## 6. 验收

- `orca executor set opencode --flags "run --format json"` → `show` 看到 flags 生效
- 项目 `.orca/config.json` 覆盖用户 `~/.orca/config.json`（per-field）
- `ORCA_OPENCODE_FLAGS=...` shell env 覆盖一切
- 无 override 时行为与现状逐字节一致（向后兼容）
- 全测绿 + code-reviewer 自检过
