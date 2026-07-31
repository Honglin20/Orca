"""test_kd_train_script.py —— kd-train-script agent 关键不变量测试。

覆盖：
- folder-agent 结构契约（agent.md / SKILL.md / generation workflow / 2 个
  checklist 文件齐全 + 关键不变量文本）
- 参考模板 ``references/templates/train_pipeline.py`` 静态校验
  （py_compile / --help / stable base CLI / 无 distributed+sandwich 残留 /
  用 kd 库 / mode dispatch 顺序）
- 功能 smoke：teacher 模式（placeholder + 真 user train.py）+ distill 模式
  （端到端：先构造 teacher_cache.pt → distill 跑通）
- fail-loud 路径（teacher 缺 --model_path / distill 缺 --student_model_path
  / --teacher_cache）

不依赖 GPU（全 CPU），不嵌入 workflow yaml。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO / "workflows" / "agents" / "kd-train-script"
SKILL_MD = AGENT_DIR / "SKILL.md"
AGENT_MD = AGENT_DIR / "agent.md"
GEN_WORKFLOW = AGENT_DIR / "references" / "workflows" / "train_pipeline_script_generation.md"
CHECKLIST_TRAINING = (
    AGENT_DIR / "references" / "workflow-checklists" /
    "train_pipeline_script_generation" / "01_training.md"
)
CHECKLIST_CLI = (
    AGENT_DIR / "references" / "workflow-checklists" /
    "train_pipeline_script_generation" / "02_cli.md"
)
TEMPLATE = AGENT_DIR / "references" / "templates" / "train_pipeline.py"

# 测试输入（真实存在的契约文件）
KD_SCRIPTS_DIR = REPO / "workflows" / "agents" / "_kd_scripts"
TEACHER_MODEL = KD_SCRIPTS_DIR / "teacher_model.py"
USER_TRAIN_PY = REPO / "examples" / "kd-nas-demo" / "train.py"
STUDENT_VARIANT = REPO / "knowledge_base" / "families" / "receiver" / "spt_alt.py"

# Stable base CLI（generation workflow §1）—— train_pipeline.py 必须全部暴露。
STABLE_BASE_CLI = [
    "--mode", "--out_ckpt", "--epochs", "--lr", "--batch_size",
    "--device", "--seed", "--variant_id", "--build_fn", "--build_cfg",
    "--model_path", "--student_model_path", "--teacher_cache", "--kd_config",
    "--user_train_import", "--user_loss_fn", "--project_root", "--env_anchor",
]


# ===========================================================================
# helpers
# ===========================================================================
def _run_pipeline(args: list[str], *, env_extra: dict | None = None,
                  expect_success: bool = True) -> subprocess.CompletedProcess:
    """跑参考模板 train_pipeline.py，注入 ORCA_KD_SCRIPTS_DIR。"""
    env = dict(os.environ)
    env["ORCA_KD_SCRIPTS_DIR"] = str(KD_SCRIPTS_DIR)
    env.pop("ORCA_CHART_SOCK", None)  # 抑制 orca chart 副作用
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        [sys.executable, str(TEMPLATE), *args],
        capture_output=True, text=True, env=env, timeout=180,
    )
    if expect_success:
        assert r.returncode == 0, (
            f"train_pipeline.py 期望 exit 0 实际 {r.returncode}\n"
            f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
    return r


def _find_forbidden_code_tokens(path: Path, forbidden: set[str]) -> list[str]:
    """AST 扫描：检查禁用标识符是否出现在 **代码级** 使用（import / name /
    attribute access），跳过 docstring / 字符串字面量 / 注释。

    用 AST 而非裸 substring 是为了避免误判模块 docstring 里的说明性
    文本（如 "no DDP / torchrun / sandwich sampling" 出现在 docstring 里
    是合理的，不应触发 forbidden）。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: list[str] = []

    def matches_dotted(dotted: str) -> bool:
        """Check if a dotted path equals or starts with any forbidden token."""
        for fn in forbidden:
            if dotted == fn or dotted.startswith(fn + "."):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if matches_dotted(alias.name):
                    found.append(f"L{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if matches_dotted(mod):
                found.append(f"L{node.lineno}: from {mod} import ...")
            for n in node.names:
                if n.name in forbidden:
                    found.append(f"L{node.lineno}: from {mod} import {n.name}")
        elif isinstance(node, ast.Name):
            if node.id in forbidden:
                found.append(f"L{node.lineno}: name {node.id}")
        elif isinstance(node, ast.Attribute):
            # 构建完整 dotted path（如 nas_agent.train.distillation）
            parts: list[str] = []
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                dotted = ".".join(reversed(parts))
                if matches_dotted(dotted):
                    found.append(f"L{node.lineno}: attribute {dotted}")
    return found


def _build_teacher_cache(cache_path: Path, teacher_model_path: Path,
                         dummy_shape: list[int]) -> None:
    """直接用 kd.wrapper.TeacherCache.build 构造 4-字段 cache blob 落盘。

    镜像 teacher_setup.py 的输出格式（kd/wrapper.py TeacherCache.save 写盘
    的 4 字段：teacher_model_path / state_dict / hook_names / dummy_input_shape）。
    不调 teacher_setup.py 是为了测试自包含（避免 onnxruntime 依赖）。
    """
    sys.path.insert(0, str(KD_SCRIPTS_DIR))
    from kd.wrapper import TeacherCache  # type: ignore  # noqa: E402

    # 构造 fresh teacher（零参默认架构）+ 默认 init（不需要训练就能 cache）
    spec = importlib.util.spec_from_file_location("_cache_teacher", str(teacher_model_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    teacher = mod.build_model()
    hook_names = list(teacher.feature_hook_names())

    cache = TeacherCache.build(
        teacher_model_path=str(teacher_model_path),
        teacher_state_dict=teacher.state_dict(),
        hook_names=hook_names,
        dummy_input_shape=dummy_shape,
    )
    cache.save(str(cache_path))


# ===========================================================================
# folder-agent 结构契约
# ===========================================================================
def test_agent_files_all_present():
    """所有 folder-agent 文件齐全（agent.md / SKILL.md / generation workflow /
    2 checklist / template）。"""
    for p in (AGENT_MD, SKILL_MD, GEN_WORKFLOW, CHECKLIST_TRAINING,
              CHECKLIST_CLI, TEMPLATE):
        assert p.is_file(), f"missing agent file: {p}"


def test_agent_md_has_strong_directive():
    """agent.md 必须开头声明唯一职责 + 红线（❌）+ fail loud。

    抗 spec-审查（deepseek-v4-flash 等 LLM 易把 agent.md 当 spec 评判而不执行）。
    """
    text = AGENT_MD.read_text(encoding="utf-8")
    assert "唯一职责" in text, "agent.md 开头缺「唯一职责」执行指令"
    assert "红线" in text, "agent.md 缺「红线」section"
    assert "❌" in text, "agent.md 缺 ❌ 红线列表"
    # 关键红线（KD-NAS 5 处适配的核心约束）
    assert "DDP" in text
    assert "sandwich" in text
    assert "kd.compose" in text
    assert "nas_agent.train.distillation" in text  # 红线里点名禁用
    assert "自包含" in text or "绝不 import" in text
    # 输出 JSON contract（v4 嵌入 workflow：最终消息是 JSON {train_pipeline_path}）
    assert "train_pipeline_path" in text, (
        "agent.md 输出 contract 应声明 train_pipeline_path（v4 workflow 嵌入后的 JSON 终点）"
    )


def test_skill_md_workflow_three_steps():
    """SKILL.md 必须有 Step 1/2/3 三步工作流 + 3 层校验 + verifier prompt。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "Step 1: Load Context" in text
    assert "Step 2: Generate" in text
    assert "Step 3: Validate" in text
    # 3 层校验（Layer 1 静态 / Layer 2 smoke / Layer 3 verifier）
    assert "Layer 1" in text and "Layer 2" in text and "Layer 3" in text
    # verifier prompt 模板（参考 nas-agent-pipeline 的 workflow-verifier 调用）
    assert "workflow-verifier" in text.lower() or "VERDICT:" in text
    # 输出摘要 contract
    assert "OUTPUT_DIR:" in text
    assert "MODES_SUPPORTED:" in text


def test_generation_workflow_doc_complete():
    """generation workflow 文档必须覆盖 9 个核心 sections（CLI / Model / Loss /
    Optimizer / Teacher / Distill / KD / Path / LiveChart）+ Validation + Forbidden。"""
    text = GEN_WORKFLOW.read_text(encoding="utf-8")
    for section in (
        "### 1. CLI And Runtime Args",
        "### 2. Model Construction",
        "### 3. User Task Loss",
        "### 4. Optimizer, Scheduler",
        "### 5. Teacher Mode",
        "### 6. Distill Mode",
        "### 7. KD Loss Composition",
        "### 8. Path Handling",
        "### 9. Live Chart Push",
    ):
        assert section in text, f"generation workflow 缺 section: {section}"
    # KD-NAS 5 处适配（开头明确列出）
    assert "sandwich" in text.lower()
    assert "DDP" in text
    assert "importlib" in text
    assert "kd.compose" in text
    # Validation + Forbidden
    assert "## Validation" in text
    assert "## Forbidden" in text
    # ckpt schemas（teacher / distill）
    assert "TEACHER_CKPT:" in text
    assert "STUDENT_CKPT:" in text
    assert "KD_PROXY_MSE:" in text


def test_checklists_have_critical_items():
    """两个 checklist 文件必须含 [CRITICAL] 项（verifier 必须核查的硬约束）。"""
    t = CHECKLIST_TRAINING.read_text(encoding="utf-8")
    c = CHECKLIST_CLI.read_text(encoding="utf-8")
    assert "[CRITICAL]" in t, "01_training.md 必须有 [CRITICAL] 项"
    assert "[CRITICAL]" in c, "02_cli.md 必须有 [CRITICAL] 项"
    # 关键 critical 项（不可遗漏）
    assert "No Distributed" in t or "No DDP" in t, (
        "01_training.md 缺 [CRITICAL] No Distributed/DDP 项"
    )
    assert "Self-Contained" in t, "01_training.md 缺 [CRITICAL] Self-Contained 项"
    assert "Model Loaded By Path" in t, "01_training.md 缺 [CRITICAL] Model By Path 项"
    assert "KD Library" in t, "01_training.md 缺 [CRITICAL] KD Library 项"
    assert "Stable Base CLI" in c, "02_cli.md 缺 [CRITICAL] Stable Base CLI 项"
    assert "--mode Required" in c, "02_cli.md 缺 [CRITICAL] --mode Required 项"


# ===========================================================================
# 参考模板静态校验
# ===========================================================================
def test_template_py_compile():
    """references/templates/train_pipeline.py 通过 py_compile。"""
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(TEMPLATE)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"py_compile 失败:\n{r.stderr}"


def test_template_help_lists_stable_cli():
    """--help 列出全部 stable base CLI flag。"""
    r = subprocess.run(
        [sys.executable, str(TEMPLATE), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"--help 失败:\n{r.stderr}"
    for flag in STABLE_BASE_CLI:
        assert flag in r.stdout, f"--help 输出缺 flag: {flag}"


def test_template_no_forbidden_code_tokens():
    """参考模板的 **代码级**（import / name / attribute）扫描：无 DDP /
    torchrun / DistributedDataParallel / setup_distributed / set_sample_config
    / sandwich / sample_sandwich_arch_configs / is_main_process / get_rank /
    nas_agent.train.distillation / logits_kd_loss / soft_bce_kd_loss /
    cosine_kd_loss。

    用 AST 扫描而非裸 substring：模块 docstring 里会出现 "no DDP / no
    torchrun / no sandwich sampling" 等说明文字，这是合规的（说明意图），
    不应被误判为残留代码。
    """
    forbidden = {
        # distributed / launch
        "torch.distributed",
        "DistributedDataParallel",
        "setup_distributed",
        "is_main_process",
        "get_rank",
        "get_local_rank",
        "save_checkpoint_ddp",
        "set_sample_config_ddp",
        # nas sandwich sampling
        "sandwich",
        "sample_sandwich_arch_configs",
        "set_sample_config",
        # nas distillation library
        "nas_agent.train.distillation",
        "logits_kd_loss",
        "soft_bce_kd_loss",
        "cosine_kd_loss",
    }
    found = _find_forbidden_code_tokens(TEMPLATE, forbidden)
    assert not found, (
        f"train_pipeline.py 代码级含禁用 token（docstring 字面量不算）：\n"
        + "\n".join(found)
    )


def test_template_uses_kd_library():
    """distill mode 用 kd.compose.build_kd_loss / kd.wrapper / kd.ema（lazy import）。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "from kd.compose import build_kd_loss" in text
    assert "from kd.wrapper import KDStudentWrapper" in text
    assert "from kd.wrapper import" in text and "TeacherCache" in text
    assert "from kd.ema import MeanTeacherEMA" in text
    # lazy（在函数体内，不是 top-level）
    distill_start = text.find("def run_distill_mode")
    assert distill_start > 0, "缺 run_distill_mode 函数"
    distill_body = text[distill_start:]
    for line in (
        "from kd.compose import build_kd_loss",
        "from kd.wrapper import KDStudentWrapper, TeacherCache",
        "from kd.ema import MeanTeacherEMA",
    ):
        assert line in distill_body, (
            f"KD import 必须在 run_distill_mode 函数体内（lazy）：{line}"
        )


def test_template_mode_dispatch_in_main():
    """main() 按 args.mode 分发 + 在 dispatch 前解析 user_loss/build_dataloader
    + dispatch 正确性（``if args.mode == "teacher"`` 块内真调 run_teacher_mode）。

    用 AST 验证而不靠 substring 偏移：避免有人把分支 body 互换（teacher 块调
    run_distill_mode）而 substring 测试仍 pass。
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    tree = ast.parse(text)

    main_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
            break
    assert main_fn is not None, "train_pipeline.py 缺 main() 函数"

    load_line = None
    teacher_dispatch_line = None

    for node in ast.walk(main_fn):
        # _load_user_train() 调用
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_load_user_train"):
            load_line = node.lineno
        # if args.mode == "teacher": 块内调 run_teacher_mode
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Attribute)
                    and isinstance(test.left.value, ast.Name)
                    and test.left.value.id == "args"
                    and test.left.attr == "mode"):
                for comp in test.comparators:
                    if (isinstance(comp, ast.Constant)
                            and comp.value == "teacher"):
                        for sub in ast.walk(node):
                            if (isinstance(sub, ast.Call)
                                    and isinstance(sub.func, ast.Name)
                                    and sub.func.id == "run_teacher_mode"):
                                teacher_dispatch_line = sub.lineno

    assert load_line is not None, "main() 缺 _load_user_train() 调用"
    assert teacher_dispatch_line is not None, (
        "main() 缺 `if args.mode == 'teacher': run_teacher_mode(...)` 分支 "
        "（dispatch 错配：teacher 块未调 run_teacher_mode）"
    )
    assert teacher_dispatch_line > load_line, (
        "run_teacher_mode 调用必须在 _load_user_train() 之后（两模式共享解析结果）"
    )


# ===========================================================================
# 功能 smoke（teacher 模式）
# ===========================================================================
def test_smoke_teacher_mode_placeholder(tmp_path):
    """teacher 模式 + placeholder fallback（不传 --user_train_import）：

    跑通 1 epoch + 产 TEACHER_CKPT + TASK_LOSS_FINAL + ckpt schema 正确。
    """
    out_ckpt = tmp_path / "teacher_placeholder.pth"
    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "placeholder_test",
    ])
    assert "TEACHER_CKPT:" in r.stdout
    assert "TASK_LOSS_FINAL:" in r.stdout
    assert out_ckpt.is_file()

    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    assert blob["mode"] == "teacher"
    assert blob["variant_id"] == "placeholder_test"
    assert "state_dict" in blob
    assert "build_cfg" in blob
    assert blob["epochs"] == 1
    assert isinstance(blob["final_loss"], float)


def test_smoke_teacher_mode_real_user_train(tmp_path):
    """teacher 模式 + 真 examples/kd-nas-demo/train.py（compute_loss MSE）：

    跑通 + loss 是有限数（非 NaN/Inf）+ state_dict 可 load 回 teacher。
    """
    out_ckpt = tmp_path / "teacher_real.pth"
    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--user_train_import", str(USER_TRAIN_PY),
        "--user_loss_fn", "compute_loss",
        "--project_root", str(USER_TRAIN_PY.parent),
    ])
    assert "TEACHER_CKPT:" in r.stdout
    for line in r.stdout.splitlines():
        if line.startswith("TASK_LOSS_FINAL:"):
            val = float(line.split(":", 1)[1].strip())
            assert val == val and val != float("inf"), f"loss 不是有限数: {val}"
            break
    else:
        pytest.fail("stdout 缺 TASK_LOSS_FINAL 行")

    # state_dict 可 load 回 fresh teacher
    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    spec = importlib.util.spec_from_file_location("_check_teacher", str(TEACHER_MODEL))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    fresh = mod.build_model()
    fresh.load_state_dict(blob["state_dict"])  # 不抛 = key 完全对齐


# ===========================================================================
# 功能 smoke（distill 模式，端到端）
# ===========================================================================
def test_smoke_distill_mode_end_to_end(tmp_path):
    """distill 模式端到端：

    1. 先用 kd.wrapper.TeacherCache.build 直接构造 teacher_cache.pt（4 字段 blob）
    2. 跑 train_pipeline.py --mode distill + spt_alt student + 真 user train.py
    3. 校验 STUDENT_CKPT / KD_LOSS_FINAL / KD_PROXY_MSE + ckpt schema
    """
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "student.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({"kd_losses": ["mse"], "weights": {"mse": 1.0}}),
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "smoke_student",
        "--user_train_import", str(USER_TRAIN_PY),
        "--user_loss_fn", "compute_loss",
        "--project_root", str(USER_TRAIN_PY.parent),
    ])
    assert "STUDENT_CKPT:" in r.stdout
    assert "KD_LOSS_FINAL:" in r.stdout
    assert "KD_PROXY_MSE:" in r.stdout
    assert out_ckpt.is_file()

    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    assert blob["mode"] == "distill"
    assert blob["variant_id"] == "smoke_student"
    assert "student_state_dict" in blob
    assert blob["student_cfg"] == {"num_blocks": 3, "embed_dim": 16}
    assert blob["kd_config"]["kd_losses"] == ["mse"]
    assert isinstance(blob["proxy_mse"], float)

    # student_state_dict 可 load 回 fresh student（按路径 import spt_alt）
    sys.path.insert(0, str(STUDENT_VARIANT.parent))
    try:
        spec = importlib.util.spec_from_file_location(
            "_check_student", str(STUDENT_VARIANT)
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        fresh_student = mod.build_model(num_blocks=3, embed_dim=16)
        fresh_student.load_state_dict(blob["student_state_dict"])
    finally:
        sys.path.remove(str(STUDENT_VARIANT.parent))


def test_smoke_distill_mode_kd_loss_finite(tmp_path):
    """distill 模式 KD loss 是有限数（不要求单调下降——随机数据不求收敛）。

    守门 KD composite 真能 forward + backward + optimizer.step 不崩。
    """
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "student2.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({"kd_losses": ["mse"], "weights": {"mse": 0.5}}),
        "--epochs", "2",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "kd_check",
        "--user_train_import", str(USER_TRAIN_PY),
        "--user_loss_fn", "compute_loss",
        "--project_root", str(USER_TRAIN_PY.parent),
    ])
    for line in r.stdout.splitlines():
        if line.startswith("KD_LOSS_FINAL:"):
            val = float(line.split(":", 1)[1].strip())
            assert val == val and val != float("inf"), f"KD loss 非有限: {val}"
            break
    else:
        pytest.fail("stdout 缺 KD_LOSS_FINAL 行")


def test_smoke_distill_mode_fitnets_kd(tmp_path):
    """distill mode + feature-KD (fitnets) 端到端 smoke。

    mse KD 路径不需要 ``kd_loss.prepare()``（无 adapter 参数）；fitnets 需要——
    OFD/FitNets adapter 经 ``prepare()`` 预建 + ``kd_parameters()`` 进 optimizer
    是 distill mode 最复杂的路径，单独守门（reviewer 🟡-2）。

    spt_alt + teacher_model 都暴露 ``feature_hook_names()`` 长度 2（KD-NAS 变体
    契约 + teacher 契约一致），fitnets 取中间层 hint。
    """
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "student_fitnets.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({
            "kd_losses": ["fitnets"],
            "weights": {"fitnets": 1.0},
        }),
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "fitnets_test",
        "--user_train_import", str(USER_TRAIN_PY),
        "--user_loss_fn", "compute_loss",
        "--project_root", str(USER_TRAIN_PY.parent),
    ])
    assert "STUDENT_CKPT:" in r.stdout
    assert "KD_LOSS_FINAL:" in r.stdout
    assert "KD_PROXY_MSE:" in r.stdout
    assert out_ckpt.is_file()

    # feature-KD 适配器参数应已落到 ckpt 训练（student_state_dict 可 load 回）
    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    assert blob["mode"] == "distill"
    assert blob["kd_config"]["kd_losses"] == ["fitnets"]
    for line in r.stdout.splitlines():
        if line.startswith("KD_LOSS_FINAL:"):
            val = float(line.split(":", 1)[1].strip())
            assert val == val and val != float("inf"), (
                f"fitnets KD loss 非有限: {val}（adapter forward/backward 应正常）"
            )
            break


# ===========================================================================
# fail-loud 路径
# ===========================================================================
def test_teacher_mode_missing_model_path_fails(tmp_path):
    """teacher 模式缺 --model_path → 非零退出 + stderr/stdout 报因（fail loud）。"""
    r = _run_pipeline([
        "--mode", "teacher",
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    assert "--model_path" in (r.stderr + r.stdout)


def test_distill_mode_missing_student_model_path_fails(tmp_path):
    """distill 模式缺 --student_model_path → 非零退出。"""
    r = _run_pipeline([
        "--mode", "distill",
        "--teacher_cache", str(tmp_path / "nonexistent.pt"),
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    assert "--student_model_path" in (r.stderr + r.stdout)


def test_distill_mode_missing_teacher_cache_fails(tmp_path):
    """distill 模式缺 --teacher_cache → 非零退出。"""
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    assert "--teacher_cache" in (r.stderr + r.stdout)


def test_distill_mode_teacher_cache_not_found(tmp_path):
    """distill 模式传不存在的 teacher_cache 路径 → 非零退出 + FileNotFoundError。

    TeacherCache.load 对缺失文件 raise FileNotFoundError（kd/wrapper.py）。
    """
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(tmp_path / "never_existed.pt"),
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    combined = r.stderr + r.stdout
    assert "teacher_cache" in combined or "FileNotFoundError" in combined


# ===========================================================================
# fail-loud: empty dataloader + missing loss fn（reviewer 🟡#1/#2/#3 修复守门）
# ===========================================================================
def _write_empty_loader_user_train(p: Path) -> None:
    """写一个 compute_loss OK 但 build_dataloader 返回空 list 的 user train.py。

    触发 NaN-loss fail-loud guard：epoch 0 n_batches=0 → last_avg 保持 nan
    → `math.isfinite(last_avg)` False → raise SystemExit（不写 NaN ckpt）。
    """
    p.write_text(
        "import torch.nn.functional as F\n"
        "def compute_loss(s_out, y):\n"
        "    return F.mse_loss(s_out, y)\n"
        "def build_dataloader():\n"
        "    return []  # empty loader — simulates broken/one-shot generator\n",
        encoding="utf-8",
    )


def test_teacher_mode_empty_dataloader_fails_loud(tmp_path):
    """teacher 模式 + 空 dataloader → 非零退出 + 不写 NaN ckpt（CLAUDE.md Rule 12）。

    守门 reviewer 🟡#1：dataloader 空时 last_avg 保持 nan，必须 raise 不静默落盘。
    """
    user_train = tmp_path / "empty_train.py"
    _write_empty_loader_user_train(user_train)
    out_ckpt = tmp_path / "should_not_exist.pth"

    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--user_train_import", str(user_train),
        "--user_loss_fn", "compute_loss",
    ], expect_success=False)

    assert r.returncode != 0, "空 dataloader 应 fail loud，不应 return 0"
    combined = r.stderr + r.stdout
    # 错误信息含 "no training batches" 或 "NaN"
    assert "no training batches" in combined.lower() or "nan" in combined.lower(), (
        f"错误信息应说明 dataloader 空 / NaN，实际：{combined}"
    )
    # 关键：ckpt 文件不应存在（不应写 NaN ckpt）
    assert not out_ckpt.exists(), (
        "空 dataloader 时不应写 ckpt（防 NaN teacher → NaN student → NaN proxy_mse 链）"
    )


def test_distill_mode_empty_dataloader_fails_loud(tmp_path):
    """distill 模式 + 空 dataloader → 非零退出 + 不写 NaN ckpt。

    distill mode 先 materialise 一个 batch 做 kd_loss.prepare（用 next(iter(dl))），
    所以空 loader 在 prepare 阶段就会 StopIteration → 未捕获 → 非零退出。
    这是 prepare 阶段的隐式 fail-loud（即使是空的也会响）。
    """
    user_train = tmp_path / "empty_train.py"
    _write_empty_loader_user_train(user_train)

    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "should_not_exist.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({"kd_losses": ["mse"], "weights": {"mse": 1.0}}),
        "--epochs", "1",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--user_train_import", str(user_train),
        "--user_loss_fn", "compute_loss",
    ], expect_success=False)

    assert r.returncode != 0
    # distill prepare 阶段 next(iter(dl)) 对空 list → StopIteration
    # 或 main loop 跑 0 batch → NaN guard
    assert not out_ckpt.exists(), "空 dataloader 时不应写 ckpt"


def test_user_train_module_missing_loss_fn_fails(tmp_path):
    """user train.py 缺 loss fn（--user_loss_fn 指向不存在的函数）→ 非零退出。

    覆盖 `_load_user_train` 的 AttributeError 分支（reviewer 🟡#3）。
    """
    # 写一个有 build_dataloader 但缺 compute_loss 的 user train.py
    user_train = tmp_path / "no_loss_fn.py"
    user_train.write_text(
        "import torch.nn.functional as F\n"
        # 故意不定义 compute_loss —— 只有 some_other_loss
        "def some_other_loss(s_out, y):\n"
        "    return F.l1_loss(s_out, y)\n"
        "def build_dataloader(batch_size=2):\n"
        "    class _L:\n"
        "        def __iter__(self):\n"
        "            import torch\n"
        "            for _ in range(2):\n"
        "                yield torch.randn(2, 4, 48, 64, 1), torch.randn(2, 4, 48, 64, 1)\n"
        "    return _L()\n",
        encoding="utf-8",
    )

    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--device", "cpu",
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--user_train_import", str(user_train),
        "--user_loss_fn", "compute_loss",  # 模块里没这个函数
    ], expect_success=False)

    assert r.returncode != 0
    combined = r.stderr + r.stdout
    # 错误信息含 AttributeError / "loss fn" / 模块名
    assert "compute_loss" in combined or "loss fn" in combined.lower() or \
           "AttributeError" in combined, (
        f"错误信息应说明 loss fn 缺失，实际：{combined}"
    )
