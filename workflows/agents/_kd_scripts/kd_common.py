"""kd_common.py —— KD-NAS 新三脚本（pick_variant / tune_latency / recorder）共享的确定性 helper。

无 LLM、无网络、不读时钟、不读随机。fail loud。

关键约定：
  - variant_id = ``.py`` 文件名 stem；done 谓词跨 run 复用的核心。
  - ``variant_sha256`` = 变体 ``.py`` 字节 sha256（文件改了 → 重做）。
  - ``latency_provider_id`` = ``"<path::func>|<sha16>"``（换 latency 脚本 → 重做）。
  - ``target_latency_us`` 浮点归一比较。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any

# KNOBS leverage 缩容优先级（高→低）。禁止字母序（"low"<"medium"<"high" 反了）。
RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
VALID_LEVERAGE = set(RANK)
LEVERAGE_DEFAULT = "medium"

# ── accuracy_baseline_kind → best 方向（单一真相源）────────
# 当前真实消费者：``viz_kd_stage._push_pareto_front``（import 本函数判轴方向）+
# ``kd_common.compute_met_accuracy_absolute``（内部调本函数判 met_accuracy）。
# finalize_kd 不 import kd_common（其文本报告用本地 kind 字面），仅在概念上对齐。
# 越高越好（best=max）：snr / acc；越低越好（best=min）：mse / nmse / ber / db。
HIGHER_BETTER_KINDS = {"snr", "acc"}
LOWER_BETTER_KINDS = {"mse", "nmse", "ber", "db"}


def accuracy_direction(kind: str) -> str:
    """accuracy kind → best 方向：``"max"``（越高越好）/ ``"min"``（越低越好）/ ``""``（未知）。

    真实消费者：``viz_kd_stage._push_pareto_front`` + ``kd_common.compute_met_accuracy_absolute``。
    未知 kind → 空串（caller 须 fail loud / 低置信，**不** auto 猜方向）。
    """
    k = (kind or "").strip().lower()
    if k in HIGHER_BETTER_KINDS:
        return "max"
    if k in LOWER_BETTER_KINDS:
        return "min"
    return ""


def to_float(v: Any) -> float | None:
    """宽松转 float（坐标图过滤用）：``None`` / bool / NaN / 非数值 → ``None``。

    单一真相源（DRY）：viz_kd_stage._push_all_models_table / _push_pareto_front 共用（finalize_kd
    报告用本地 to_float-free 字面读法，不 import 此处）。防字节级副本漂移。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f  # NaN → None
    return None


def is_measured_row(row: dict[str, Any]) -> bool:
    """该 ledger 行是否携带**真实** accuracy 测量（非 ``accuracy=0`` 哨兵）。

    哨兵行（须剔除，否则在 min 方向 kind 下会以 ``accuracy=0`` 虚假占据帕累托前沿）：
      - ``FAIL_latency``：变体未进训练池（gate 阶段落账，latency 是真测的、accuracy=0 哨兵）。
      - ``FAIL_train``：训练崩 / 无 ckpt（accuracy 未测）。
      - ``FAIL_accuracy`` 且 ``accuracy_kind`` 为空：measure_student 子进程失败（rc!=0），
        ``accuracy=0`` 是哨兵；只有 ``accuracy_kind`` 非空才是 measure rc==0 的真测值
        （measure emit ``STUDENT_ACCURACY_KIND`` 才说明真跑到了解析阶段，真值可能恰为 0.0）。

    真测行（保留）：``status ∈ {SUCCESS, FAIL_accuracy}`` 且 ``accuracy_kind`` 非空。
    """
    status = row.get("status")
    if status not in ("SUCCESS", "FAIL_accuracy"):
        return False
    if not str(row.get("accuracy_kind") or "").strip():
        return False
    return True

# 终态：训练已发生过（不再重训，跨 run 跳过）。
TRAIN_DONE_STATUSES = {"SUCCESS", "FAIL_accuracy", "FAIL_train"}
# 全部终态（F1 fix 2026-08-04：必须含 FAIL_build —— gen_student validate_contract 3-strike 也是终态；
# 与 kd_reducer._LEDGER_STATUS / finalize_kd.known_statuses / viz_kd_stage._push_fail_status_bar 集合成员对齐）。
ALL_TERMINAL_STATUSES = TRAIN_DONE_STATUSES | {"FAIL_latency", "FAIL_build", "FAIL_export"}


