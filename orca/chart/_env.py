"""_env.py —— render_chart 调用前的确定性 run env 自加载（SPEC 2026-07-23 §3.1）。

回答「in-session 路径下子代理 bash 拿不到 ORCA_* env，怎么仍让 render_chart 跑通？」：
从调用方**已有**的产物路径（viz_struct 的 ``--ledger``、finalize step6 的 ``--champions``）
向上找 ``orca_env.sh``，**仅补 render_chart 强依赖的 4 个身份键**（ORCA_RUN_ID/NODE/SESSION_ID/
CHART_SOCK）进 ``os.environ``，让下游 ``render_chart`` 照常从 env 读（铁律 #2 不破：env 仍只从
env 读，本函数只负责在脚本层补 env）。

**确定性优先（deterministic over model-mediated）**：不靠 LLM「记得 source / 传 --env_file /
不拆调用」，改从已确定的输入（anchor 路径）派生 env 来源——把易错步骤从 LLM 手里拿走。

**单一标志 + 内容校验**：upward 搜索时不仅看 ``orca_env.sh`` 文件是否存在，还要求其内容含
``^export ORCA_CHART_SOCK=`` 行——避免用户项目根碰巧有同名 ``orca_env.sh`` 误匹配。不依赖
``chart_daemon.log``（daemon 产物，headless / 自定义 output_dir 无）。

**依赖**：仅 stdlib（``os``/``pathlib``/``re``/``shlex``）。与 ``_render.py`` 同属 light-touch
客户端，零 Orca runtime 依赖，``import`` 失败时调用方降级（见 viz_struct §3.2）。
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

# render_chart 强依赖的 4 个身份键（``_render._REQUIRED_ENV``）。
# 仅补这 4 个，不动 ORCA_ARTIFACTS_DIR / ORCA_KB_DIR（setup 节点 prompt 已处理其缺失回退）。
_RUN_ENV_KEYS = ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK")

# env 文件名（与 ``cli._write_orca_env`` 落盘约定一致）。
_ENV_FILENAME = "orca_env.sh"

# 单一标志 + 内容校验：内容必须含此 pattern 行才算本 run 的 env 文件。
# ``^export ORCA_CHART_SOCK=`` —— 与 ``_write_orca_env`` 写出的 ``shlex.quote(str(sock_path))`` 形态兼容
# （值非空即匹配 ``=`` 后非空，不约束值字符集，兼容 sock path 含 ``/`` ``-`` 等）。
_SOCK_MARK_PATTERN = re.compile(r"^export\s+ORCA_CHART_SOCK=", re.MULTILINE)

# export 行解析：``export K=V``，V 可被 shlex.quote 包成 ``'...'`` 或裸值。
_EXPORT_PATTERN = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_run_env_from_artifacts(anchor_path: str | Path) -> dict[str, str]:
    """从 anchor_path 向上找 ``orca_env.sh``，把 4 个 ORCA_* 身份键补进 ``os.environ``。

    SPEC 2026-07-23 §3.1 契约：

    - **幂等 no-op**：``os.environ`` 已含 ``ORCA_CHART_SOCK`` → 直接返 ``{}``（尊重已注 env，
      真 orca-run / ClaudeExecutor spawn 路径零影响；4 键同源同注假设，half-injection 视为异常
      配置本 SPEC 不解）。
    - **向上搜**：从 anchor_path 所在目录起，逐级向父走，找**首个**祖先目录含 ``orca_env.sh``
      **且文件内容匹配 ``^export ORCA_CHART_SOCK=`` 行**（单一标志 + 内容校验，避免同名文件误匹配）。
      找到 → 解析 ``export K=V`` 行，**仅** 4 键写进 ``os.environ``（已存在的 env 不覆盖——显式
      spawn 的 env 优先），返已补键集。
    - **找不到**（非 orca-run / headless / 自定义 output_dir / env 文件存在但无 SOCK 行）
      → 返 ``{}`` 不补（调用方按 env_missing 走 sidecar 不阻断路径）。

    Args:
        anchor_path: 任一 run 内产物路径（ledger / champions / snapshot 等）。锚点不假设 ledger
            必在 ``<run_id>/artifacts/`` 下（防自定义 output_dir / 回退 llm_artifacts）；从 anchor
            向上走到首个匹配的 run_dir 为止。

    Returns:
        本次**实际补进** ``os.environ`` 的键值 dict（仅含本次新补、原本不在 env 中的键）。
        幂等 no-op 或找不到 → 空 dict。调用方据返 dict 是否含 ``ORCA_CHART_SOCK`` 判自加载是否成功
        （SPEC §3.2 ``viz_env_status=env_loaded_from_file`` 的判据）。
    """
    # 幂等短路：env 已含 SOCK → 视为已注，不动。
    if os.environ.get("ORCA_CHART_SOCK"):
        return {}

    env_file = _find_run_env_file(anchor_path)
    if env_file is None:
        return {}

    # 读 env 文件并解析 export 行。文件读失败（IO 错 / 编码错）→ 保守返 {}，让调用方走 env_missing
    # 路径（viz 是 sidecar，不阻断主循环）。
    try:
        content = env_file.read_text(encoding="utf-8")
    except OSError:
        return {}

    injected: dict[str, str] = {}
    for line in content.splitlines():
        m = _EXPORT_PATTERN.match(line)
        if not m:
            continue
        key, raw_val = m.group(1), m.group(2).strip()
        if key not in _RUN_ENV_KEYS:
            continue
        # 已存在 env 不覆盖（显式 spawn env 优先；防本进程外层已注入更新值被覆盖）。
        if os.environ.get(key):
            continue
        # shlex.split 解 ``'value with space'`` / ``plain`` 两种形态。
        # 失败（畸形引号）→ 保守跳过该键（不写脏 env）；其余键照常补。
        try:
            parts = shlex.split(raw_val, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        value = parts[0]
        os.environ[key] = value
        injected[key] = value

    return injected


def _find_run_env_file(anchor_path: str | Path) -> Path | None:
    """从 anchor_path 向上找首个祖先目录含 ``orca_env.sh`` 且内容匹配 SOCK 标志行。

    SPEC §3.1：单一标志 + 内容校验。防用户项目根碰巧有同名 ``orca_env.sh`` 误匹配。
    """
    # anchor_path 可能是文件（ledger.jsonl）或目录；取其所在目录起向上搜。
    p = Path(anchor_path)
    if p.is_file():
        start = p.parent
    else:
        start = p

    try:
        parents = [start, *start.parents]
    except OSError:
        return None

    for d in parents:
        candidate = d / _ENV_FILENAME
        if not candidate.is_file():
            continue
        # 内容校验：含 ``^export ORCA_CHART_SOCK=`` 行才算本 run 的 env 文件。
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if _SOCK_MARK_PATTERN.search(text):
            return candidate
    return None
