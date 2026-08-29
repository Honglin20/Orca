#!/usr/bin/env python3
"""
kb_graph.py — 把 knowledge_base/ 的【内部连接逻辑】解析成交互式知识图（pyvis HTML）。

KB 内部不是平铺文件，而是一张多类型边知识图。本脚本把图自动还原出来：

  节点类型:
    D  direction    meta.json 目录 + directions/D*.md      (仅三层族, 如 wireless_receiver)
    M  move         latency_moves.md  (M1..M31)
    P  primitive    primitives.md     (§1..§N)
    A  铁律         common/ascend_constraints.md  (铁律1..9, 跨族共享)
    R  raw 骨架     raw/*.py.md       (可跑骨架 + 变异提示)
    F  failure      failures.md       (反模式 AVOID 清单)

  边类型:
    锚定     M -> D     【锚定: Dx/M#】 标签
    bundle   D -> M     direction 的 "bundle 的 move" 段
    接地     M|P -> A   §A# 引用（move/primitive 依赖哪条硬件铁律）
    raw绑定  R -> M|D   raw 文件头的 M#/D# 三重标签
    变异     P -> M     primitive 正文里点名的 move
    冲突     M -- M     latency_moves.md 的「冲突表（不可同叠）」

用法:
  python workflows/agent-struct-exploration/scripts/kb_graph.py                          # 默认 wireless_receiver（图最全）
  python workflows/agent-struct-exploration/scripts/kb_graph.py --family cnn             # 单层族（只有 M/P，无 D/R）
  python workflows/agent-struct-exploration/scripts/kb_graph.py --all                    # 所有族合并（label 带族前缀）
  python workflows/agent-struct-exploration/scripts/kb_graph.py --kb-dir <kb-root>  # 显式指定 KB 根（默认随脚本 <script>/../knowledge_base）
  python workflows/agent-struct-exploration/scripts/kb_graph.py --out kb_graph.html      # 指定输出

打开生成的 HTML 即可拖拽 / 悬停看详情 / 用右上 filter 按节点类型过滤。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pyvis.network import Network

# ─────────────────────────── 样式 ───────────────────────────
NODE_STYLE = {  # type -> (color, shape, size)
    "D": ("#3498DB", "box", 22),
    "M": ("#2ECC71", "dot", 16),
    "P": ("#9B59B6", "triangle", 14),
    "A": ("#95A5A6", "diamond", 13),
    "R": ("#E67E22", "square", 12),
    "F": ("#E74C3C", "box", 16),
}
# D 节点按 ascend 友好性覆盖底色
ASCEND_COLOR = {"friendly": "#27AE60", "conditional": "#F1C40F", "hostile": "#C0392B"}
EDGE_STYLE = {  # type -> (color, dashes)
    "锚定":    ("#2980B9", False),
    "bundle":  ("#1ABC9C", False),
    "接地":    ("#7F8C8D", [3, 4]),
    "raw绑定": ("#E67E22", [6, 3]),
    "变异":    ("#9B59B6", [2, 3]),
    "冲突":    ("#E74C3C", [8, 4]),
}
NODE_TYPE_CN = {"D": "方向", "M": "算子", "P": "原语", "A": "铁律", "R": "骨架", "F": "反模式"}


# ─────────────────────────── 解析器 ───────────────────────────
def _resolve_kb(kb: Path) -> None:
    if not kb.is_dir():
        sys.exit(f"[kb_graph] 找不到 KB 目录: {kb}")
    idx = kb / "index.json"
    if not idx.exists():
        sys.exit(f"[kb_graph] 缺 index.json，不是合法 KB: {idx}")


def list_families(kb: Path) -> list[str]:
    idx = json_load(kb / "index.json")
    return list(idx.get("families", {}).keys())


def json_load(p: Path):
    import json
    return json.loads(p.read_text(encoding="utf-8"))


def parse_axioms(kb: Path) -> dict[str, dict]:
    """common/ascend_constraints.md 的铁律 1..N → A 节点（跨族共享）。"""
    p = kb / "common" / "ascend_constraints.md"
    out: dict[str, dict] = {}
    if not p.exists():
        print(f"[kb_graph] WARN: 无 {p}，A 节点缺失", file=sys.stderr)
        return out
    for ln in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+铁律\s+(\d+)：(.+)", ln)
        if m:
            num, title = m.group(1), m.group(2).strip()
            out[f"A{num}"] = {"title": f"铁律{num}：{title}", "file": p}
    return out


MOVE_HEAD = re.compile(r"^###\s+(?:Move\s+)?(M\d+|[A-Z]\d+)[.:：]\s*(.*)")
# KB 三种 move 命名：wireless `### M1.` / cnn `### Move A1：` / transformer `### A1.`
TIER_HEAD = re.compile(r"^##\s+([^\s.：:]+)")  # `## A.` / `## T1.` / `## B 类.` → 分类码


def parse_moves(family_dir: Path) -> dict[str, dict]:
    """latency_moves.md → M 节点（单遍扫描，带 tier / anchors / axioms）。"""
    p = family_dir / "latency_moves.md"
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    cur_tier, cur_m = None, None
    for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not ln.startswith("###"):
            ht = TIER_HEAD.match(ln)
            if ht:
                cur_tier, cur_m = ht.group(1), None
                continue
        hm = MOVE_HEAD.match(ln)
        if hm:
            cur_m = hm.group(1)
            out[cur_m] = {"title": hm.group(2).strip(), "tier": cur_tier,
                          "anchors": [], "axioms": set(), "file": p, "line": i}
            continue
        if cur_m:
            am = re.search(r"【锚定:\s*([^/】]+)/M\d+】", ln)
            if am and not out[cur_m]["anchors"]:
                ds = am.group(1).strip()
                if ds != "全局":
                    out[cur_m]["anchors"] = re.findall(r"D\d+", ds)
            for ax in re.findall(r"§A(\d+)", ln):
                out[cur_m]["axioms"].add(int(ax))
            # latency_moves 用 `ascend_constraints.md` §N`（§N 不带 A）引用铁律，补抓
            for cite in re.finditer(r"ascend_constraints\.md[`']?\s*((?:§\d+\s*)+)", ln):
                for ax in re.findall(r"§(\d+)", cite.group(1)):
                    out[cur_m]["axioms"].add(int(ax))
    return out


def parse_primitives(family_dir: Path) -> dict[str, dict]:
    """primitives.md → P 节点（§N 段，记录它点名的 M 与 §A）。"""
    p = family_dir / "primitives.md"
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    cur_p, body_start = None, 0
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        hm = re.match(r"^##\s+(\d+)\.\s*(.*)", ln)
        if hm:
            cur_p = f"P{hm.group(1)}"
            out[cur_p] = {"title": hm.group(2).strip(), "moves": set(),
                          "axioms": set(), "file": p, "line": i + 1}
            continue
        if cur_p:
            for m in re.findall(r"\bM\d+\b", ln):
                out[cur_p]["moves"].add(m)
            for ax in re.findall(r"§A(\d+)", ln):
                out[cur_p]["axioms"].add(int(ax))
    return out


def parse_directions(family_dir: Path, meta: dict) -> dict[str, dict]:
    """directions/D*.md → D 节点（含 bundle 的 move + meta 标签）。"""
    ddir = family_dir / "directions"
    out: dict[str, dict] = {}
    if not ddir.is_dir():
        return out
    for f in sorted(ddir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        hm = re.search(r"^#\s+(D\d+)\s*[·•・]\s*([^\n（(]+)", text, re.M)
        if not hm:
            continue
        did, name = hm.group(1), hm.group(2).strip()
        bundles = []
        bm = re.search(r"##\s*bundle[^\n]*\n(.*?)(?:\n##\s|\Z)", text, re.S)
        if bm:
            bundles = list(dict.fromkeys(re.findall(r"\bM\d+\b", bm.group(1))))
        tags = meta.get("directions", {}).get(did, {})
        out[did] = {"name": name, "bundles": bundles, "tags": tags, "file": f}
    return out


def parse_raws(family_dir: Path) -> list[dict]:
    """raw/*.py.md → R 节点（文件头抽 M#/D# 三重标签）。"""
    rdir = family_dir / "raw"
    out = []
    if not rdir.is_dir():
        return out
    for f in sorted(rdir.glob("*.md")):
        first = f.read_text(encoding="utf-8").splitlines()[0:1]
        head = first[0] if first else ""
        out.append({
            "rid": f.stem, "file": f,
            "moves": list(dict.fromkeys(re.findall(r"\bM\d+\b", head))),
            "dirs": list(dict.fromkeys(re.findall(r"\bD\d+\b", head))),
        })
    return out


def parse_conflicts(family_dir: Path) -> list[tuple[str, str]]:
    """latency_moves.md 的「冲突表」→ M↔M 冲突边（排除「可同叠」例外行）。"""
    p = family_dir / "latency_moves.md"
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    cm = re.search(r"##\s*冲突表.*?\n(.*?)(?:\n##\s|\Z)", text, re.S)
    pairs = []
    if cm:
        for row in cm.group(1).splitlines():
            if "|" not in row or "可同叠" in row:
                continue
            ms = re.findall(r"\bM\d+\b", row)
            if len(ms) >= 2:
                pairs.append((ms[0], ms[1]))
    return pairs


# ─────────────────────────── 建图 ───────────────────────────
class Graph:
    def __init__(self, multi: bool):
        self.nodes = {}      # id -> dict(attrs)
        self.edges = set()   # (src, dst, type)
        self.multi = multi

    def label(self, family: str | None, kind: str, num: str) -> str:
        bare = f"{kind}{num}"
        if not self.multi:
            return bare
        prefix = family.replace("_receiver", "").replace("wireless", "wr") if family else "cmn"
        return f"{prefix}:{bare}"

    def add_node(self, nid: str, *, family, kind, num, title, file=None):
        color, shape, size = NODE_STYLE[kind]
        self.nodes[nid] = {
            "label": self.label(family, kind, num),
            "title": title, "group": kind, "color": color,
            "shape": shape, "size": size, "family": family or "", "file": str(file) if file else "",
        }
        return nid

    def add_edge(self, src, dst, etype):
        if src and dst and src in self.nodes and dst in self.nodes and src != dst:
            self.edges.add((src, dst, etype))


def build_family(g: Graph, family: str, kb: Path):
    fdir = kb / "families" / family
    if not fdir.is_dir():
        print(f"[kb_graph] WARN: 族目录缺失 {fdir}", file=sys.stderr)
        return
    # M 节点
    moves = parse_moves(fdir)
    for mid, info in moves.items():
        tier = f"[{info['tier']}] " if info["tier"] else ""
        g.add_node(f"{family}:{mid}", family=family, kind="M", num=mid,
                   title=f"{tier}M 算子 · {mid} {info['title']}\n"
                         f"族: {family}\n锚定方向: {', '.join(info['anchors']) or '全局'}\n"
                         f"接地铁律: {', '.join(f'§A{a}' for a in sorted(info['axioms'])) or '—'}\n"
                         f"文件: {info['file']}:{info['line']}")
    # P 节点 + P→M / P→A
    prims = parse_primitives(fdir)
    for pid, info in prims.items():
        g.add_node(f"{family}:{pid}", family=family, kind="P", num=pid.replace("P", ""),
                   title=f"P 原语 · {pid} {info['title']}\n族: {family}\n"
                         f"相关 move: {', '.join(sorted(info['moves'])) or '—'}\n"
                         f"接地铁律: {', '.join(f'§A{a}' for a in sorted(info['axioms'])) or '—'}\n"
                         f"文件: {info['file']}:{info['line']}")
    # D 节点（三层族）+ 锚定/bundle 边
    tiers = json_load(kb / "index.json")["families"].get(family, {}).get("tiers")
    meta = json_load(fdir / "meta.json") if (fdir / "meta.json").exists() else {"directions": {}}
    dirs = parse_directions(fdir, meta)
    for did, info in dirs.items():
        asc = info["tags"].get("ascend", "?")
        color = ASCEND_COLOR.get(asc, NODE_STYLE["D"][0])
        nid = g.add_node(f"{family}:{did}", family=family, kind="D", num=did,
                         title=f"D 方向 · {did} {info['name']}\n族: {family}\n"
                               f"ascend: {asc}  latency_tier: {info['tags'].get('latency_tier','?')}  "
                               f"risk: {info['tags'].get('risk','?')}  物理: {info['tags'].get('physics','?')}\n"
                               f"bundle move: {', '.join(info['bundles']) or '—'}\n来源: {info['tags'].get('source','—')}\n"
                               f"文件: {info['file']}")
        g.nodes[nid]["color"] = color  # 按 ascend 覆盖
        for m in info["bundles"]:
            g.add_edge(nid, f"{family}:{m}", "bundle")
    # M→D 锚定
    for mid, info in moves.items():
        for d in info["anchors"]:
            g.add_edge(f"{family}:{mid}", f"{family}:{d}", "锚定")
    # M→A 接地
    for mid, info in moves.items():
        for a in info["axioms"]:
            g.add_edge(f"{family}:{mid}", f"A{a}", "接地")
    for pid, info in prims.items():
        for a in info["axioms"]:
            g.add_edge(f"{family}:{pid}", f"A{a}", "接地")
        for m in info["moves"]:
            g.add_edge(f"{family}:{pid}", f"{family}:{m}", "变异")
    # R 节点 + raw 绑定
    for r in parse_raws(fdir):
        nid = g.add_node(f"{family}:R:{r['rid']}", family=family, kind="R", num=r["rid"],
                         title=f"R 骨架 · {r['file'].name}\n族: {family}\n"
                               f"绑定 move: {', '.join(r['moves']) or '—'}\n"
                               f"绑定方向: {', '.join(r['dirs']) or '—'}\n文件: {r['file']}")
        for m in r["moves"]:
            g.add_edge(nid, f"{family}:{m}", "raw绑定")
        for d in r["dirs"]:
            g.add_edge(nid, f"{family}:{d}", "raw绑定")
    # M↔M 冲突
    for a, b in parse_conflicts(fdir):
        g.add_edge(f"{family}:{a}", f"{family}:{b}", "冲突")
    # F 节点（反模式）
    fp = fdir / "failures.md"
    if fp.exists():
        g.add_node(f"{family}:F", family=family, kind="F", num="",
                   title=f"F 反模式 · failures.md\n族: {family}\nAVOID 清单 + analyst append\n文件: {fp}")
        # failures 与 move 的反向约束：扫 failures.md 里点名的 M#
        for m in re.findall(r"\bM\d+\b", fp.read_text(encoding="utf-8")):
            g.add_edge(f"{family}:F", f"{family}:{m}", "锚定")  # 复用一条边表示关联


def render(g: Graph, out: Path, title: str):
    net = Network(height="920px", width="100%", bgcolor="#11151c", font_color="#ECEFF4",
                  directed=True, notebook=False, cdn_resources="in_line",
                  select_menu=False, filter_menu=True, heading=title)
    for nid, a in g.nodes.items():
        net.add_node(nid, label=a["label"], title=a["title"], group=a["group"],
                     color=a["color"], shape=a["shape"], size=a["size"])
    for src, dst, etype in sorted(g.edges):
        color, dashes = EDGE_STYLE[etype]
        net.add_edge(src, dst, color=color, dashes=dashes, title=etype, width=2 if etype == "冲突" else 1.2)
    net.barnes_hut(spring_length=140, central_gravity=0.25)
    net.save_graph(str(out))
    _inject_legend(out)


def _inject_legend(out: Path):
    """把图例 overlay 注入 HTML（pyvis 不自带 legend）。"""
    rows = []
    for kind, (color, *_rest) in NODE_STYLE.items():
        rows.append(f'<span class="sw" style="background:{color}"></span>{kind}·{NODE_TYPE_CN[kind]}')
    for etype, (color, dashes) in EDGE_STYLE.items():
        dash = "dashed" if dashes else "solid"
        rows.append(f'<span class="ln" style="background:{color};{"" if not dashes else "opacity:.6"}"></span>{etype}')
    legend = (
        '<div id="legend" style="position:fixed;top:8px;left:8px;z-index:10;background:rgba(17,21,28,.92);'
        'color:#ECEFF4;padding:10px 12px;border-radius:8px;font:13px/1.7 monospace;border:1px solid #333">'
        + "<br>".join(rows) +
        '<br><span style="opacity:.6">D 颜色=ascend：绿friendly/黄conditional/红hostile</span>'
        '</div>'
    )
    css = '<style>#legend .sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:4px}' \
          '#legend .ln{display:inline-block;width:18px;height:0;border-top:3px solid;margin-right:4px;vertical-align:middle}</style>'
    html = out.read_text(encoding="utf-8")
    html = html.replace("</head>", css + "</head>", 1)
    html = html.replace("<body>", "<body>" + legend, 1)
    out.write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="把 knowledge_base/ 内部连接逻辑渲染成交互式知识图")
    ap.add_argument("--kb-dir", default=str(Path(__file__).resolve().parent.parent / "knowledge_base"), help="KB 根目录")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--family", help="只画指定族（默认 wireless_receiver）")
    grp.add_argument("--all", action="store_true", help="所有族合并")
    ap.add_argument("--out", default="kb_graph.html", help="输出 HTML 路径")
    args = ap.parse_args()

    kb = Path(args.kb_dir).resolve()
    _resolve_kb(kb)
    families = list_families(kb)
    if args.all:
        sel, multi = families, True
    else:
        fam = args.family or ("wireless_receiver" if "wireless_receiver" in families else families[0])
        if fam not in families:
            sys.exit(f"[kb_graph] 未知族 '{fam}'，可选: {families}")
        sel, multi = [fam], False

    g = Graph(multi=multi)
    # A 节点（跨族共享）
    for aid, info in parse_axioms(kb).items():
        g.add_node(aid, family=None, kind="A", num=aid.replace("A", ""),
                   title=f"{info['title']}\n（跨族共享铁律）\n文件: {info['file']}")
    for fam in sel:
        build_family(g, fam, kb)

    title = f"KB 知识图 · {' + '.join(sel)} · {len(g.nodes)} 节点 / {len(g.edges)} 边"
    out = Path(args.out).resolve()
    render(g, out, title)
    print(f"[kb_graph] ✔ {out}")
    print(f"         节点 {len(g.nodes)} / 边 {len(g.edges)} / 族 {sel}")
    by = {}
    for _, _, t in g.edges:
        by[t] = by.get(t, 0) + 1
    for k in ["锚定", "bundle", "接地", "raw绑定", "变异", "冲突"]:
        if k in by:
            print(f"           {k:<7}: {by[k]}")


if __name__ == "__main__":
    main()