def sha256_file(path: str) -> str:
    """文件字节 sha256（变体身份 / teacher ckpt 校验）。fail loud（读不了 → raise）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def acquire_run_lock(artifacts_dir: str, run_id: str, max_age_s: float = 3600.0) -> str:
    """单写者护栏——防并发 run 写同一 kd_artifacts_dir。

    心跳式锁（非内核锁；agent 跨节点无法持有内核锁）：写 ``<artifacts_dir>/orca.lock``
    = ``{run_id, ts}``。若已存在且 run_id 不同且 ts 在 ``max_age_s`` 内 → raise（另一 run 活跃）。
    同 run_id 重复 acquire → 刷新 ts（幂等）。坏锁/超时锁 → 覆盖。
    """
    import time
    os.makedirs(artifacts_dir, exist_ok=True)
    lock = os.path.join(artifacts_dir, "orca.lock")
    if os.path.isfile(lock):
        try:
            d = json.loads(open(lock, encoding="utf-8").read())
            other = d.get("run_id")
            ts = float(d.get("ts", 0))
            if other and other != run_id and (time.time() - ts) < max_age_s:
                raise RuntimeError(
                    f"kd_artifacts_dir {artifacts_dir} 被另一 run {other!r} 占用（lock 新鲜，"
                    f"{max_age_s}s 内）；并发 run 不支持，请等其结束或换 kd_artifacts_dir。"
                )
        except (json.JSONDecodeError, OSError, ValueError):
            pass  # 坏锁 → 覆盖
    import time as _t
    payload = {"run_id": run_id, "ts": _t.time()}
    with open(lock, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return lock


def provider_id(provider: str) -> str:
    """latency_provider 的稳定身份：``<path::func>|<文件 sha256 前 16>``。

    文件读不到（如非本地路径）→ 退化为 ``<provider>|<nobyhash>``（仍随 provider 串变化）。
    换 provider 脚本内容 → 身份变 → done 谓词判重做。
    """
    provider = (provider or "").strip()
    if "::" in provider:
        path_part = provider.split("::", 1)[0]
    else:
        path_part = provider
    try:
        return f"{provider}|{sha256_file(path_part)[:16]}"
    except OSError:
        return f"{provider}|nobyhash"


def validate_variant(mod: Any, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验 receiver KB 变体 .py 的契约：``build_model`` callable + ``DUMMY_INPUT.shape`` +
    ``KNOBS``（step<0 / leverage∈{high,medium,low} / default·min 数值）。

    返回 ``(dummy_input, knobs)``；``knobs={}`` 表示该 variant 不可调（latency 超阈即 FAIL_latency）。

    当前消费者：``test_receiver_variants.py::test_variant_knobs_valid``（receiver KB 契约
    smoke）。生产路径（tune_latency / gen_student）当前直接读 ``KNOBS`` 字段，未调本函数；
    保留作 receiver KB contract 文档化 + 单点测试入口（contract 改时只改一处）。
    """
    if not hasattr(mod, "build_model") or not callable(mod.build_model):
        raise AttributeError(f"{path} 无 callable build_model（契约必备）")
    di = getattr(mod, "DUMMY_INPUT", None)
    if not isinstance(di, dict) or not isinstance(di.get("shape"), list) or not di["shape"]:
        raise ValueError(
            f"{path} DUMMY_INPUT 缺 shape（list）——禁硬编码 shape 回退（用户须声明真实 I/O 维度）"
        )
    knobs = getattr(mod, "KNOBS", None)
    if knobs is None:
        return di, {}
    if not isinstance(knobs, dict) or not knobs:
        raise ValueError(f"{path} KNOBS 必须是非空 dict（得到 {type(knobs).__name__}）")
    for k, kn in knobs.items():
        if not isinstance(kn, dict):
            raise ValueError(f"{path} KNOBS[{k!r}] 不是 dict")
        for field in ("default", "min", "step", "leverage"):
            if field not in kn:
                raise ValueError(f"{path} KNOBS[{k!r}] 缺字段 {field!r}")
        if not isinstance(kn["step"], (int, float)) or kn["step"] >= 0:
            raise ValueError(f"{path} KNOBS[{k!r}].step 必须 <0（缩容方向；得到 {kn['step']!r}）")
        if kn["leverage"] not in RANK:
            raise ValueError(
                f"{path} KNOBS[{k!r}].leverage={kn['leverage']!r} 非法；须 ∈ {sorted(RANK)}"
            )
        if not isinstance(kn["default"], (int, float)) or not isinstance(kn["min"], (int, float)):
            raise ValueError(f"{path} KNOBS[{k!r}] default/min 须为数值")
    return di, knobs


# ── 绝对精度基线对比（§3 迁移：原 measure_student._compute_met_accuracy_absolute）─────
# 当前消费者：test_struct_kd_p7.py::TestMeasureStudentAbsoluteBaseline（绝对基线 + kind 方向
# 不变量守护）。无生产路径消费——保留作 receiver KB contract 不变量的文档化 + 单点测试入口
# （将来若 distill/eval agent 想显式对比绝对基线，从 kd_common 引即可，无需重写）。
# 注：原 measure_student._parse_accuracy（含 STUDENT_ACCURACY 协议优先级）**未**迁——活跃
# 路径的精度解析由 teacher_setup._parse_accuracy（含 TEACHER_/STUDENT_ACCURACY 优先级）承担，
# kd_common 不持有重复版（YAGNI / 防字节级副本漂移）。


