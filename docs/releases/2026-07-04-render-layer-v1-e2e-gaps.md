# phase-15 render layer v1 —— e2e gaps 闭环

> 接续 [2026-07-04-render-layer-v1.md](./2026-07-04-render-layer-v1.md)。phase-15 v1 合入前
> test-coverage-e2e 真跑发现 2 个用户可见的视觉异常，本 release note 闭环修复。

---

## 背景

phase-15 v1（commit `ae0126b` + `edd738f`）走 spec → 实现 → 1327 passed 自验证合并。
合入前用户跑 `examples/demo_task.yaml`（"read the file pyproject.toml"）真跑验证，
发现 2 个 gap（test-coverage-e2e 报告）：

- **GAP #1（P1）**：opencode `read` 一个**文件**时，result 同样是 XML envelope
  （`<path>...</path>\n<type>file</type>\n<content>...</content>`），与目录 envelope 同形。
  原 `_normalize_file_read` 只检测 `<type>directory</type>`，file 走兜底 `_line_numbered`，
  导致：
    1. 前 3 行被当文件内容渲染：`<path>...</path>` / `<type>file</type>` / `<content>`
    2. opencode 自带的 `N:` 行号前缀 + Rich `Syntax(line_numbers=True)` → **双重行号**
    3. 末尾 `(End of file - total N lines)` + `</content>` 也被当内容渲染
- **GAP #2（P2）**：`_make_subtitle` 对 `file_write` 返回空串。spec §8.1 file_write
  header 应是 `✏ <path> (new, <bytes>B)`。

## 改动

### `orca/iface/cli/widgets/tool_render/normalize.py`

- 抽统一 helper `_parse_opencode_xml_envelope(text) -> {type, path, entries?|content?} | None`
  （DRY：directory + file 共享同一 envelope 解析器）
- `_parse_opencode_dir_entries_body` + `_strip_opencode_file_content` 分别处理两类 body
- `_strip_opencode_file_content` 剥三层 opencode 自加修饰：
    1. envelope 起手换行（`<content>\n<file line 1>`）
    2. 每行 `N: ` 行号前缀（避免与 Rich Syntax 双重行号）
    3. 尾部 `(End of file - total N lines)` marker + envelope 收尾换行
- `_normalize_file_read`：仅在 `text.lstrip().startswith("<path>")` 时尝试 XML 解析
  （避免把 claude Read 的普通 HTML/XML 文件原文误判）
- fail visible（§13）：envelope 解析失败 / `<type>` 已解析但取值未知 / 缺 entries/content
  → warning log + 降级原文（不 raise）
- `_make_subtitle` 加 `file_write` 分支：`return f"new, {payload.get('bytes', 0)}B"`

### `docs/specs/render-layer-design-draft.md` §6.3 订正

原 "opencode `read` 文件：同 claude（result 为文件内容文本）" 与实测不符。订正为：

> opencode `read` 文件：**同为 XML envelope**（实测 shape 见下），用
> `_parse_opencode_xml_envelope` 提取 `<content>` 内文本，**剥掉 opencode 自加的
> `N:` 行号前缀**和尾部 `(End of file - total N lines)` marker，再行号化为 `content`。
>
> envelope 检测：仅在 `result.lstrip().startswith("<path>")` 时尝试，避免 claude Read
> 普通 XML/HTML 文件误判。XML 解析失败 → fail visible（§13）。

并补 envelope shape 样本（tape 证据 `runs/demo_task-20260704-085641-f15c8d.jsonl` seq=5）。

### fixtures + 测试

- `tests/e2e_phase15/_artifacts/render_tool_cases.json`：
    - 新增 case `opencode_read_file_xml`：opencode read 文件 envelope 输入 → 期望
      剥 envelope + 无 `N:` 前缀的 content payload（content_len / first_line 字面断言）
    - 修 `claude_write_new_file` 期望 subtitle：`""` → `"new, 30B"`（GAP#2 闭环）
- `tests/iface/cli/test_tool_render.py` 新增 3 类共 5 个测试：
    - `TestOpencodeReadFileEnvelope`：
        - `test_opencode_read_file_strips_envelope`：剥 envelope（无 tag / `N:` 前缀 / EOF marker）
        - `test_opencode_read_file_preserves_line_count`：行数守恒（剥前缀后行数 == 原文件行数）
        - `test_opencode_read_file_tape_evidence`：真实 tape `runs/demo_task-20260704-085641-f15c8d.jsonl`
          seq=5 回归（72 行 TOML 干净渲染；防 opencode 升级漂移）
    - `TestFileWriteSubtitle`：
        - `test_file_write_subtitle_new_bytes`：file_write payload → `new, NB`
        - `test_file_write_subtitle_zero_bytes`：空文件 → `new, 0B`（边界）

## 验证

- `pytest tests/iface/cli/test_tool_render.py -v`：38 passed（baseline 32 + 6 新增）
- `pytest tests/ -q --ignore=tests/e2e_mxint --ignore=tests/e2e_phase14`：
  **1333 passed** 0 failed（baseline 1327 + 6 新增；30 skipped 含 playwright 未装）
- **真跑验证**（用户视角，证明 GAP#1 修好）：
    - 读真实 tape `runs/demo_task-20260704-085641-f15c8d.jsonl` seq=5 → normalize → render_tool
    - 渲染结果（SVG `/tmp/gap1_opencode_read_file.svg`）：72 行干净 TOML，单重 Rich 行号
      （1..72 连续），无 `<path>` / `<type>file</type>` / `<content>` tag，无 `(End of file`
      marker，无 `N:` 双重行号
- **GAP#2 验证**：构造 `Write(file_path=new_module.py, content=<30 bytes>)` → header
  `✏ new_module.py (new, 30B)`（SVG `/tmp/gap2_file_write_subtitle.svg`）

## Commit

- `900fcfd` —— `fix(render): phase-15 e2e gaps (opencode read file envelope + file_write subtitle)`

## 非目标（surgical）

不动：translate 层、canonical Event schema、其他 kind normalizer、kinds.py renderer、
log_stream/node_detail 改造、phase-14 agent 池、并行进程持有文件（profiles/builtin/* +
terminal.py + gates/dialog.py + exec/validator.py + executor_cmds.py + config.py +
examples/demo_task.yaml + tests/e2e_mxint/）。
