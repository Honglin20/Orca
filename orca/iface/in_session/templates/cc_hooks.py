"""cc_hooks.py —— CC（Claude Code）settings.json hook 脚本片段生成（SPEC §2.4.1）。

回答「CC 的 PostToolUse(Task) 与 Stop 是两个独立 hook 进程，output 怎么跨进程传递？」
——经**文件**（cache）。``orca in-session start`` 生成两段 shell 脚本片段贴进
``.claude/settings.json`` 的 ``hooks`` 字段：
  - **PostToolUse(Task|Agent)**：提 ``tool_response.content`` flatten → 覆盖写
    ``<rundir>/orca-output-<run_id>.txt``（last-write-wins，多 Task/turn 不拼）。
  - **Stop**：``[ -f cache ] && args+=(--output ...)`` → spawn ``orca in-session next``
    → 读 stdout JSON → ``{"done":false,prompt}`` ⇒ ``decision:block, reason:prompt``；
    ``{"done":true}`` ⇒ 放行。删 cache（一次性）。

**安全契约**（spec-review r3 闭环 B-1/B-2/B-7）：
  - shell 用 **bash 数组 + ``"${ARGS[@]}"``** 传 argv（避免 word-splitting；cache 含
    空格/换行是常态）。
  - ``decision:block`` 的 JSON 用 **``jq -n --arg p "$PROMPT"``** 构造（prompt 含换行/
    引号/反斜杠是常态，字符串拼接会产非法 JSON）。
  - PostToolUse 的 tmp 文件用 ``trap rm EXIT`` 兜底清理（jq 失败也清，不泄漏）。

**铁律**（D-v7-1）：本模块只生成 spawn CLI + parse JSON 顶层字段的 shell 脚本，零 Orca
业务逻辑（无 advance/router/replay/tape 路径）。合规计数由 CLI 在无 ``--output`` 时
自处理（CLI 把 ``""`` normalize 为 None，B2）。
"""

from __future__ import annotations

from pathlib import Path


def _rundir_from_tape(tape_path: str) -> str:
    """cache 路径 = 与 tape 同目录。返 shell 安全的绝对路径目录。"""
    return str(Path(tape_path).parent)


def render_cc_settings_fragment(
    *, run_id: str, tape_path: str, yaml_path: str, model: str,
) -> dict:
    """构造贴入 ``.claude/settings.json`` 的 ``hooks`` 片段。

    返回 ``{"hooks": {"Stop": [...], "PostToolUse": [...]}}`` 结构，用户把整段 merge
    进自己的 settings.json（已有 hooks 时合并数组）。

    cache 路径：``<rundir>/orca-output-<run_id>.txt``（SPEC §2.4.1）。
    """
    rundir = _rundir_from_tape(tape_path)
    cache = f"{rundir}/orca-output-{run_id}.txt"

    # PostToolUse(Task|Agent)：flatten tool_response.content → 覆盖写 cache（last-write-wins）。
    # bash heredoc 拿 stdin JSON；jq 提 text blocks。
    posttooluse_script = _posttooluse_script(cache)
    # Stop：读 cache（存在则 --output，否则省略 argv → CLI branch 4 + 合规计数）；
    # spawn next → 解析 JSON → decision:block 放 prompt / 放行；最后删 cache。
    stop_script = _stop_script(tape_path=tape_path, run_id=run_id, cache=cache)

    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Task|Agent",
                    "hooks": [
                        {
                            "type": "command",
                            "command": posttooluse_script,
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": stop_script,
                        }
                    ],
                }
            ],
        }
    }


def _posttooluse_script(cache: str) -> str:
    """PostToolUse(Task|Agent) → 覆盖写 cache。

    从 stdin 读 CC hook JSON（``{tool_response:{content:[{type:text,text}]}}``），
    flatten text blocks → ``write(tmp) + mv`` 原子覆盖 cache。Task 非当前 Orca 节点
    （marker inactive）也写——守门在 CLI 侧（CLI 无 marker 时返 no-marker，passthrough）。

    安全 / 清理（B-7）：
      - ``trap 'rm -f "$TMP"' EXIT`` 兜底清 tmp（jq 失败 / pipe broken 也清）。
      - ``set -euo pipefail``（与 _stop_script 对称）。
    """
    return (
        'set -euo pipefail; '
        f'TMP={cache}.tmp.$$; '
        f'trap \'rm -f "$TMP"\' EXIT; '
        'jq -r \'.tool_response.content // [] '
        '| map(select(.type == "text") | .text) | join("\\n")\' > "$TMP"; '
        f'mv -f "$TMP" {cache}'
    )


def _stop_script(*, tape_path: str, run_id: str, cache: str) -> str:
    """Stop → 读 cache → spawn next → 解析 JSON → decision block/pass；删 cache。

    ``[ -f cache ] && args+=(--output "$(cat cache)")``：cache 不存在则省略 argv
    （SPEC §2.4.1）→ CLI ``output=None`` → branch 4 + 合规计数。

    安全 / 正确性（B-1/B-2）：
      - **bash 数组 + ``"${ARGS[@]}"``**：cache 含空格/换行/特殊字符不破坏 argv
        （word-splitting 不会把 output 内容拆成多 token）。
      - ``decision:block`` 的 JSON 用 ``jq -n --arg p "$PROMPT"`` 构造，prompt 含换行 /
        引号 / 反斜杠也产合法 JSON。
    """
    return (
        'set -euo pipefail; '
        f'ARGS=(--tape {tape_path} --run-id {run_id}); '
        f'if [ -f {cache} ]; then ARGS+=(--output "$(cat {cache})"); fi; '
        'OUT=$(orca in-session next "${ARGS[@]}"); '
        f'rm -f {cache}; '
        'DONE=$(echo "$OUT" | jq -r \'.done // false\'); '
        'if [ "$DONE" = "true" ]; then '
        '  exit 0; '
        'else '
        '  PROMPT=$(echo "$OUT" | jq -r \'.prompt // empty\'); '
        '  if [ -n "$PROMPT" ]; then '
        '    jq -n --arg p "$PROMPT" \'{decision:"block", reason:$p}\'; '
        '  fi; '
        'fi'
    )