def compute_met_accuracy_absolute(
    student_acc: float, detected_kind: str, baseline: float, kind_override: str,
) -> tuple[bool, str, str]:
    """绝对精度基线对比。返回 (met_accuracy, used_kind, confidence)。

    原迁移自 ``measure_student._compute_met_accuracy_absolute``；当前消费者：
    ``test_struct_kd_p7.py::TestMeasureStudentAbsoluteBaseline``（不变量守护测）。
    生产路径（distill / finalize）当前直接读 train_pipeline --mode eval 的 MET_ACCURACY 输出，
    不调本函数；本函数存在是为锁「绝对基线 + kind 方向」语义，便于未来显式对比复用。

    - ``kind_override`` 非空 → 锁方向；与 detected_kind 不符 → WARN（用 override）。
    - kind unknown → met=false, confidence=low（绝不静默 pass）。
    """
    used = (kind_override or "").strip().lower() or detected_kind
    confidence = "high"
    if kind_override and detected_kind != "unknown" and detected_kind != used:
        print(
            f"[kd_common] WARN: 自动检测 kind={detected_kind!r} 与 "
            f"--accuracy_baseline_kind={used!r} 不符；按 override {used!r} 判定。",
            file=sys.stderr,
        )
    direction = accuracy_direction(used)
    if direction == "max":
        met = bool(student_acc >= baseline)
    elif direction == "min":
        met = bool(student_acc <= baseline)
    else:
        met = False
        confidence = "low"
        print(
            f"[kd_common] WARN: accuracy kind 未知（detected={detected_kind!r}, "
            f"override={kind_override!r}）；无法判方向 → met_accuracy=false, confidence=low。",
            file=sys.stderr,
        )
    return met, used, confidence


def feq(a: Any, b: Any, rel: float = 1e-6) -> bool:
    """浮点近似相等（target_latency_us 字符串 vs float 比较）。非数 → False。"""
    try:
        af, bf = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(af - bf) <= rel * max(1.0, abs(af), abs(bf))


def read_ledger(path: str) -> list[dict[str, Any]]:
    """读 ledger.jsonl。**fail loud**：任一行非合法 JSON → raise（不 warn-跳过，
    否则坏行会静默缩小变体池 / 误判 done）。文件不存在 → 空列表。
    """
    if not path or not os.path.isfile(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                # 坏行 raise（ledger 是 domain 真相源，corruption → 误 skip/重跑）。
                raise ValueError(
                    f"ledger {path} 第 {lineno} 行非合法 JSON：{e}\n原文：{s[:200]!r}"
                ) from e
            if isinstance(obj, dict):
                out.append(obj)
    return out


# ── ledger 增量写 + 子进程辅助（crash-safety 契约级逻辑，勿复制）─────────────────
# ``append_ledger_row`` 的「主线程持 orca.lock + 逐行 write+flush」是 crash-safety 契约，
# 留在 kd_common 供活跃/未来脚本复用（当前活跃消费者：gen_student / distill 间接经
# ledger_reducer append）。


def append_ledger_row(ledger_path: str, row: dict[str, Any]) -> None:
    """增量 append 一行到 ledger（主线程持 orca.lock，逐行 write+flush，crash-safe）。

    契约：调用方必须先 ``acquire_run_lock`` 拿到单写者锁。JSONL append-only
    逐行原子；kill 不丢已完成行。
    """
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)) or ".", exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def run_subproc(argv: list[str]) -> tuple[int, str, str]:
    """跑子进程，返 (rc, stdout, stderr)。"""
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def parse_key(stdout: str, key: str) -> str | None:
    """从确定性脚本的 ``KEY: value`` stdout 行取值（首个匹配）。"""
    for line in stdout.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return None


def is_variant_done(
    rows_for_v: list[dict[str, Any]],
    cur_target_latency_us: float,
    cur_provider_id: str,
    variant_sha256: str,
) -> bool:
    """done 谓词（跨 run 复用核心）。

    任一**有效行**（variant_sha256 匹配 + latency_provider_id 匹配）满足：
      - status ∈ {SUCCESS, FAIL_accuracy, FAIL_train}：
          * SUCCESS：还须 ckpt 文件存在且非空且 latency_us_median ≤ 当前 target
            （target 调低到低于该 variant latency → 不再算 done，需重试更小 cfg）。
          * FAIL_accuracy / FAIL_train：训练已发生、结果已记 → done（「记账后下一个」，不重训）。
      - status == FAIL_latency：仅当 row.target_latency_us ≈ 当前 target（同 target 才算 done；
        改 target → 重试）。
    """
    for r in rows_for_v:
        if r.get("variant_sha256") != variant_sha256:
            continue
        if r.get("latency_provider_id") != cur_provider_id:
            continue
        status = r.get("status")
        if status in TRAIN_DONE_STATUSES:
            if status == "SUCCESS":
                ckpt = r.get("ckpt", "")
                lat = r.get("latency_us_median")
                ckpt_ok = bool(ckpt) and os.path.isfile(ckpt) and os.path.getsize(ckpt) > 0
                lat_ok = lat is not None and float(lat) <= float(cur_target_latency_us)
                if ckpt_ok and lat_ok:
                    return True
                # SUCCESS 但 ckpt 丢了 / latency 超 new target → 不算 done，落空继续找
            else:
                return True
        elif status == "FAIL_latency":
            if feq(r.get("target_latency_us"), cur_target_latency_us):
                return True
    return False
