"""test_kd_train_script.py —— kd-train-script agent 关键不变量测试（Phase 2：叶子化 codegen）。

覆盖（Phase 2 切换后的新契约）：
- folder-agent 结构契约（agent.md / SKILL.md / generation workflow / 2 个 checklist 齐全 +
  关键不变量文本：产 4 叶子 + run_config.yaml + run.sh，不产单体 train_pipeline.py）；
- 4 个叶子骨架（references/templates/leaves/*.py.skel）py_compile + AST 签名匹配引擎 loader 契约；
- 旧单体 references/templates/train_pipeline.py 已删（回归门：禁回归到单体）；
- fidelity_check.py（叶子模式 --leaves_dir）：对 demo fixture PASS + AST 自包含 / 签名 / kind 方向硬校验
  + loss drift / kind direction mismatch fail loud；
- gen_train_script output_schema 切换：train_pipeline_path 指固定引擎入口（不再指生成脚本）+
  additive leaves_dir / run_config_path / run_sh_path。

引擎本身（KDTrainer 三 mode + resume + early-stop）的覆盖在 ``test_kd_engine_trainer.py``
（Phase 1，33 单测）；此文件只覆盖 codegen 契约 + fidelity_check 工具。
"""

from __future__ import annotations

import ast
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
LEAVES_SKEL_DIR = AGENT_DIR / "references" / "templates" / "leaves"
OLD_TEMPLATE = AGENT_DIR / "references" / "templates" / "train_pipeline.py"
FIDELITY_CHECK = AGENT_DIR / "scripts" / "fidelity_check.py"
ENGINE_ENTRY = REPO / "workflows" / "agents" / "_kd_scripts" / "train_pipeline.py"

USER_TRAIN_PY = REPO / "examples" / "kd-nas-demo" / "train.py"
USER_EVAL_PY = REPO / "examples" / "kd-nas-demo" / "test_student.py"
BASELINE_CONTRACT = REPO / "knowledge_base" / "families" / "receiver" / "spt_alt.py"

# Mirror kd/_leaves._LEAF_SIGNATURES — the contract the codegen must honour.
LEAF_SIGNATURES = {
    "loss.py": {"compute_loss": ["s_out", "y"]},
    "data.py": {"build_dataloader": ["batch_size"]},
    "eval.py": {"eval_metric": ["student", "device"]},
    "optim.py": {
        "build_optimizer": ["params", "lr"],
        "build_scheduler": ["optimizer", "epochs"],
    },
}


# ===========================================================================
# 1. folder-agent 结构契约
# ===========================================================================
def test_agent_files_all_present():
    for p in (SKILL_MD, AGENT_MD, GEN_WORKFLOW, CHECKLIST_TRAINING, CHECKLIST_CLI,
              FIDELITY_CHECK):
        assert p.is_file(), f"missing agent resource: {p}"


def test_old_monolithic_template_is_deleted():
    """Phase 2: the monolithic template must not come back."""
    assert not OLD_TEMPLATE.exists(), (
        f"{OLD_TEMPLATE} should be deleted in Phase 2 (leaf-based codegen replaces it)."
    )


def test_skill_md_describes_leaf_codegen():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "compute_loss" in text and "build_dataloader" in text
    assert "eval_metric" in text and "build_optimizer" in text and "build_scheduler" in text
    # The skill must point the codegen product at the four leaves (not a monolithic script).
    assert "leaves" in text.lower()
    # run_config.yaml + run.sh are part of the product surface.
    assert "run_config.yaml" in text
    assert "run.sh" in text


def test_agent_md_output_schema_switched():
    """agent.md output JSON must point train_pipeline_path at the fixed engine entry,
    not at a per-project generated script, and expose the additive leaf paths."""
    text = AGENT_MD.read_text(encoding="utf-8")
    # train_pipeline_path is the fixed engine entry (the codegen no longer produces a script).
    assert "train_pipeline_path" in text
    assert "leaves_dir" in text
    assert "run_config_path" in text
    assert "run_sh_path" in text
    # The agent must NOT claim to produce a monolithic train_pipeline.py.
    assert "不产单体" in text or "不产" in text


