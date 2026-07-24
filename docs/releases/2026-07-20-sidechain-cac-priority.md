# Release: sidechain cac 优先 + `orca sidechain` 命令 + import 性能修复

> 日期: 2026-07-20 · 分支: in-session-unified-backend
> 计划: [`/home/mozzie/.claude/plans/vectorized-launching-river.md`](../../../home/mozzie/.claude/plans/vectorized-launching-river.md)

## 背景

CC 子 agent 过程拉取链(`cc_jsonl.CCJsonlAdapter` → `sidechain_daemon` → tape → 前端)的路径解析在
`resolve_cc_sidechain_root`,4 级优先级中「探测歧义」原默认走 `.claude`。用户希望:

1. **`~/.cac` 存在即默认走 cac**(无需手编 config);
2. **有 CLI 命令设置 `sidechain.family`**(此前只能手编 `~/.orca/config.json`)。

命令形态经确认为 `orca sidechain` sub-Typer(与 `executor`/`skill`/`install` 同模式)。本次只改 CC 家族
(cc/cac)为 cac 优先;opencode 家族(opencode/nga)保持不变(留 follow-up)。

## 改动

### 1. resolver cac 优先（`orca/events/adapters/_family.py`）

`resolve_cc_sidechain_root` 的 probe 分支由「单一存在走那个 / 两存歧义默认 cc」改为 **cac 优先**:

| 场景 | 结果（family / source） |
|---|---|
| 只 `.cac` 存在 | cac / probe |
| 只 `.claude` 存在 | cc / probe |
| 两存 | **cac / probe**（原 cc / probe-ambig） |
| 都不存在 | cc / default（不变，CC 官方原生路径） |

边界:「都无」保持 default `.claude`(用户装了 cac 才走 cac)。`detect_cc_existing_roots` docstring 术语
「歧义」→「两存」(cac 优先已消解)。opencode 分支 `resolve_opencode_db` **不动**。

### 2. `orca sidechain family` 命令（新 `orca/iface/in_session/sidechain_cmds.py`）

照搬 `executor_cmds.set` 的 config 写入模式(`load_config → setdefault → save_config`、`--scope project|user`、
写完回打生效值):

```
orca sidechain family cac            # 设为 cac（写 sidechain.family）
orca sidechain family                # 查看当前生效 family + resolved 路径 + source
orca sidechain family --unset        # 清除（回探测）
orca sidechain family cac --scope user   # 写用户级 ~/.orca/config.json
```

合法值取 `CC_FAMILY_DOTDIR | OPENCODE_FAMILY_DOTDIR` 并集 = `{cc, cac, opencode, nga}`(加新前端自动同步);
非法值 / 非法 scope → exit 2(fail loud)。依赖方向:只 import `iface.cli.config` + `events.adapters._family`,
**不 import `in_session.cli`**(否则与 cli 的 `add_typer` 成环);读 family 用 `config.sidechain_family`(与
`cli._read_sidechain_family_from_config` 共享,DRY)。`cli.py` 模块级 `app.add_typer(sidechain_app, name="sidechain")`。

doctor(`_check_sidechain_backend` CC 分支)同步:fam_eff 两存分支 `cc/probe-ambig` → `cac/probe`;hint 由
「歧义 + 建议设 config」改为信息性「两存(cac 优先);如需 .claude: `orca sidechain family cc`」。

### 3. `load_merged_config` 合并 sidechain（`orca/iface/cli/config.py`）—— 修既有 bug

原 `load_merged_config` 只对 `CONFIG_FIELDS`(binaries/flags/prompt_channel)做 project 覆盖 user 合并,
`sidechain` 作为未知 key **只从 user 透传** → 写 project 级 `sidechain.family` 读不到。新命令默认 scope=project
暴露了它。修复:`sidechain` 也做 project 覆盖 user 合并(与 spawn 维度同语义,两层都生效)。

同文件加 `sidechain_family(cfg)` 纯函数(单一读 family 源,`cli._read_sidechain_family_from_config` 与
`sidechain_cmds` 共享);`from orca.profiles.registry import ...` 改 lazy(移入 `apply_config_env`,profiles 仅此函数用)。

### 4. import 性能修复（`orca/iface/cli/__init__.py`）—— 修 daemon liveness 回归

新命令 `sidechain_cmds` import `orca.iface.cli.config`,触发 `orca/iface/cli/__init__.py` 原 eager
`from .app import OrcaApp` + `from .commands import main`(Textual TUI 重依赖),使 `import config` 从
~0.1s 飙到 ~4.4s、`import in_session.cli` 从 3.7s 到 5.9s → sidechain daemon 启动慢 → pidfile 迟写 →
liveness 测试 5s 超时误判 dead(改动前 baseline 全 pass,改动后 5 个 daemon e2e fail)。

修复:`__init__.py` 改 PEP 562 `__getattr__` lazy(`main`/`OrcaApp` 按需加载)。grep 确认无人经包顶层引用它们
(console_scripts `orca=...in_session.cli:main`、`tars=...cli.commands:main` 直指子模块)。修复后 config import
`4.4s → 0.08s`,cli import `5.9s → 1.2s`(比 baseline 3.7s 更快)。

## 测试

- `tests/events/test_adapters_family.py`:`test_cc_priority_probe_ambiguity_defaults_to_claude` → 反转为
  `..._both_defaults_to_cac`(两存 → cac/probe);文件头 docstring 同步。
- `tests/iface/in_session/test_in_session_v8.py`:`test_doctor_sidechain_backend_cc_ambiguity_hint` →
  `..._both_cac_preferred`(family=cac / source=probe / 两存提示 / `orca sidechain family cc` 指引)。
- 新 `tests/iface/in_session/test_sidechain_cmds.py`:set/show/unset/非法值/scope/idempotent unset。
- 验证集(`test_adapters_family` + `test_sidechain_cmds` + `test_in_session_v8` + `test_sidechain_daemon` +
  `test_executor_cmds`):**177 passed**,含此前 fail 的 5 个 daemon e2e。

## code-reviewer 自检

派 code-reviewer 验证(核心检查全 pass):依赖单向(`sidechain_cmds` 不 import `in_session.cli`)、
PEP 562 lazy(`textual` 不 eager 加载,访问 `OrcaApp` 才触发)、无循环 import、`load_merged_config`
合并边界(user/project/non-dict/extra keys)、star import、`sidechain_family` 边界。修 2 个 minor:
死 import `typing.Any`;unset 后空 `{"sidechain": {}}` 残留 → 整键清理(并增强 unset 测试断言)。

## 不做（follow-up）

- opencode/nga 家族优先级与 hint 对称化(保持 opencode 优先)。
- 「都无目录」的 default 改 cac(保持 `.claude`)。
- `host_session` 的 config fallback(YAGNI)。
- `_load_config_file` 对 `sidechain` 加载期格式校验:现仅 `CONFIG_FIELDS` 享 warn-drop 一致性校验,
  `sidechain` 非法格式(如写成字符串)由 `sidechain_family` 防御性返 None 兜底(行为正确),但缺加载期 warn 提示。

## 用法

```bash
# .cac 存在即自动走 cac（零配置），doctor 可验:
orca doctor   # 看 sidechain_backend check: family=cac (source=probe)

# 显式设置 / 查看 / 清除:
orca sidechain family cac
orca sidechain family
orca sidechain family --unset
```
