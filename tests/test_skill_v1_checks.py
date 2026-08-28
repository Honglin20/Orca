"""test_skill_v1_checks.py —— create-workflow skill 元层 V1 三脚本单测。

对象：``orca/skills/create-workflow/scripts/`` 下三个 stdlib-only 校验脚本
（check_dev_residue / check_agent_md_static / check_charts）。fixture 全部在
tmp_path 内联构造，不依赖仓库既有文件；脚本经 subprocess 真跑（exit code +
stdout 清单 + findings 都是被测契约）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "orca"
    / "skills"
    / "create-workflow"
    / "scripts"
)


def _run(script: str, *args) -> subprocess.CompletedProcess:
    # 固定子进程 stdout 编码：父进程按 utf-8 解码，子进程 locale 不一致时会假红。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / script), *(str(a) for a in args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── check_dev_residue ─────────────────────────────────────────────────────────


def test_dev_residue_reports_dev_id(tmp_path):
    md = _write(tmp_path / "agent.md", "按 BLK-3 的结论处理后续分支。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 1
    assert "BLK-3" in r.stdout
    assert "[dev-id]" in r.stdout


def test_dev_residue_reports_migration_word(tmp_path):
    md = _write(tmp_path / "agent.md", "本流程迁移自旧项目的评测管线。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 1
    assert "迁移自" in r.stdout
    assert "[archaeology]" in r.stdout


def test_dev_residue_vit_model_name_exempt(tmp_path):
    md = _write(tmp_path / "agent.md", "骨干网络选用 ViT-14 配置。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 0, r.stdout


def test_dev_residue_percentile_whitelist_vs_single_digit(tmp_path):
    ok = _write(tmp_path / "ok.md", "延迟分位 P95=12ms，P99=30ms，P999=50ms，尾延迟P99仍在阈值内。\n")
    r = _run("check_dev_residue.py", ok)
    assert r.returncode == 0, r.stdout

    bad = _write(tmp_path / "bad.md", "按 P5 处理该分支。\n")
    r = _run("check_dev_residue.py", bad)
    assert r.returncode == 1
    assert "P5" in r.stdout
    assert "[milestone]" in r.stdout


def test_dev_residue_single_digit_p_cjk_adjacent(tmp_path):
    """中文邻接（无空格）的单数字 P 记号同样命中——CJK 旁的 ASCII 词边界必须视为边界。"""
    md = _write(tmp_path / "agent.md", "按P5处理该分支。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 1
    assert "P5" in r.stdout


def test_dev_residue_exempt_hit_suppresses_only_that_hit(tmp_path):
    """豁免只抑制被包含的命中：同行 ViT-14（豁免）与其它编号（照报）并存。"""
    md = _write(tmp_path / "agent.md", "骨干网络选 ViT-14，编号见 BLK-3。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 1
    assert "BLK-3" in r.stdout
    assert "T-14" not in r.stdout  # 豁免命中不进报告（span 级而非行级）


def test_dev_residue_utf8_encoding_name_exempt(tmp_path):
    md = _write(tmp_path / "agent.md", "输入文件按 UTF-8 编码读取。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 0, r.stdout


def test_dev_residue_clean_file_manifest(tmp_path):
    md = _write(tmp_path / "agent.md", "读取输入数据并输出汇总报告。\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 0, r.stdout
    assert "1 files" in r.stdout  # 扫描清单正控：非零扫描才算数


def test_dev_residue_missing_path_exit_2(tmp_path):
    r = _run("check_dev_residue.py", tmp_path / "nope.md")
    assert r.returncode == 2
    assert r.stderr.strip()


def test_dev_residue_test_fixture_name_out_of_scope(tmp_path):
    """测试项目名硬编码不在 deterministic 表内（误报率高，留给语义审查）——钉住边界。"""
    md = _write(tmp_path / "agent.md", "MNIST=0.98\n")
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 0, r.stdout


def test_dev_residue_allow_flag_suppresses(tmp_path):
    md = _write(tmp_path / "agent.md", "按 BLK-3 的结论处理。\n")
    r = _run("check_dev_residue.py", md, "--allow", r"BLK-\d+")
    assert r.returncode == 0, r.stdout


def test_dev_residue_allow_flag_invalid_regex_exit_2(tmp_path):
    md = _write(tmp_path / "agent.md", "干净内容。\n")
    r = _run("check_dev_residue.py", md, "--allow", "[")
    assert r.returncode == 2
    assert r.stderr.strip()


def test_dev_residue_multiple_inputs_each_manifest(tmp_path):
    clean = _write(tmp_path / "a.md", "读取输入并输出报告。\n")
    dirty = _write(tmp_path / "b.md", "按 BLK-3 的结论处理。\n")
    r = _run("check_dev_residue.py", clean, dirty)
    assert r.returncode == 1
    assert "BLK-3" in r.stdout
    assert r.stdout.count("→ 1 files") == 2  # 每输入一行清单


def test_dev_residue_non_utf8_reads_with_warn(tmp_path):
    md = tmp_path / "notes.md"
    md.write_bytes(b"\xff\xfe plain text\n")  # 非 UTF-8 字节
    r = _run("check_dev_residue.py", md)
    assert r.returncode == 0
    assert "[warn]" in r.stdout


# ── check_agent_md_static ─────────────────────────────────────────────────────

_FM = "---\ndescription: 基准评测\n---\n"


def _folder_agent(tmp_path, body: str, frontmatter: str | None = _FM) -> Path:
    agent_dir = tmp_path / "agents" / "bench"
    _write(agent_dir / "agent.md", (frontmatter or "") + body)
    return agent_dir


def test_agent_static_flat_script_in_agent_root(tmp_path):
    agent_dir = _folder_agent(tmp_path, "跑基准评测并写报告。\n")
    _write(agent_dir / "run_bench.py", "print('bench')\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[layout]" in r.stdout


def test_agent_static_relative_script_ref(tmp_path):
    agent_dir = _folder_agent(tmp_path, "```bash\npython3 scripts/x.py\n```\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[script-ref]" in r.stdout


def test_agent_static_artifacts_env_prefix_legal(tmp_path):
    # deployed-workflow command form: $ORCA_ARTIFACTS_DIR/scripts/<file> is a
    # legitimate absolute invocation, not a relative ref
    agent_dir = _folder_agent(
        tmp_path,
        '```bash\npython3 "$ORCA_ARTIFACTS_DIR/scripts/x.py" --artifacts "$ORCA_ARTIFACTS_DIR"\n```\n')
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout


def test_agent_static_deployed_convention_prose_downgrade(tmp_path):
    # a file using the deployed absolute form may mention deployed scripts
    # shorthand in prose -> one aggregated warn, not per-line errors
    body = ('```bash\npython3 "$ORCA_ARTIFACTS_DIR/scripts/x.py"\n```\n'
            'Then inspect `scripts/helper.py` for the output shape.\n')
    agent_dir = _folder_agent(tmp_path, body)
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout
    assert "[warn]" in r.stdout and "部署约定" in r.stdout


def test_agent_static_deployed_command_position_still_error(tmp_path):
    # the downgrade never covers command position: a bare interpreter-relative
    # invocation fails at spawn-cwd resolution even in a deployed-convention file
    body = ('```bash\npython3 "$ORCA_ARTIFACTS_DIR/scripts/x.py"\n```\n'
            '```bash\npython3 scripts/bare.py\n```\n')
    agent_dir = _folder_agent(tmp_path, body)
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[script-ref]" in r.stdout


def test_agent_static_resource_script_with_artifacts_flag_not_deployed(tmp_path):
    # coincidence trap (ns_retrain style): a resource script taking an
    # --artifacts-dir flag is NOT the deployment convention — bare mentions
    # in such a file stay error-level
    body = ('```bash\npython3 "$ORCA_AGENT_RESOURCES/scripts/run.py" '
            '--artifacts-dir "$ORCA_ARTIFACTS_DIR"\n```\n'
            'See `scripts/other.py` for details.\n')
    agent_dir = _folder_agent(tmp_path, body)
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[script-ref]" in r.stdout


def test_agent_static_folder_agent_missing_frontmatter(tmp_path):
    agent_dir = _folder_agent(tmp_path, "跑基准评测。\n", frontmatter=None)
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[frontmatter]" in r.stdout


def test_agent_static_file_agent_without_frontmatter_legal(tmp_path):
    md = _write(tmp_path / "agents" / "helper.md", "# 助手\n纯 prompt body，无 YAML 头。\n")
    r = _run("check_agent_md_static.py", md)
    assert r.returncode == 0, r.stdout
    assert "1 files" in r.stdout


def test_agent_static_bash_fence_for_loop(tmp_path):
    agent_dir = _folder_agent(
        tmp_path, "```bash\nfor i in 1 2 3; do\n  echo $i\ndone\n```\n"
    )
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[inline-shell]" in r.stdout


def test_agent_static_inline_python_c_logic(tmp_path):
    agent_dir = _folder_agent(
        tmp_path, '```bash\npython -c "for i in range(3): print(i)"\n```\n'
    )
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[inline-python]" in r.stdout


def test_agent_static_long_sequential_fence_warns(tmp_path):
    lines = "\n".join(f"echo step {i}" for i in range(10))
    agent_dir = _folder_agent(tmp_path, f"```bash\n{lines}\n```\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0
    assert "[warn]" in r.stdout


def test_agent_static_compliant_folder_agent(tmp_path):
    agent_dir = _folder_agent(
        tmp_path,
        "# 基准评测\n\n## 执行\n\n```bash\n"
        'python3 "$ORCA_AGENT_RESOURCES/scripts/bench.py" --out "$ORCA_ARTIFACTS_DIR"\n'
        "```\n",
    )
    _write(agent_dir / "scripts" / "bench.py", "print('bench')\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout


def test_agent_static_no_agents_dir_zero_files(tmp_path):
    yaml = _write(tmp_path / "wf.yaml", "name: demo\n")
    r = _run("check_agent_md_static.py", yaml)
    assert r.returncode == 0, r.stdout
    assert "0 files" in r.stdout


def test_agent_static_missing_path_exit_2(tmp_path):
    r = _run("check_agent_md_static.py", tmp_path / "nope.md")
    assert r.returncode == 2
    assert r.stderr.strip()


def test_agent_static_unsupported_file_type_exit_2(tmp_path):
    py = _write(tmp_path / "tool.py", "print('not an agent')\n")
    r = _run("check_agent_md_static.py", py)
    assert r.returncode == 2
    assert r.stderr.strip()


def test_agent_static_non_utf8_reads_with_warn(tmp_path):
    md = tmp_path / "agents" / "bad.md"
    md.parent.mkdir(parents=True)
    md.write_bytes(b"\xff\xfe plain body\n")
    r = _run("check_agent_md_static.py", md)
    assert r.returncode == 0
    assert "[warn]" in r.stdout


def test_agent_static_python_fence_not_flagged(tmp_path):
    """python 围栏内的 for 是示例代码，不是 bash 控制流内联——负控。"""
    agent_dir = _folder_agent(
        tmp_path, "```python\nfor i in range(3):\n    print(i)\n```\n"
    )
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout


def test_agent_static_unlabeled_fence_control_flagged(tmp_path):
    agent_dir = _folder_agent(tmp_path, "```\nif [ -f report ]; then echo ok; fi\n```\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[inline-shell]" in r.stdout


def test_agent_static_indent_tolerated_incl_tab(tmp_path):
    """容 ≤4 缩进（含 tab）的启行控制流都要命中。"""
    spaced = _folder_agent(tmp_path, "```bash\n    while true; do sleep 1; done\n```\n")
    tabbed_dir = tmp_path / "agents2" / "bench"
    _write(tabbed_dir / "agent.md", _FM + "```bash\n\tfor i in 1 2; do echo $i; done\n```\n")
    for target in (spaced, tabbed_dir):
        r = _run("check_agent_md_static.py", target)
        assert r.returncode == 1, target
        assert "[inline-shell]" in r.stdout


def test_agent_static_python3_dash_c_with_assert(tmp_path):
    agent_dir = _folder_agent(tmp_path, "```bash\npython3 -c 'assert total > 0'\n```\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 1
    assert "[inline-python]" in r.stdout


def test_agent_static_py_c_scope_limited_to_argument(tmp_path):
    """-c 参数串之外的同行文本（后续命令含 for）不参与判定。"""
    agent_dir = _folder_agent(
        tmp_path, '```bash\npython -c "print(1)" && echo "wait for done"\n```\n'
    )
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout


def test_agent_static_quoted_env_prefix_legal(tmp_path):
    """引号先闭写法 "$ORCA_AGENT_RESOURCES"/scripts/x.py 同样是合法绝对引用。"""
    agent_dir = _folder_agent(
        tmp_path,
        "```bash\n"
        'bash "$ORCA_AGENT_RESOURCES"/scripts/bench.py --out "$ORCA_ARTIFACTS_DIR"\n'
        "```\n",
    )
    _write(agent_dir / "scripts" / "bench.py", "print('bench')\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout


def test_agent_static_non_script_resource_ref_not_flagged(tmp_path):
    """prose 提及 scripts/ 下的非脚本文件（.json）不按脚本相对引用报。"""
    agent_dir = _folder_agent(tmp_path, "结果写 scripts/out.json。\n")
    r = _run("check_agent_md_static.py", agent_dir)
    assert r.returncode == 0, r.stdout


def test_agent_static_long_fence_boundary_at_8(tmp_path):
    """>8 行才警：8 行静默、9 行 [warn]。"""
    quiet = _folder_agent(
        tmp_path, "```bash\n" + "\n".join(f"echo s{i}" for i in range(8)) + "\n```\n"
    )
    r = _run("check_agent_md_static.py", quiet)
    assert r.returncode == 0
    assert "[warn]" not in r.stdout

    loud_dir = tmp_path / "agents9" / "bench"
    _write(
        loud_dir / "agent.md",
        _FM + "```bash\n" + "\n".join(f"echo s{i}" for i in range(9)) + "\n```\n",
    )
    r = _run("check_agent_md_static.py", loud_dir)
    assert r.returncode == 0
    assert "[warn]" in r.stdout


# ── check_charts ──────────────────────────────────────────────────────────────

_IMPORT_GUARD = (
    "import sys\n\n"
    "try:\n"
    "    from orca.chart import render_chart\n"
    "except Exception:\n"
    "    render_chart = None\n\n\n"
)


def _chart_script(call_lines: list[str]) -> str:
    """规范样板：try 包裹的 render_chart 调用（调用行由用例给定）。"""
    body = "\n".join("        " + line for line in call_lines)
    return (
        _IMPORT_GUARD
        + "def main() -> int:\n"
        '    data = [{"step": 1, "value": 2}]\n'
        "    try:\n"
        f"{body}\n"
        "    except Exception as e:\n"
        '        sys.stderr.write(f"chart push failed: {e}\\n")\n'
        "    return 0\n\n\n"
        "main()\n"
    )


def _workflow(tmp_path, scripts: dict[str, str]) -> Path:
    root = tmp_path / "workflow"
    for name, code in scripts.items():
        _write(root / "agents" / "bench" / "scripts" / name, code)
    return root


_LINE_CALL = [
    "render_chart(",
    '    chart_type="line",',
    "    data=data,",
    '    label="bench/metrics",',
    '    title="Metrics",',
    '    x="step",',
    '    y="value",',
    ")",
]


def test_charts_duplicate_label_title_across_files(tmp_path):
    root = _workflow(
        tmp_path,
        {
            "a.py": _chart_script(_LINE_CALL),
            "b.py": _chart_script(_LINE_CALL),
        },
    )
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-dup]" in r.stdout


def test_charts_heatmap_missing_value(tmp_path):
    root = _workflow(
        tmp_path,
        {
            "heat.py": _chart_script(
                [
                    "render_chart(",
                    '    chart_type="heatmap",',
                    "    data=data,",
                    '    label="bench/matrix",',
                    '    title="Matrix",',
                    '    x="col",',
                    '    y="row",',
                    ")",
                ]
            ),
        },
    )
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-heatmap]" in r.stdout


def test_charts_pareto_missing_directions(tmp_path):
    root = _workflow(
        tmp_path,
        {
            "front.py": _chart_script(
                [
                    "render_chart(",
                    '    chart_type="pareto",',
                    "    data=data,",
                    '    label="bench/front",',
                    '    title="Front",',
                    '    x="latency",',
                    '    y="accuracy",',
                    ")",
                ]
            ),
        },
    )
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-pareto]" in r.stdout


def test_charts_bare_call_outside_try(tmp_path):
    code = (
        _IMPORT_GUARD
        + 'render_chart(chart_type="line", data=[{"step": 1}], '
        'label="bare/call", title="Bare", x="step", y="v")\n'
    )
    root = _workflow(tmp_path, {"bare.py": code})
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-try]" in r.stdout


def test_charts_label_must_be_string_literal(tmp_path):
    var_call = [
        "render_chart(",
        '    chart_type="line",',
        "    data=data,",
        "    label=name,",
        '    title="Var",',
        '    x="step",',
        '    y="value",',
        ")",
    ]
    fst_call = [
        line if "label=" not in line else '    label=f"{name}",' for line in var_call
    ]
    var_root = _workflow(tmp_path, {"var.py": 'name = "bench/var"\n\n' + _chart_script(var_call)})
    fst_root = _workflow(
        tmp_path / "fst",
        {"fst.py": 'name = "bench/fst"\n\n' + _chart_script(fst_call)},
    )
    for root in (var_root, fst_root):
        r = _run("check_charts.py", root)
        assert r.returncode == 1, root
        assert "[chart-literal]" in r.stdout


def test_charts_compliant_line_chart(tmp_path):
    root = _workflow(tmp_path, {"bench_plot.py": _chart_script(_LINE_CALL)})
    r = _run("check_charts.py", root)
    assert r.returncode == 0, r.stdout
    assert "1 files / 1 call sites" in r.stdout


def test_charts_no_call_sites_zero_legal(tmp_path):
    root = _workflow(tmp_path, {"plain.py": "print('no chart here')\n"})
    r = _run("check_charts.py", root)
    assert r.returncode == 0, r.stdout
    assert "0 call sites" in r.stdout


def test_charts_alias_import_flagged(tmp_path):
    """from orca.chart import render_chart as rc 后 rc(...) 调用点不可静态识别。"""
    code = (
        "try:\n"
        "    from orca.chart import render_chart as rc\n"
        "except Exception:\n"
        "    rc = None\n"
    )
    root = _workflow(tmp_path, {"aliased.py": code})
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-call-form]" in r.stdout


def test_charts_attribute_call_flagged(tmp_path):
    """oc.render_chart(...) 属性调用形态偏离样板（即使参数全是字面量）。"""
    call = [
        "oc.render_chart(",
        '    chart_type="line",',
        "    data=data,",
        '    label="bench/attr",',
        '    title="Attr",',
        '    x="step",',
        '    y="value",',
        ")",
    ]
    body = "\n".join("        " + line for line in call)
    code = "import orca.chart as oc\n\n\ndef main():\n    data = [1]\n    try:\n" + body + "\n    except Exception:\n        pass\n"
    root = _workflow(tmp_path, {"attr_call.py": code})
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-call-form]" in r.stdout


def test_charts_rebinding_flagged(tmp_path):
    """f = render_chart 重绑定后的调用点不可静态识别。"""
    root = _workflow(
        tmp_path,
        {"rebind.py": "from orca.chart import render_chart\n\nf = render_chart\n"},
    )
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-call-form]" in r.stdout


def test_charts_star_kwargs_flagged(tmp_path):
    call = [
        "render_chart(",
        "    **kwargs,",
        ")",
    ]
    root = _workflow(
        tmp_path,
        {
            "star.py": _chart_script(call)
            + "\n\nkwargs = dict(chart_type='line', label='a', title='b', data=[])\n"
        },
    )
    r = _run("check_charts.py", root)
    assert r.returncode == 1
    assert "[chart-literal]" in r.stdout


def test_charts_missing_path_and_file_input_exit_2(tmp_path):
    r = _run("check_charts.py", tmp_path / "nowhere")
    assert r.returncode == 2
    assert r.stderr.strip()

    yml = _write(tmp_path / "wf.yaml", "name: demo\n")
    r = _run("check_charts.py", yml)
    assert r.returncode == 2
    assert "目录" in r.stderr


def test_charts_non_utf8_reads_with_warn(tmp_path):
    root = tmp_path / "workflow"
    scripts = root / "agents" / "bench" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "commented.py").write_bytes(b"# note \xff\xfe\n")
    r = _run("check_charts.py", root)
    assert r.returncode == 0
    assert "[warn]" in r.stdout