def test_generation_workflow_doc_complete():
    text = GEN_WORKFLOW.read_text(encoding="utf-8")
    # The workflow must talk about leaves + self-containment.
    assert "self-contained" in text.lower() or "自包含" in text
    assert "AST signature" in text or "AST 签名" in text
    # Forbidden tokens (regression guard: the doc must warn against them).
    for token in ("DDP", "torchrun", "nas_agent.train.distillation"):
        assert token in text, (
            f"workflow doc should mention forbidden token {token!r} (regression guard)"
        )


def test_checklists_have_critical_items():
    for cl in (CHECKLIST_TRAINING, CHECKLIST_CLI):
        text = cl.read_text(encoding="utf-8")
        # Each checklist must reference the leaf contract.
        assert "compute_loss" in text or "leaf" in text.lower() or "叶子" in text, (
            f"{cl.name} must reference the leaf contract"
        )
        # AST self-containment / signature are first-class checks now.
        assert "AST" in text or "ast" in text, (
            f"{cl.name} must reference AST self-containment / signature checks"
        )


# ===========================================================================
# 2. 叶子骨架静态校验
# ===========================================================================
def test_leaf_skeletons_exist():
    files = {"loss.py.skel", "data.py.skel", "eval.py.skel", "optim.py.skel"}
    actual = {p.name for p in LEAVES_SKEL_DIR.glob("*.py.skel")}
    assert actual == files, f"leaf skeleton set mismatch: {actual} vs {files}"


@pytest.mark.parametrize("leaf_name,signatures", sorted(LEAF_SIGNATURES.items()))
def test_leaf_skeleton_py_compile_and_signature(leaf_name, signatures):
    """Each leaf skeleton py_compiles + carries the contract callables with the
    required positional args (engine loader enforces the same signature)."""
    skel = LEAVES_SKEL_DIR / f"{leaf_name[:-3]}.py.skel"
    src = skel.read_text(encoding="utf-8")
    # py_compile on the skeleton source (suffix-neutral).
    compile(src, str(skel), "exec")  # raises SyntaxError on failure
    tree = ast.parse(src)
    for fn_name, expected_args in signatures.items():
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == fn_name),
            None,
        )
        assert fn is not None, f"{leaf_name}: missing callable {fn_name!r}"
        args = fn.args.args
        ndef = len(fn.args.defaults)
        required = [a.arg for a in args[: len(args) - ndef]]
        assert required == expected_args, (
            f"{leaf_name}::{fn_name} required positional args = {required}, "
            f"expected {expected_args}"
        )
        # The skeleton body must raise NotImplementedError (unspecialised = fail loud).
        # Skip a leading docstring when scanning.
        body_nodes = fn.body
        if (body_nodes and isinstance(body_nodes[0], ast.Expr)
                and isinstance(body_nodes[0].value, ast.Constant)
                and isinstance(body_nodes[0].value.value, str)):
            body_nodes = body_nodes[1:]
        body_src = " ".join(ast.dump(n) for n in body_nodes)
        assert "NotImplementedError" in body_src, (
            f"{leaf_name}::{fn_name} skeleton must raise NotImplementedError "
            f"(unspecialised leaf = fail loud, never a placeholder fallback)"
        )


def test_leaf_skeletons_self_contained():
    """Skeletons themselves must satisfy the self-containment rule
    (no sibling / relative imports, only whitelisted top-level imports)."""
    from workflows.agents._kd_scripts.kd._leaves import _check_self_contained

    for skel in LEAVES_SKEL_DIR.glob("*.py.skel"):
        src = skel.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(skel))
        violations = _check_self_contained(tree, skel)
        assert not violations, f"{skel.name}: {violations}"


