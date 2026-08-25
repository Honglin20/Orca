"""orca/iface/in_session/_artifacts.py —— project-scoped artifacts 派生（SSOT）。

回答「in-session 路径的 ``$ORCA_ARTIFACTS_DIR`` 从哪派生？」：从 tape 唯一真相源读
``workflow_name`` + ``inputs``（SPEC 2026-08-06 §2.1）——inputs 含非空**绝对**
``project_root`` → ``<project_root>/artifacts/<wf_name>/``（project-scoped，跨 run
复用），否则 per-run ``runs/<run_id>/artifacts/`` 回落（向后兼容）。

2026-08-26 下沉自 ``cli.py``（逐字搬移，公开名去下划线）：in-session script 节点
spawn env（``_step_io.execute_script_inline``）需要同一派生——agent 节点
``orca_env.sh`` 写入、bootstrap mkdir、next 重写之外的**第四个消费者接同一真相源**
（plan 2026-08-25 prof-opt-v4 §10.2，D-1 修复），禁复制。``cli.py`` re-import
私有名 alias 保既有调用点 / 测试 import 路径零改。
"""

from __future__ import annotations

import json
from pathlib import Path

from orca.chart._paths import artifacts_dir_for_run


def read_workflow_name(tape_path: Path) -> str | None:
    """读 tape 首条 ``workflow_started.data.workflow_name``（m12 重复 bootstrap 检测用）。

    tape 不存在 / 无 workflow_started / 损坏 → None（调用方按「无信息」跳过，不崩）。
    扫前 ``TAPE_HEAD_SCAN_LIMIT`` 行找不到 workflow_started 即放弃（workflow_started
    正常是首条事件；corrupt tape 截掉首行时不读整个大文件，review m5）。
    """
    if not tape_path.is_file():
        return None
    try:
        with open(tape_path, encoding="utf-8") as f:
            for _i, line in enumerate(f):
                if _i >= TAPE_HEAD_SCAN_LIMIT:
                    return None
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if obj.get("type") == "workflow_started":
                    return obj.get("data", {}).get("workflow_name")
    except (json.JSONDecodeError, OSError):
        return None
    return None


def read_workflow_inputs(tape_path: Path) -> dict:
    """读 tape 首条 ``workflow_started.data.inputs``（project-scoped artifacts 解析用）。

    镜像 ``read_workflow_name`` 的 tape 头扫描骨架（同一 ``workflow_started`` 事件）。
    无 ws / 损坏 / 无 inputs → ``{}``（调用方按「无 project_root」回落 per-run，不崩）。

    本地 helper 而非 import ``orca.events.replay._replay_fold``（私有）
    或既有 ``inputs_from_tape``（已删，E5）——SPEC 2026-08-06 §2.1：next 路径 ``--inputs``
    默认 ``"{}"`` 拿不到原始 inputs，bootstrap/next 两处统一从 tape 读为单一真相源。
    """
    if not tape_path.is_file():
        return {}
    try:
        with open(tape_path, encoding="utf-8") as f:
            for _i, line in enumerate(f):
                if _i >= TAPE_HEAD_SCAN_LIMIT:
                    return {}
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if obj.get("type") == "workflow_started":
                    inputs = obj.get("data", {}).get("inputs")
                    return inputs if isinstance(inputs, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
    return {}


def resolve_artifacts_dir(
    tape_path: Path, run_id: str,
) -> tuple[Path, bool]:
    """SPEC 2026-08-06 §2.1：派生 ``$ORCA_ARTIFACTS_DIR``（in-session 入口）。

    返回 ``(artifacts_dir, is_project_scoped)``：
      - workflow 的 inputs 含非空**绝对** ``project_root`` + 有 wf_name →
        ``<project_root>/artifacts/<workflow_name>/``，``is_project_scoped=True``。
      - 否则 → 既有 per-run ``runs/<run_id>/artifacts/``（向后兼容，旧 workflow 零回归），
        ``is_project_scoped=False``。

    fail loud：``project_root`` 给了但非绝对 → raise（防相对路径跨 run 漂移；
    bootstrap 起点暴露，不静默 ``.resolve()`` 出错位路径）。

    ``wf_name`` + ``inputs`` 都从 tape 读（bootstrap 与 ``next`` 两处统一，单一真相源；
    ``next --inputs`` 默认 ``"{}"`` 不反映真实 inputs，tape 是唯一可信源）。

    **Rule 7 surfacing**：SPEC §2.1 示例签名是 ``-> Path``，本实现偏离为 ``tuple[Path, bool]``。
    理由：SPEC 同段要求 project-scoped mkdir 失败 fail loud 区别于 per-run fail-open，
    调用方（bootstrap）需要 discriminator 区分两种路径性质；path 字面比对（``path !=
    artifacts_dir_for_run(...).resolve()``）会耦合 per-run 路径布局更脆。tuple 是最小返回契约。
    """
    wf_name = read_workflow_name(tape_path)
    inputs = read_workflow_inputs(tape_path)
    proj = (inputs or {}).get("project_root", "")
    if proj and wf_name:
        p = Path(proj)
        if not p.is_absolute():
            raise ValueError(
                f"project_root 必须绝对路径：{proj!r}（workflow={wf_name!r}）"
            )
        return (p / "artifacts" / wf_name).resolve(), True
    return artifacts_dir_for_run(tape_path.parent, run_id).resolve(), False


# tape 头扫描行数上限（workflow_started 正常是首条；超此仍无即放弃，防读大文件）。
TAPE_HEAD_SCAN_LIMIT = 100
