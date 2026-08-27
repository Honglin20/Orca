#!/usr/bin/env python3
"""_migrate_per_dir.py —— workflows/ 平铺 → per-wf 自包含目录一次性迁移（批 D）。

SPEC：C:/Users/mozzie/.claude/plans/crystalline-chasing-dewdrop.md 步骤 3；
计划：docs/plans/2026-08-27-workflow-per-dir-layout-plan.md §4.4。

用法（WSL venv）：
  .venv/bin/python scripts/_migrate_per_dir.py            # dry-run：只打印计划动作
  .venv/bin/python scripts/_migrate_per_dir.py --execute  # 真执行 + 自检

铁律：agents/**/agent.md、SKILL.md、subagents/**/*.md 正文零改动（自检 e 强制）。
内容修正白名单仅：workflow.yaml :59 default、psu 5 脚本 parents[4]→[5]、
elastic supernet_template.py docstring、ptq run_ptq_sweep.py 锚定注释、kb_graph.py
默认 KB 根锚定（.py/yaml 注释类，SPEC 授权清单）。

幂等：目标已存在 → skip + warn（断点重跑安全）；任何失败立即中止（fail loud）。
用后删除（批 I），git 历史留档。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WFS = REPO / "workflows"

# ── SPEC「目标布局」逐字固化 ────────────────────────────────────────────────
WF_DIRS = {  # wf-name == yaml 文件名 stem（批 D 前置探针已验证 14/14 name_eq_stem）
    "agent-struct-exploration", "nas-supernet", "nas-supernet-v2", "nas-supernet-v3",
    "puzzle", "puzzle-supernet", "prof-opt", "nas-agent-pipeline", "nas-hp-search",
    "quant-ptq-sweep", "quant-qat", "quant-sensitivity", "quant-bit-curve",
    "prune-channel-sweep",
}

# 共享 agent：首份 git mv 到首 wf，其余 cp -r + git add（SPEC L52 逐字）
SHARED_AGENTS = [  # (首 wf, [其余 wf], [agent 名])
    ("nas-agent-pipeline", ["nas-hp-search"],
     ["supernet-train-script", "nas-search-pipeline", "nas-train-runner", "nas-select"]),
]

# 只取子集的池：_quant_scripts 仅 _common.py/_device.py，4 个 quant wf 各一份
# （prune-channel-sweep 自包含不用）；首 wf git mv，其余 copy2 + git add
QUANT_POOL_FILES = ["_common.py", "_device.py"]
QUANT_WFS = ["quant-ptq-sweep", "quant-qat", "quant-sensitivity", "quant-bit-curve"]

EXCLUSIVE_POOLS = {  # 独占池：git mv 整目录（kd-nas 删除后单一下家）
    "_struct_scripts": "agent-struct-exploration",
    "_puzzle_scripts": "puzzle",
    "_po_scripts": "prof-opt",
}

EXTRA_AGENT_MOVES = {"pz_expand": "puzzle"}  # SPEC 步骤 3.1：唯一 checklist 载体，非 yaml 引用

KB_WF = "agent-struct-exploration"  # knowledge_base/ + scripts/kb_graph.py 的下家

# ── 内容修正白名单（相对 WFS / REPO 的新布局路径）─────────────────────────────
# (文件, [(旧串, 新串, 预期次数)]) —— 替换计数不符 → exit(1)（SPEC 授权清单外零改动）
CONTENT_FIXES = [
    # 既定例外①：yaml input default（非 prompt；:59）
    ("workflows/agent-struct-exploration/workflow.yaml", [
        ('default: "workflows/agents/_struct_scripts/latency_onnxrt.py::measure"',
         'default: "workflows/agent-struct-exploration/agents/_struct_scripts/latency_onnxrt.py::measure"',
         1),
    ]),
    # psu 系 5 脚本 parents[4]→parents[5] + 同步行内注释（迁移后目录深度 +1）
    ("workflows/puzzle-supernet/agents/psu_retrain/scripts/progress_watcher.py",
     [("parents[4]", "parents[5]", 2),
      ("脚本位于 workflows/agents/<node>/scripts/",
       "脚本位于 workflows/puzzle-supernet/agents/<node>/scripts/", 1)]),
    ("workflows/puzzle-supernet/agents/psu_run_train/scripts/progress_watcher.py",
     [("parents[4]", "parents[5]", 2),
      ("脚本位于 workflows/agents/<node>/scripts/",
       "脚本位于 workflows/puzzle-supernet/agents/<node>/scripts/", 1)]),
    ("workflows/puzzle-supernet/agents/psu_retrain/scripts/_common.py",
     [("parents[4]", "parents[5]", 1)]),
    ("workflows/puzzle-supernet/agents/psu_run_search/scripts/_common.py",
     [("parents[4]", "parents[5]", 1)]),
    ("workflows/puzzle-supernet/agents/psu_expand_supernet/scripts/search_space_table.py",
     [("parents[4]", "parents[5]", 1)]),
    # docstring 用法示例路径（.py 注释类允许清单）
    ("workflows/nas-hp-search/agents/elastic_optimizer/references/supernet_template.py",
     [("python workflows/agents/elastic_optimizer/references/supernet_template.py",
       "python workflows/nas-hp-search/agents/elastic_optimizer/references/supernet_template.py",
       1)]),
    # 路径锚定注释（锚定公式 parent.parent.parent/"_quant_scripts" 不变，只改注释字面）
    ("workflows/quant-ptq-sweep/agents/ptq-sweeper/scripts/run_ptq_sweep.py",
     [("workflows/agents/ptq-sweeper/scripts/",
       "workflows/quant-ptq-sweep/agents/ptq-sweeper/scripts/", 1),
      ("workflows/agents/_quant_scripts/",
       "workflows/quant-ptq-sweep/agents/_quant_scripts/", 1)]),
    # kb_graph.py：默认 KB 根改脚本相对锚定（不依赖 cwd）+ docstring 用法路径
    (f"workflows/{KB_WF}/scripts/kb_graph.py",
     [("python scripts/kb_graph.py",
       f"python workflows/{KB_WF}/scripts/kb_graph.py", 5),
      ("--kb-dir knowledge_base  # 指定 KB 根",
       "--kb-dir <kb-root>  # 显式指定 KB 根（默认随脚本 <script>/../knowledge_base）", 1),
      ('default="knowledge_base"',
       'default=str(Path(__file__).resolve().parent.parent / "knowledge_base")', 1)]),
]


def die(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        die(f"git {' '.join(args)} 失败:\n{p.stderr}")
    return p


def p_rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


# ── Phase 1：agent 引用提取（parser 口径）+ grep 交叉核对 ─────────────────────
def extract_refs() -> dict[str, list[str]]:
    sys.path.insert(0, str(REPO))
    import yaml as yaml_lib
    from orca.compile.parser import _iter_agent_nodes
    from orca.schema import Workflow

    # 双形态源（幂等重跑）：平铺优先，源已迁则读 per-wf
    yamls = sorted(WFS.glob("*.yaml")) or sorted(WFS.glob("*/workflow.yaml"))
    refs: dict[str, list[str]] = {}
    for yaml_path in yamls:
        stem = yaml_path.parent.name if yaml_path.name == "workflow.yaml" else yaml_path.stem
        # 轻量解析（不过物化/校验管线）：引用提取只需 node.agent/prompt/name 三态，
        # 且重跑场景共享副本可能尚未 cp，物化必然失败
        wf = Workflow(**yaml_lib.safe_load(yaml_path.read_text(encoding="utf-8")))
        names = []
        for node, _is_body, _parent in _iter_agent_nodes(wf):
            if node.agent is not None:          # 显式引用
                names.append(node.agent)
            elif node.prompt is None:           # 旧约定 name-fallback（prompt+agent 双 None）
                names.append(node.name)
            # node.prompt 非 None 且 agent None → 内联节点，无池引用
        if wf.name != stem:
            die(f"{yaml_path}: yaml name 字段 {wf.name!r} != 目录/文件名 stem，WF_DIRS 假设破灭")
        if len(set(names)) != len(names):
            die(f"{yaml_path}: 重复 agent 引用 {sorted(names)}")
        refs[stem] = sorted(set(names))
    if set(refs) != WF_DIRS:
        die(f"源 yaml 集 {sorted(refs)} != WF_DIRS {sorted(WF_DIRS)}")
    # grep 粗集交叉核对：yaml 文本 `agent:` 行 ⊇ parser 集（防只走引用漏池成员）
    for stem, parser_refs in refs.items():
        src = WFS / f"{stem}.yaml"
        if not src.is_file():
            src = WFS / stem / "workflow.yaml"
        text = src.read_text(encoding="utf-8")
        grep_refs = {m.group(1).strip() for m in
                     re.finditer(r"^\s*agent:\s*([\w\-\.]+)\s*$", text, re.M)}
        if not grep_refs <= set(parser_refs):
            die(f"{stem}: grep 粗集 {sorted(grep_refs - set(parser_refs))} 不在 parser 集内")
    return refs


def build_plan(refs: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """动作清单：[(动作, src, dst)]，动作 ∈ MV / CPO(整目录 copy) / CPF(单文件 copy)。"""
    moves: list[tuple[str, str, str]] = []

    def mv(src: Path, dst: Path, what: str):
        moves.append((what, p_rel(src), p_rel(dst)))

    pool = REPO / "workflows" / "agents"
    # 共享 agent -> 首 wf（首份 git mv；其余 wf 走 cp，引用循环里跳过，见 Phase 3）
    shared_first: dict[str, str] = {
        agent: first for first, _others, agents in SHARED_AGENTS for agent in agents
    }
    # 1) yaml + 各 wf 的 agent 引用（源/目标双查：幂等重跑时源已迁走、目标就位）
    for wf in sorted(WF_DIRS):
        yaml_src = WFS / f"{wf}.yaml"
        if not yaml_src.is_file() and not (WFS / wf / "workflow.yaml").is_file():
            die(f"{wf}: 源 yaml 与目标 workflow.yaml 均不存在")
        mv(yaml_src, WFS / wf / "workflow.yaml", "MV-YAML")
        for agent in refs[wf]:
            src = pool / agent
            dst = WFS / wf / "agents" / agent
            if shared_first.get(agent, wf) != wf:
                continue  # 共享 agent 非首 wf：由 Phase 3 cp 副本覆盖，不 mv、不双查
            if not src.is_dir() and not dst.is_dir():
                die(f"{wf} 引用的 agent 目录源与目标均不存在: {src}")
            mv(src, dst, "MV-AGENT")

    # 2) 显式补充（pz_expand：唯一 checklist 载体，非 yaml 引用）
    for agent, wf in EXTRA_AGENT_MOVES.items():
        mv(pool / agent, WFS / wf / "agents" / agent, "MV-EXTRA")

    # 3) 共享 agent 首份 mv（首 wf 已在引用集覆盖）+ 其余 cp
    for first, others, agents in SHARED_AGENTS:
        for agent in agents:
            if agent not in refs[first]:
                die(f"共享 agent {agent} 不被首 wf {first} 引用")
            ref_wfs = {wf for wf, names in refs.items() if agent in names}
            if ref_wfs - {first, *others}:
                die(f"共享 agent {agent} 被 SHARED_AGENTS 未声明的 wf 引用: "
                    f"{sorted(ref_wfs - {first, *others})}")
            for other in others:
                moves.append(("CPO-AGENT", p_rel(WFS / first / "agents" / agent),
                              p_rel(WFS / other / "agents" / agent)))

    # 4) _quant_scripts：首 wf 两文件 mv，其余 copy
    for fname in QUANT_POOL_FILES:
        mv(pool / "_quant_scripts" / fname,
           WFS / QUANT_WFS[0] / "agents" / "_quant_scripts" / fname, "MV-POOL")
    for other in QUANT_WFS[1:]:
        for fname in QUANT_POOL_FILES:
            moves.append(("CPF-POOL", p_rel(WFS / QUANT_WFS[0] / "agents" / "_quant_scripts" / fname),
                          p_rel(WFS / other / "agents" / "_quant_scripts" / fname)))

    # 5) 独占池整目录 mv
    for poolname, wf in EXCLUSIVE_POOLS.items():
        mv(pool / poolname, WFS / wf / "agents" / poolname, "MV-POOL")

    # 6) subagents 拍平（目录整体 rename，git mv 到不存在目标）
    subs = REPO / "workflows" / "subagents"
    for sub in sorted(subs.iterdir()) if subs.is_dir() else []:
        if not sub.is_dir():
            continue
        if sub.name not in WF_DIRS:
            die(f"subagents/ 下未知 wf 目录: {sub.name}")
        md = sorted(sub.glob("*.md"))
        if not md:
            die(f"subagents/{sub.name} 无 md")
        mv(sub, WFS / sub.name / "subagents", "MV-SUB")

    # 7) KB + kb_graph.py 收编
    mv(REPO / "knowledge_base", WFS / KB_WF / "knowledge_base", "MV-KB")
    mv(REPO / "scripts" / "kb_graph.py", WFS / KB_WF / "scripts" / "kb_graph.py", "MV-SCRIPT")
    return moves


def check_plan_covers_pool(moves: list[tuple[str, str, str]]) -> None:
    """校验：agents/ 池的 tracked 顶层目录全部有下家（漏迁即自检 b 炸，提前 fail）。"""
    tracked = {line.split("/", 2)[2].split("/")[0] for line in
               git("ls-files", "workflows/agents/").stdout.splitlines() if line.strip()}
    covered = {src.split("/")[2] for _kind, src, _dst in moves
               if src.startswith("workflows/agents/")}
    uncovered = tracked - covered
    if uncovered:
        die(f"agents/ 池 tracked 目录无下家: {sorted(uncovered)}")


# ── Phase 2：dry-run / execute ──────────────────────────────────────────────
def execute_moves(moves: list[tuple[str, str, str]]) -> None:
    for kind, src, dst in moves:
        s, d = REPO / src, REPO / dst
        if d.exists():
            if kind in ("CPO-AGENT", "CPF-POOL"):
                git("add", dst)  # 幂等补 add（前次中断可能 cp 完未 add）
                print(f"SKIP(幂等+add): {src} -> {dst}")
            elif s.exists():
                print(f"WARN: 目标已存在且源仍在，跳过（需人工判读）: {src} -> {dst}")
            else:
                print(f"SKIP(幂等): 已完成 {src} -> {dst}")
            continue
        if not s.exists():
            die(f"源不存在且目标未就位（断链）: {src} -> {dst}")
        if kind == "MV-SCRIPT":
            # kb_graph.py 被 .gitignore:109 忽略（从未 tracked，git mv 不可用）——
            # 新路径不受该条目约束，文件系统 move + git add 收编入库（实测裁决）
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
            git("add", dst)
        elif kind.startswith("MV"):
            d.parent.mkdir(parents=True, exist_ok=True)
            git("mv", src, dst)
        elif kind == "CPO-AGENT":
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            git("add", dst)
        elif kind == "CPF-POOL":
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            git("add", dst)
        print(f"OK {kind}: {src} -> {dst}")


def apply_content_fixes() -> None:
    for rel, subs in CONTENT_FIXES:
        f = REPO / rel
        if not f.is_file():
            die(f"内容修正目标不存在: {rel}")
        text = f.read_text(encoding="utf-8")
        for old, new, expect in subs:
            n_old, n_new = text.count(old), text.count(new)
            if n_old == expect:
                text = text.replace(old, new)
            elif n_old == 0 and n_new == expect:
                print(f"SKIP(幂等): {rel} 已含新串 {new[:60]}")
            else:
                die(f"{rel}: 旧串出现 {n_old} 次（预期 {expect}）、新串 {n_new} 次 —— 不匹配")
        f.write_text(text, encoding="utf-8", newline="\n")
        git("add", rel)
        print(f"OK FIX: {rel}")


# ── Phase 4：骨架清理（先 pycache 后空壳，自底向上；plan-adversary Q16a）──────
def cleanup_skeleton() -> None:
    for pyc in sorted(WFS.rglob("__pycache__"), reverse=True):
        shutil.rmtree(pyc)
        print(f"RM PYCACHE: {p_rel(pyc)}")
    if (REPO / "scripts" / "__pycache__").is_dir():  # kb_graph.py 孤儿 pyc
        for pyc in (REPO / "scripts" / "__pycache__").glob("kb_graph*.pyc"):
            pyc.unlink()
            print(f"RM ORPHAN PYC: {p_rel(pyc)}")
    # agents/ 子树自底向上清空目录：kd 系残留（批 B git rm 后盘上只剩 pycache 壳）
    # 在 pycache 删除后留下空目录链；含 tracked 内容的目录 rmdir 自然失败（OSError）
    # → 不吞：最终由下方壳检查 fail loud 兜底
    if (WFS / "agents").is_dir():
        for d in sorted((WFS / "agents").rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                    print(f"RMDIR: {p_rel(d)}")
                except OSError:
                    pass
    shells = [WFS / "agents" / "_quant_scripts", WFS / "agents", WFS / "subagents"]
    for shell in shells:  # 自底向上：深层在前（列表序已按深度）
        if not shell.exists():
            continue
        residual = [p for p in shell.rglob("*") if p.exists()]
        if residual:
            die(f"空壳目录非空（tracked 残留？）: {shell} -> {[p_rel(p) for p in residual[:10]]}")
        shell.rmdir()
        print(f"RMDIR: {p_rel(shell)}")


# ── Phase 5：自检（exit code 说话）───────────────────────────────────────────
def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


SELF = "scripts/_migrate_per_dir.py"  # 本脚本 untracked 属预期（批 D commit 入库，批 I 删）


def verify(moves: list[tuple[str, str, str]]) -> None:
    # a. git status 无未预期 untracked（脚本产物全部已 add；.pyc 已清）
    for line in git("status", "--porcelain", "workflows/", "knowledge_base", "scripts/").stdout.splitlines():
        if line.startswith("??") and line[3:].strip() != SELF:
            die(f"自检 a 失败：未跟踪残留 {line.strip()}")
    # b. workflows/ 根下零 yaml、零 agents/、零 subagents/
    root_yaml = list(WFS.glob("*.yaml"))
    if root_yaml:
        die(f"自检 b 失败：根下残留 yaml {[p.name for p in root_yaml]}")
    for bad in ("agents", "subagents"):
        if (WFS / bad).exists():
            die(f"自检 b 失败：{WFS / bad} 仍存在")
    # c. 14 wf 目录各含 workflow.yaml
    for wf in sorted(WF_DIRS):
        f = WFS / wf / "workflow.yaml"
        if not f.is_file():
            die(f"自检 c 失败：{f} 不存在")
    # d. 共享副本 sha256 逐一比对（基准 = 首份 git mv 目标；成功标准 4b）
    for first, others, agents in SHARED_AGENTS:
        for agent in agents:
            base = WFS / first / "agents" / agent
            for other in others:
                copy = WFS / other / "agents" / agent
                pairs = [(bf, copy / bf.relative_to(base))
                         for bf in sorted(base.rglob("*")) if bf.is_file()]
                for b, c in pairs:
                    if not c.is_file() or sha256(b) != sha256(c):
                        die(f"自检 d 失败：副本不一致 {p_rel(b)} vs {p_rel(c)}")
                print(f"OK SHA256: {agent} {first} <-> {other}（{len(pairs)} 文件）")
    for fname in QUANT_POOL_FILES:
        base = WFS / QUANT_WFS[0] / "agents" / "_quant_scripts" / fname
        for other in QUANT_WFS[1:]:
            c = WFS / other / "agents" / "_quant_scripts" / fname
            if not c.is_file() or sha256(base) != sha256(c):
                die(f"自检 d 失败：_quant_scripts/{fname} 副本不一致（{other}）")
        print(f"OK SHA256: _quant_scripts/{fname} x{len(QUANT_WFS)}")
    # e. md 零内容改动：git diff --cached -M 中 *.md 仅 R100（rename）或 A（共享副本，
    #    由 d 的 sha256 覆盖），出现 M 即铁律破灭
    out = git("diff", "--cached", "-M", "--name-status").stdout
    for line in out.splitlines():
        status, path = line.split("\t")[0], line.split("\t")[-1]
        if path.endswith(".md"):
            if status.startswith("R"):
                score = status[len("R"):]
                if score and int(score.rstrip("%")) != 100:
                    die(f"自检 e 失败：md rename 相似度非 100%: {line}")
            elif not status.startswith("A"):
                die(f"自检 e 失败：md 出现非 rename/add 状态: {line}")
    md_r = sum(1 for l in out.splitlines()
               if l.split("\t")[-1].endswith(".md") and l.startswith(("R", "A")))
    print(f"OK MD-ZERO-CHANGE: staged md 全部 R100/A（共 {md_r} 个）")
    print("ALL CHECKS PASSED")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true", help="真执行（默认 dry-run 只打印）")
    args = ap.parse_args()

    if not args.execute:
        st = git("status", "--porcelain", "workflows/", "knowledge_base", "scripts/")
        dirty = [l for l in st.stdout.splitlines() if l[3:].strip() != SELF]
        if dirty:
            die(f"dry-run 前置失败：迁移域内有未提交改动\n" + "\n".join(dirty))

    refs = extract_refs()
    moves = build_plan(refs)
    check_plan_covers_pool(moves)

    print(f"== 计划动作 {len(moves)} 条（{'EXECUTE' if args.execute else 'DRY-RUN'}）==")
    for kind, src, dst in moves:
        print(f"{kind}: {src} -> {dst}")
    print(f"== 内容修正 {len(CONTENT_FIXES)} 个文件 ==")
    if not args.execute:
        for rel, subs in CONTENT_FIXES:
            print(f"FIX: {rel}（{len(subs)} 处替换）")
        print("dry-run 结束（未执行任何动作）")
        return

    execute_moves(moves)
    apply_content_fixes()
    cleanup_skeleton()
    verify(moves)


if __name__ == "__main__":
    main()