# ===========================================================================
# 3. fidelity_check.py（叶子模式）
# ===========================================================================
def _write_demo_leaves(user_dir: Path) -> dict[str, Path]:
    """Write the four demo leaves (ported from examples/kd-nas-demo) under user_dir."""
    leaves = {
        "loss.py": '''\
"""compute_loss ported from examples/kd-nas-demo/train.py (MSE)."""
import torch.nn.functional as F


def compute_loss(s_out, y):
    return F.mse_loss(s_out, y)
''',
        "data.py": '''\
"""Re-iterable random (x, y) batch generator (DUMMY_INPUT shape)."""
import torch

_SHAPE = (1, 4, 48, 64, 1)


class _RandomDataLoader:
    def __init__(self, batch_size=4, n_batches=8, shape=_SHAPE):
        self.batch_size = batch_size
        self.n_batches = n_batches
        self.shape = shape

    def __iter__(self):
        inner = tuple(self.shape[1:])
        for _ in range(self.n_batches):
            x = torch.randn(self.batch_size, *inner)
            y = torch.randn(self.batch_size, *inner)
            yield x, y

    def __len__(self):
        return self.n_batches


def build_dataloader(batch_size):
    return _RandomDataLoader(batch_size=batch_size)
''',
        "eval.py": '''\
"""eval_metric ported from examples/kd-nas-demo/test_student.py (NMSE)."""
import torch

DUMMY_SHAPE = [1, 4, 48, 64, 1]


def eval_metric(student, device):
    torch.manual_seed(20260725)
    n = 8
    x = torch.randn(n, *DUMMY_SHAPE[1:]).to(device)
    y = torch.randn(n, *DUMMY_SHAPE[1:]).to(device)
    student.eval()
    with torch.no_grad():
        out = student(x)
    target = y.view_as(out)
    num = float(torch.sum((out - target) ** 2).item())
    den = float(torch.sum(target ** 2).item()) + 1e-12
    return num / den, "nmse"
''',
        "optim.py": '''\
"""build_optimizer / build_scheduler — demo user has none, return None."""
def build_optimizer(params, lr):
    return None


def build_scheduler(optimizer, epochs):
    return None
''',
    }
    out = {}
    for name, src in leaves.items():
        p = user_dir / name
        p.write_text(src, encoding="utf-8")
        out[name] = p
    return out


def _dummy_input_json() -> str:
    return json.dumps({"shape": [1, 4, 48, 64, 1], "dtype": "float32"})


def _fidelity_env() -> dict[str, str]:
    """fidelity_check subprocess env: spt_alt.py imports _model8_blocks from its
    own dir, so the receiver dir must be on PYTHONPATH."""
    env = os.environ.copy()
    receiver_dir = str(BASELINE_CONTRACT.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{receiver_dir}:{existing}" if existing else receiver_dir
    )
    return env


def test_fidelity_check_demo_leaves_pass(tmp_path):
    """fidelity_check.py against the demo leaves + user train.py / eval script → PASS."""
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    _write_demo_leaves(leaves_dir)

    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(USER_TRAIN_PY),
            "--user_eval", str(USER_EVAL_PY),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "nmse",
            "--project_root", str(USER_TRAIN_PY.parent),
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 0, (
        f"fidelity_check rc={out.returncode}\nstdout:{out.stdout}\nstderr:{out.stderr}"
    )
    assert "FIDELITY: PASS" in out.stdout
    assert "LEAF_AST_OK: true" in out.stdout
    assert "KIND_DIRECTION_OK: true" in out.stdout


def test_fidelity_check_catches_loss_drift(tmp_path):
    """Wrong loss formula (L1 instead of MSE) → FIDELITY: FAIL."""
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    leaves = _write_demo_leaves(leaves_dir)
    # Corrupt the loss: swap MSE for L1.
    leaves["loss.py"].write_text(
        leaves["loss.py"].read_text(encoding="utf-8").replace("F.mse_loss", "F.l1_loss"),
        encoding="utf-8",
    )
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(USER_TRAIN_PY),
            "--user_eval", str(USER_EVAL_PY),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "nmse",
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 2, f"expected FAIL rc=2, got {out.returncode}\n{out.stdout}"
    assert "FIDELITY: FAIL" in out.stdout


def test_fidelity_check_catches_kind_direction_mismatch(tmp_path):
    """leaf kind=mse (min) vs --accuracy_baseline_kind=snr (max) → hard FAIL (D2)."""
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    _write_demo_leaves(leaves_dir)
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(USER_TRAIN_PY),
            "--user_eval", str(USER_EVAL_PY),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "snr",  # max-direction; leaf returns nmse (min)
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 2
    assert "KIND_DIRECTION_OK: false" in out.stdout


