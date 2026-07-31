"""setup_helpers.py —— kd-setup 步骤 5/6 的确定性后端（rule 5：确定性逻辑用代码）。

[DEPRECATED v4] 本模块的两个 CLI 子命令（``find-teacher-ckpt`` / ``grep-user-train``）自 v4
嵌入（2026-07-31）起**不再被 active path 调用**：
  - ``find-teacher-ckpt`` 被 ``train_pipeline.py --mode teacher``（固定 ``--out_ckpt``）取代；
  - ``grep-user-train`` 被 ``train-script-gen``（生成 train_pipeline.py 时搬用户 loss/dataloader）取代。
保留本文件 + 其单元测试（``test_setup_helpers_*``）作历史参考 + 可复用 AST/scan 工具；
未来若无外部消费者，可整体移到 ``_deprecated/``。

回应用户反馈 R4：原 ``kd-setup/agent.md`` step5 把「teacher_train_command 产的 ckpt 路径」
和 step6「用户 train.py 的 loss/dataloader dotted-path」留给 LLM grep / 字符串拼，违反
rule 5（确定性逻辑用代码）。本模块把这两段下沉为可 import 的纯函数，agent.md 只调脚本。

两个入口（CLI 子命令 ``find-teacher-ckpt`` / ``grep-user-train``，stdout emit ``KEY: value``）：

  - **find-teacher-ckpt**：解析 teacher_train_command 的 ``--out <path>``（首选，无歧义）；
    失败则扫 ``project_root`` 下最新 ``.pt/.ckpt``（排除 ``teacher_cache.pt`` /
    ``teacher_meta.json`` / ``gate_manifest.json`` / ``ledger.jsonl`` / 已存在的 ckpts/）。
    找到 → 拷到 ``--target``（如 ``$TEACHER_CKPT``）；找不到 → exit 2 fail loud。
  - **grep-user-train**：在 ``project_root`` 找用户 train.py（候选名 ``train.py`` /
    ``trainer.py`` / ``task_train.py`` / ``<--train-module>``），AST 解析找：
      * loss callable：``def compute_loss`` / ``def <name>.*loss`` / ``loss = nn.XLoss(...)``
        / ``criterion = nn.XLoss(...)``；
      * dataloader dotted-path：``train.py`` 自身（``user_train_import = <abs train.py>``）。
    抽到 → emit ``USER_TRAIN_IMPORT`` + ``USER_LOSS_FN``；抽不到 → emit ask-user 哨兵 JSON
    （粘已 grep 过的候选模式，不编造）。

CLI::

    python3 setup_helpers.py find-teacher-ckpt \\
      --project_root <abs> --train_command "<cmd>" --target <abs TEACHER_CKPT>

    python3 setup_helpers.py grep-user-train \\
      --project_root <abs> [--train_command "<cmd>"] [--baseline_model_path <abs>]

stdout（find-teacher-ckpt）::

    TEACHER_CKPT: <abs path>     # 拷贝后的目标 ckpt（即 --target）
    TEACHER_CKPT_SRC: <abs path> # 原始 ckpt 来源（--out 或扫描结果，诊断用）

stdout（grep-user-train）::

    USER_TRAIN_IMPORT: <abs train.py 或空>
    USER_LOSS_FN: <callable name 或空>
    USER_TRAIN_SENTINEL: <ask-user JSON 或空>  # 抽不到时填，agent 须原样上抛

fail loud：CLI 输入不符（project_root 不存在 / find-teacher-ckpt 扫不到任何 ckpt）→ exit 2。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# 排除：扫描 project_root 最新 ckpt 时忽略的文件名模式（避免把 teacher_cache / 历史
# ckpt 当成新训 teacher ckpt）。teacher_cache.pt 是 teacher_setup 自己产的，不是
# teacher_train_command 的产物——若不排除会把上一轮的 cache 当本轮 ckpt，teacher_meta
# 哈希校验失败但已被拷贝走，污染 setup。
_EXCLUDE_NAMES = {
    "teacher_cache.pt", "teacher_meta.json", "gate_manifest.json",
    "ledger.jsonl", "orca.lock",
}
# 排除目录：这些目录下的 .pt/.ckpt 不算 teacher_train_command 产物（是 orca 自己的
# artifact / 用户既有 ckpts）。
_EXCLUDE_DIR_PARTS = {"kd-nas-artifacts", "ckpts", ".git", "__pycache__", "node_modules"}

# 扫描时**硬剪枝**的目录名（rglob/os.walk 不进入）。这些目录体积大且不可能含用户
# train.py / teacher ckpt（venv 的 site-packages 单独可破十万文件；llm_artifacts 含
# 多份 git worktree 副本，重复扫一遍巨慢）。**关键**：在 Orca 仓库自身做 demo 跑时
# （project_root=Orca repo），不剪枝会让 setup_helpers grep-user-train > 30s 超时。
_PRUNE_DIRS = {
    ".venv", "venv", "env", ".env",  # Python 虚拟环境
    "__pycache__", ".git", ".hg", ".svn",  # VCS + 缓存
    "node_modules", ".next", "dist", "build", "target",  # JS / Rust / Go 构建
    "site-packages",  # 兜底（即便嵌在非标位置）
    ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache",  # Python 工具缓存
    "llm_artifacts",  # Orca 自家多 worktree 副本（agent-struct-exploration 产出）
}

# loss callable 候选命名模式（grep-user-train AST 解析用）。
# 优先级：``def compute_loss`` > ``def <name>`` 其中 name 含 ``loss``。
_LOSS_FN_NAMES = ("compute_loss", "task_loss", "kd_loss", "train_loss")

# train.py 候选文件名（grep-user-train 用）。
_TRAIN_CANDIDATES = ("train.py", "trainer.py", "task_train.py", "training.py")


def _parse_out_from_command(train_command: str) -> str | None:
    """从 teacher_train_command 解析 ``--out <path>`` / ``--out=<path>``。

    用户训练脚本约定（见 demo train_teacher.py）：``--out <ckpt_path>``。无歧义首选。
    命令里没有 ``--out`` → None（fallback 到扫描）。
    """
    if not train_command:
        return None
    tokens = train_command.split()
    for i, tok in enumerate(tokens):
        if tok == "--out" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--out="):
            return tok[len("--out="):]
        # 常见别名（demo 早期用 --output）
        if tok in ("--output", "--ckpt", "--ckpt-path", "--ckpt_path") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(("--output=", "--ckpt=", "--ckpt-path=", "--ckpt_path=")):
            return tok.split("=", 1)[1]
    return None


def _walk_with_prune(project_root: Path):
    """``os.walk`` + 硬剪枝 ``_PRUNE_DIRS``（避免扫 venv/site-packages 巨慢）。

    ``Path.rglob`` 在 Python 3.12 对 33k+ 文件的 repo（含 .venv/site-packages）会卡 >30s
    （WSL2 Windows mount 雪上加霜）。剪掉 venv / .git / node_modules / llm_artifacts 等
    不可能含用户 train.py / teacher ckpt 的目录后 < 1s。

    yield ``(root_str, rel_dir_parts_tuple, filename)``——不用 ``Path`` 对象避免 stat
    调用（WSL2 + Windows mount 上 stat 巨慢，2943 文件能跑 6s）。caller 按需 ``Path(root)/f``。
    """
    root_str = str(project_root)
    for root, dirs, files in os.walk(root_str):
        # 原地改 dirs 剪枝（os.walk 文档推荐的 prune 方式）
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        # rel_dir_parts：相对于 project_root 的目录段（用 str split 比 Path.relative_to 快）
        if root == root_str:
            rel_parts: tuple[str, ...] = ()
        elif root.startswith(root_str + os.sep):
            rel_parts = tuple(root[len(root_str) + 1:].split(os.sep))
        else:
            rel_parts = ()  # 不在 project_root 下（不应发生），保守当作 root
        for f in files:
            yield root, rel_parts, f


def _is_excluded_relpath(rel_parts: tuple[str, ...], filename: str) -> bool:
    """是否在排除目录段内 / 是排除文件名。纯字符串比较，无 stat 调用（WSL2 友好）。"""
    if filename in _EXCLUDE_NAMES:
        return True
    for part in rel_parts:
        if part in _EXCLUDE_DIR_PARTS:
            return True
    return False


def find_teacher_ckpt(
    project_root: str, train_command: str, target: str,
) -> tuple[str, str]:
    """解析 teacher_train_command 产物 → 拷到 ``target``。返回 ``(target_abs, src_abs)``。

    优先级：``--out <path>``（无歧义）→ 扫 ``project_root`` 最新 ``.pt/.ckpt/.pth``。

    raises:
      FileNotFoundError: 解析不到 + 扫描无候选 → 让 caller exit 2 fail loud。
    """
    pr = Path(project_root).resolve()
    if not pr.is_dir():
        raise FileNotFoundError(f"project_root 不存在或非目录：{pr}")

    src: Path | None = None
    out_hint = _parse_out_from_command(train_command)
    if out_hint:
        # ``--out`` 可能是相对 project_root 的路径（用户命令 cwd=project_root）。
        cand = Path(out_hint)
        if not cand.is_absolute():
            cand = pr / cand
        if cand.is_file():
            src = cand.resolve()

    if src is None:
        # 扫 project_root 最新 .pt/.ckpt/.pth（按 mtime 倒序）
        candidates: list[Path] = []
        for root, rel_parts, fname in _walk_with_prune(pr):
            if fname.endswith((".pt", ".ckpt", ".pth")) and not _is_excluded_relpath(rel_parts, fname):
                candidates.append(Path(root) / fname)
        if not candidates:
            raise FileNotFoundError(
                f"teacher_train_command 无 --out，且 project_root={pr} 下无 .pt/.ckpt/.pth "
                f"候选（已排除 {_EXCLUDE_DIR_PARTS} / {_EXCLUDE_NAMES}）。请确认 teacher_train_command "
                f"产 ckpt 的路径，或在其命令加 --out <path>。"
            )
        # 最新 mtime（确定性 tiebreak：相同 mtime 时按路径字典序，避免文件系统抖动选不同文件）
        candidates.sort(key=lambda p: (-p.stat().st_mtime, str(p)))
        src = candidates[0].resolve()

    target_path = Path(target).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # 拷贝（不是 symlink：teacher_setup 后续读 ckpt 加载 state_dict，文件存在即可；
    # 拷贝让源 ckpt 可被用户脚本清理而不影响 setup）
    shutil.copyfile(str(src), str(target_path))
    return str(target_path), str(src)


# ── grep-user-train：AST 解析用户 train.py 找 loss/dataloader ──────────────────


def _find_train_py(project_root: str, train_command: str) -> Path | None:
    """在 project_root 找用户 train.py（候选文件名 + train_command 出现的 .py 作为补充候选）。

    语义辨析：``teacher_train_command`` 跑的是 ``train_teacher.py``（teacher 训练脚本），
    **不是**用户 train.py（student KD 消费的 loss+dataloader）；故 train_command 仅作
    ``project_root`` 定位线索，**不**把它出现的 .py 当 student train.py 优先选。
    """
    pr = Path(project_root).resolve()
    # 1) 候选名（train.py / trainer.py / task_train.py / training.py）—— mTime 倒序、tiebreak 字典序
    # 用 _walk_with_prune 而非 rglob：venv/site-packages 会让 rglob 卡 >30s（实测）。
    candidates: list[Path] = []
    candidate_names = set(_TRAIN_CANDIDATES)
    for root, rel_parts, fname in _walk_with_prune(pr):
        if fname in candidate_names and not _is_excluded_relpath(rel_parts, fname):
            candidates.append(Path(root) / fname)
    if not candidates:
        return None
    candidates.sort(key=lambda p: (-p.stat().st_mtime, str(p)))
    return candidates[0].resolve()


def _extract_loss_fn_from_ast(tree: ast.AST) -> str | None:
    """AST 解析找 loss callable 名。

    优先级：
      1. ``def compute_loss`` / ``def task_loss`` / ... （明确 loss 命名）；
      2. ``def <name>`` 其中 name 含 ``loss``（小写）；
      3. ``criterion = nn.XLoss(...)`` / ``loss = nn.XLoss(...)`` （注：返回的是变量名，
         不是 callable，但 train_adapter 只 import module 取 attr，所以**不**走这个分支——
         loss_fn 必须是 module 级 callable 名，故只走 1/2）。
    """
    # 1. def compute_loss / def task_loss / ...
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in _LOSS_FN_NAMES:
                return node.name
    # 2. def <name> 含 loss（小写）
    loss_funcs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "loss" in node.name.lower():
            # 排除明显非 loss callable 的（如 ``def compute_kd_loss`` 也算 loss callable）
            loss_funcs.append(node.name)
    if loss_funcs:
        # 多候选时选最短名（最贴近 "loss"），稳定 tiebreak
        loss_funcs.sort(key=lambda n: (len(n), n))
        return loss_funcs[0]
    return None


def _ask_user_sentinel(patterns_seen: list[str], project_root: str) -> dict[str, Any]:
    """构造 ask-user 哨兵 JSON（抽不到 loss fn 时 agent 原样上抛，**不编造**）。"""
    return {
        "_orca_ask_user": (
            "在你项目 train.py 里训练 loss 的 callable 名是什么？"
            "（已扫描但未抽到，请贴 dotted-path 或函数定义片段）"
        ),
        "options": patterns_seen or ["def compute_loss(...):", "criterion = nn.MSELoss()"],
        "context": f"已扫 project_root={project_root} 下 train.py 候选，AST 解析未命中 loss callable",
        "_sentinel": "orca_ask_user_v1",
    }


def grep_user_train(
    project_root: str, train_command: str = "", baseline_model_path: str = "",
) -> tuple[str, str, dict[str, Any] | None]:
    """找用户 train.py + 解析 loss fn。返回 ``(train_import, loss_fn, sentinel_or_None)``。

    - 找到 train.py + loss fn → ``(abs_train_py, loss_fn_name, None)``；
    - 找不到 train.py / 抽不到 loss fn → ``("", "", sentinel)``（agent 原样上抛）。
    """
    pr = Path(project_root).resolve()
    train_py = _find_train_py(pr, train_command)
    if train_py is None:
        return "", "", _ask_user_sentinel(
            [f"未在 {pr} 下找到 train.py / trainer.py / task_train.py 候选"], str(pr),
        )

    try:
        tree = ast.parse(train_py.read_text(encoding="utf-8"))
    except (SyntaxError, OSError) as e:
        return "", "", _ask_user_sentinel(
            [f"train_py={train_py} AST parse 失败：{type(e).__name__}: {e}"], str(pr),
        )

    loss_fn = _extract_loss_fn_from_ast(tree)
    if not loss_fn:
        # 收集 AST 里所有 funcdef 名作 patterns_seen，让用户能从候选里挑（不编造）
        func_names = [
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
            and not n.name.startswith("_")
        ][:8]
        return "", "", _ask_user_sentinel(
            [f"train.py={train_py} 函数候选：{func_names}"], str(pr),
        )

    return str(train_py), loss_fn, None


# ── CLI ────────────────────────────────────────────────────────────────────────


def _emit_find_teacher_ckpt(args) -> int:
    try:
        target_abs, src_abs = find_teacher_ckpt(
            args.project_root, args.train_command, args.target,
        )
    except FileNotFoundError as e:
        print(f"[setup_helpers] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"TEACHER_CKPT: {target_abs}")
    print(f"TEACHER_CKPT_SRC: {src_abs}")
    return 0


def _emit_grep_user_train(args) -> int:
    train_import, loss_fn, sentinel = grep_user_train(
        args.project_root, args.train_command, args.baseline_model_path,
    )
    print(f"USER_TRAIN_IMPORT: {train_import}")
    print(f"USER_LOSS_FN: {loss_fn}")
    # sentinel 是多行 JSON；用一行 compact JSON emit 让 agent.md bash 好解析
    print(f"USER_TRAIN_SENTINEL: {json.dumps(sentinel, ensure_ascii=False) if sentinel else ''}")
    return 0


def _main() -> int:
    p = argparse.ArgumentParser(description="kd-setup 步骤 5/6 的确定性后端（rule 5）")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find-teacher-ckpt", help="解析 teacher_train_command 产物 → 拷到 target")
    p_find.add_argument("--project_root", required=True)
    p_find.add_argument("--train_command", default="")
    p_find.add_argument("--target", required=True, help="目标 ckpt 绝对路径（如 $TEACHER_CKPT）")
    p_find.set_defaults(func=_emit_find_teacher_ckpt)

    p_grep = sub.add_parser("grep-user-train", help="AST 解析用户 train.py 找 loss fn")
    p_grep.add_argument("--project_root", required=True)
    p_grep.add_argument("--train_command", default="")
    p_grep.add_argument("--baseline_model_path", default="")
    p_grep.set_defaults(func=_emit_grep_user_train)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(_main())
