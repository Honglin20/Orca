# Release: `orca executor` CLI 扩展 —— 命令唯一真相源 + spawn 参数全可改

> 日期：2026-07-07 ｜ 计划：[`docs/plans/2026-07-07-executor-cli-extend.md`](../plans/2026-07-07-executor-cli-extend.md)

## 背景

换平台（claude → opencode → nga …）时 flags 不同（如 `nga` 无 `--dangerously-skip-permissions`），但 `orca executor set` 只能改 binary，flags/prompt_channel 无法 CLI 改、也看不到最终生效命令。`resolve_flags` 通道（phase-14 半成品）死代码——builtin profile 未接 `flags_env`、`set-flags` 命令仅在注释里提及。

## 目标

给 backend 最终拼出的命令建立**唯一真相源**——完整可见、binary/flags/prompt_channel 三维任意可改。约束：接口只有一套、真相源只有一个（多 fallback 生效只一份）。

## 接口（唯一一套）

```
orca executor show [profile]          # 唯一真相源：完整生效 argv + 每字段来源标注
orca executor set <profile> \         # 唯一写入入口：三维任组，写完回打生效命令
    [--binary <path>] [--flags "<s>"] [--prompt-channel <stdin|argv>]
    [--scope project|user]            # 默认 project（.orca/config.json）
orca executor unset <profile> [field] # field ∈ binary|flags|prompt_channel|all（默认 all）
    [--scope project|user]
orca executor list                    # 列 profile + 标 * 哪个被 override
orca executor test <profile>          # 健康检查（保留）
```

**破坏性变更**：`set` 的 binary 从位置参数改为 `--binary`（三维护一）。旧 `set claude "ccr code"` → 新 `set claude --binary "ccr code"`。

## config schema（向后兼容）

```json
{
  "binaries":       {"opencode": "nga"},
  "flags":          {"opencode": ["run", "--format", "json"]},
  "prompt_channel": {"opencode": "argv"}
}
```

- 旧 `{binaries:{...}}` 继续生效（flags/prompt_channel 缺 = 该维无 override）。
- flags 规范存 **list**（JSON-natural）；`--flags` 输入串 shlex.split 成 list 存储；读取兼容 list|string。

## 优先级（per-profile per-field，多 fallback 生效只一份）

```
shell env (ORCA_<PROFILE>_CLI/_FLAGS/_PROMPT_CHANNEL)
  >  .orca/config.json   （项目，cwd 下，可 check-in）
  >  ~/.orca/config.json （用户）
  >  profile default
```

per-field project 覆盖 user（非整份替换）。

## 改动清单

| 文件 | 改动 |
|---|---|
| `orca/profiles/base.py` | +`prompt_channel_env` 字段 + `resolve_prompt_channel()`（镜像 `resolve_flags`，含 stdin/argv 双层校验回落）|
| `orca/profiles/builtin/{claude,opencode,ccr}.py` | 各 +`flags_env` +`prompt_channel_env`（接通死通道）|
| `orca/iface/cli/config.py` | 三字段 schema + 项目级 `.orca/config.json` + `load_merged_config`（per-field project 覆盖 user）+ `apply_config_env` 注入三字段 + `shell_env_snapshot`（首次注入前抓取，show 判 env 来源）+ flags list\|string 归一 |
| `orca/iface/cli/executor_cmds.py` | 重写 show（生效 argv + 来源）/ set（三维 + scope）/ unset（field）/ list |
| `orca/gates/dialog.py`、`orca/exec/validator.py`、`orca/exec/claude/executor.py` | surgical：`.flags`→`.resolve_flags()`、`.prompt_channel`→`.resolve_prompt_channel()` |
| `tests/iface/cli/test_executor_cmds.py`、`test_executor_e2e.py` | 适配新签名 + 新增：flags/prompt_channel 注入、项目覆盖用户、四态来源端到端、resolve_prompt_channel |

## 关键设计决策

1. **spawn 参数 vs 协议参数的线**：config 只覆盖 spawn 参数（binary/flags/prompt_channel/env_prefixes）；协议参数（translator/terminal/stream_format/capabilities）改了会破坏解析契约，走既有 `.orca/profiles/<name>.py` 整份覆盖。本次 scope 只开前三。
2. **env 注入桥保分层**：profiles 层不 import config；config 启动期 `setdefault` 注入 env，`resolve_*()` 运行时读 env（与既有 binary 机制同构）。`config_mod.` 前缀访问路径函数，让测试 `monkeypatch.setattr(config_mod,...)` 能生效（修了一个导入绑定陷阱）。
3. **shell env 快照**：首次 `apply_config_env` 前（注入前）抓 `ORCA_*` env 子集，供 show 区分「shell export」vs「config 注入」——注入后 os.environ 已污染，无法事后区分。快照放在 `apply_config_env` 开头（非 `bootstrap_config`），保证任何入口下都在首次注入前抓取，时序无关。
4. **set 回打不绕过 env 层**：review 建议回打绕过 env 层避免滞后，但**保留 env 层更诚实**——若用户 export 了 env，回打如实标 `← env` 并提示「config 写入被 env 覆盖」，比静默展示 config 值更符合 fail-loud（Rule 7 surface conflicts）。

## 自检 review 结论（code-reviewer）

- ✅ 依赖铁律完全遵守（config 只 import profiles.registry；executor_cmds 对 exec 延迟 import）。
- ✅ resolve_prompt_channel 放 profiles 层正确（与 resolve_flags 同构）。
- ✅ fail loud 双层校验（注入层 + resolve 层）。
- ✅ 向后兼容逐字节保住（无 override 时 resolve 返 default）。
- ✅ 接口单一性（三维统一在 set named option）。
- 已采纳加固：snapshot 捕获挪进 `apply_config_env`（时序无关）、补四态同字段端到端 show 测试、noop 测试加 env 断言。

## 验收

- targeted 套件 142 全绿（`test_executor_cmds` + `test_executor_e2e` + `tests/profiles/`）。
- 真实 CLI 烟测（隔离 tmp）全场景通过：default/项目/用户/env 三层来源、flags list 存储、unset 单字段/all、list 带 *、错误路径 exit code。
- 真实 config 文件无污染（修复了测试期导入绑定导致的污染，已清理）。
- 广义回归（exec/run/gates/compile/iface）1448 passed；3 failed 均为集成测试（`@pytest.mark.integration` 需真后端 / `orca mcp` 子进程 15s timing），与本改动代码路径无关。