def test_fidelity_check_catches_signature_drift(tmp_path):
    """Renaming a required positional arg → AST signature mismatch → FAIL."""
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    leaves = _write_demo_leaves(leaves_dir)
    # Rename `s_out` → `output` in compute_loss.
    bad = leaves["loss.py"].read_text(encoding="utf-8").replace("s_out", "output")
    leaves["loss.py"].write_text(bad, encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(USER_TRAIN_PY),
            "--user_eval", str(USER_EVAL_PY),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "nmse",
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 2
    assert "LEAF_SIGNATURE_MISMATCH" in out.stderr or "LEAF_AST_OK: false" in out.stdout


def test_fidelity_check_catches_self_containment_violation(tmp_path):
    """A non-whitelisted top-level import → AST self-containment FAIL."""
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    leaves = _write_demo_leaves(leaves_dir)
    leaves["data.py"].write_text(
        leaves["data.py"].read_text(encoding="utf-8") + "\nimport pandas\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(USER_TRAIN_PY),
            "--user_eval", str(USER_EVAL_PY),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "nmse",
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 2
    assert "LEAF_AST_OK: false" in out.stdout


def _write_real_loader_user_train(tmp_path: Path) -> Path:
    """Write a user train.py that uses a real torchvision loader (no random).

    Mirrors the audit-run 6c2ebe user ``train.py``: the agent should have
    ported this verbatim instead of fabricating with ``torch.rand``.
    """
    src = '''\
"""User train.py — real torchvision MNIST loader (no synthetic data)."""
import torch
import torch.nn.functional as F
from torchvision import datasets, transforms


def compute_loss(s_out, y):
    return F.cross_entropy(s_out, y)


def build_dataloader(batch_size=128):
    t = transforms.Compose([transforms.ToTensor()])
    ds = datasets.MNIST("./data", train=True, download=True, transform=t)
    return torch.utils.data.DataLoader(ds, batch_size=batch_size, shuffle=True)
'''
    p = tmp_path / "train.py"
    p.write_text(src, encoding="utf-8")
    return p


def test_fidelity_check_catches_data_fabrication(tmp_path):
    """Leaf fabricates data with torch.rand but user train.py has a real
    torchvision loader → LEAF_FABRICATION_OK: false → FIDELITY: FAIL.

    Reproduces the audit-run 6c2ebe root cause: codegen synthesised
    ``torch.rand`` pixels + ``torch.randint`` labels because ``torchvision``
    was (wrongly) treated as outside the leaf import whitelist.
    """
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    leaves = _write_demo_leaves(leaves_dir)  # supplies loss/eval/optim + a random-data leaf
    # Overwrite data.py with the audit-run fabrication: random pixels + labels.
    leaves["data.py"].write_text(
        '''\
"""Fabricated data leaf — the audit-found anti-pattern."""
import torch
from torch.utils.data import DataLoader, TensorDataset

_N = 1024
_SHAPE = (1, 4, 48, 64, 1)


def build_dataloader(batch_size):
    images = torch.rand(_N, *_SHAPE[1:])
    labels = torch.randint(0, 10, (_N,))
    return DataLoader(TensorDataset(images, labels), batch_size=batch_size)
''',
        encoding="utf-8",
    )
    user_train = _write_real_loader_user_train(tmp_path)
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(user_train),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "nmse",
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 2, (
        f"expected FAIL rc=2, got {out.returncode}\nstdout:{out.stdout}\nstderr:{out.stderr}"
    )
    assert "LEAF_FABRICATION_OK: false" in out.stdout
    assert "fabrication detected" in out.stderr
    assert "FIDELITY: FAIL" in out.stdout


def test_fidelity_check_allows_user_synthetic_data(tmp_path):
    """When the user's own train.py uses ``torch.randn`` (synthetic-data demo,
    denoising autoencoder, etc.), the leaf's random data is a verbatim port,
    not fabrication → LEAF_FABRICATION_OK: true.

    Locks in the kd-nas-demo case: ``examples/kd-nas-demo/train.py`` uses
    ``torch.randn`` for its loader, so the demo leaf's ``torch.randn`` is
    ported-verbatim, not fabricated.
    """
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    _write_demo_leaves(leaves_dir)  # demo leaves use torch.randn
    # USER_TRAIN_PY (kd-nas-demo) uses torch.randn → user_synthetic=True.
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(USER_TRAIN_PY),
            "--user_eval", str(USER_EVAL_PY),
            "--dummy_input", _dummy_input_json(),
            "--model_path", str(BASELINE_CONTRACT),
            "--build_fn", "build_model", "--build_cfg", "{}",
            "--accuracy_baseline_kind", "nmse",
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 0, (
        f"fidelity_check rc={out.returncode}\nstdout:{out.stdout}\nstderr:{out.stderr}"
    )
    assert "LEAF_FABRICATION_OK: true" in out.stdout


def _fabrication_unit_case(leaf_body: str) -> bool:
    """Run _check_no_random_fabrication against ``leaf_body`` (data.py-shaped)
    with a real-loader user (user_synthetic=False). Returns True iff the
    check flagged fabrication (i.e. emitted at least one violation).
    """
    import importlib.util, tempfile
    spec = importlib.util.spec_from_file_location(
        "_fc_fabrication", FIDELITY_CHECK,
    )
    fc = importlib.util.module_from_spec(spec)
    sys.modules["_fc_fabrication"] = fc
    spec.loader.exec_module(fc)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(leaf_body)
        p = f.name
    violations = fc._check_no_random_fabrication(Path(p), user_synthetic=False)
    return len(violations) > 0


def test_fabrication_scan_catches_common_factory_variants():
    """The fabrication scan must catch every common random-data factory,
    not just ``torch.rand``/``torch.randint`` (LLMs that fail the audit will
    reach for variants).  Locks in: rand, randn, randint, normal, rand_like,
    randn_like, in-place uniform_/normal_/exponential_, np.random.*, stdlib random.
    """
    cases = [
        "import torch\ndef f():\n    x = torch.rand(8, 1, 28, 28)\n",
        "import torch\ndef f():\n    x = torch.randn(8, 1, 28, 28)\n",
        "import torch\ndef f():\n    x = torch.randint(0, 10, (8,))\n",
        "import torch\ndef f():\n    x = torch.normal(mean=0.0, std=1.0, size=(8,))\n",
        "import torch\ndef f():\n    x = torch.rand_like(torch.zeros(8))\n",
        "import torch\ndef f():\n    x = torch.randn_like(torch.zeros(8))\n",
        "import torch\ndef f():\n    x = torch.zeros(8); x.uniform_()\n",
        "import torch\ndef f():\n    x = torch.zeros(8); x.normal_()\n",
        "import torch\ndef f():\n    x = torch.zeros(8); x.exponential_()\n",
        "import numpy as np\ndef f():\n    x = np.random.rand(8, 28)\n",
        "import numpy as np\ndef f():\n    x = np.random.standard_normal((8, 28))\n",
        "import random\ndef f():\n    x = [random.random() for _ in range(8)]\n",
        "import random\ndef f():\n    x = random.randint(0, 10)\n",
    ]
    misses = [c.splitlines()[-1].strip() for c in cases if not _fabrication_unit_case(c)]
    assert not misses, (
        "anti-fabrication scan missed these factories (LLM could bypass): "
        + "; ".join(misses)
    )


def test_fabrication_scan_allows_seed_and_shuffle_primitives():
    """Seeds (``torch.manual_seed`` / ``np.random.seed`` /
    ``random.seed``) and ``torch.randperm`` (index permutation, not data
    synthesis) must NOT trip the fabrication check.
    """
    cases = [
        "import torch\ndef f():\n    torch.manual_seed(0)\n    idx = torch.randperm(100)\n    return idx\n",
        "import numpy as np\ndef f():\n    np.random.seed(0)\n",
        "import random\ndef f():\n    random.seed(0)\n",
        "import random\ndef f():\n    r = random.Random(0)\n",
        "import numpy as np\ndef f():\n    rng = np.random.default_rng(0)\n",
    ]
    false_positives = [
        c.splitlines()[-1].strip() for c in cases if _fabrication_unit_case(c)
    ]
    assert not false_positives, (
        "anti-fabrication scan false-positived on legitimate seed/shuffle: "
        + "; ".join(false_positives)
    )


def test_fidelity_check_fails_loud_on_missing_user_train(tmp_path):
    """``--user_train`` pointing at a non-existent file → rc=2 + stderr
    (fail-loud per CONTRACTS §3), not a bare FileNotFoundError traceback.
    """
    leaves_dir = tmp_path / "user"
    leaves_dir.mkdir()
    _write_demo_leaves(leaves_dir)
    out = subprocess.run(
        [
            sys.executable, str(FIDELITY_CHECK),
            "--leaves_dir", str(leaves_dir),
            "--user_train", str(tmp_path / "nonexistent_train.py"),
            "--dummy_input", _dummy_input_json(),
            "--accuracy_baseline_kind", "nmse",
        ],
        capture_output=True, text=True, env=_fidelity_env(),
    )
    assert out.returncode == 2, (
        f"expected rc=2 fail-loud, got {out.returncode}\nstderr:{out.stderr}"
    )
    assert "USER_TRAIN_MISSING" in out.stderr
    assert "FIDELITY: FAIL" in out.stderr
    # Bare traceback means we didn't catch it.
    assert "Traceback" not in out.stderr


def test_leaf_import_whitelist_contains_standard_scipy_stack():
    """The leaf import whitelist MUST include the pip scientific stack
    (torchvision / PIL / scipy / sklearn) — these are standard packages, not
    user-project modules.  Locks in the audit-found root cause: ``torchvision``
    being absent from the whitelist forced codegen to fabricate data.
    """
    import importlib.util
    leaves_spec = importlib.util.spec_from_file_location(
        "_leaves_whitelist_check",
        REPO / "workflows" / "agents" / "_kd_scripts" / "kd" / "_leaves.py",
    )
    leaves_mod = importlib.util.module_from_spec(leaves_spec)
    sys.modules["_leaves_whitelist_check"] = leaves_mod
    leaves_spec.loader.exec_module(leaves_mod)

    fid_spec = importlib.util.spec_from_file_location(
        "_fidelity_whitelist_check", FIDELITY_CHECK,
    )
    fid_mod = importlib.util.module_from_spec(fid_spec)
    sys.modules["_fidelity_whitelist_check"] = fid_mod
    fid_spec.loader.exec_module(fid_mod)

    # Parity — the two whitelists must agree.
    assert leaves_mod._LEAF_IMPORT_WHITELIST == fid_mod._LEAF_IMPORT_WHITELIST
    # The audit-found fix: standard scientific stack must be present.
    wl = leaves_mod._LEAF_IMPORT_WHITELIST
    for pkg in ("torch", "torchvision", "torchaudio", "numpy", "scipy",
                "sklearn", "PIL"):
        assert pkg in wl, (
            f"{pkg!r} missing from leaf import whitelist — its absence was the "
            f"audit-found root cause of KD-NAS codegen data fabrication"
        )


# ===========================================================================
# 4. kd-nas.yaml gen_train_script schema 切换（回归门）
# ===========================================================================
def test_kd_nas_yaml_gen_train_script_schema_switched():
    yaml_path = REPO / "workflows" / "kd-nas.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    # Additive fields must be present in gen_train_script output_schema.
    assert "leaves_dir" in text
    assert "run_config_path" in text
    assert "run_sh_path" in text
    # The schema description must point train_pipeline_path at the fixed engine entry.
    assert "固定引擎入口" in text or "_kd_scripts/train_pipeline.py" in text


# ===========================================================================
# 5. 固定引擎入口存在 + --help 列出 --artifacts_dir（回归门）
# ===========================================================================
def test_engine_entry_help_lists_artifacts_dir():
    assert ENGINE_ENTRY.is_file(), f"engine entry missing: {ENGINE_ENTRY}"
    env = os.environ.copy()
    env["ORCA_KD_SCRIPTS_DIR"] = str(ENGINE_ENTRY.parent)
    out = subprocess.run(
        [sys.executable, str(ENGINE_ENTRY), "--help"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    assert "--artifacts_dir" in out.stdout
    assert "--mode" in out.stdout and "--experiment" in out.stdout
