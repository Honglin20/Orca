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
import re
import subprocess
import sys
from typing import Any

# KNOBS leverage 缩容优先级（高→低）。禁止字母序（"low"<"medium"<"high" 反了）。
RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
VALID_LEVERAGE = set(RANK)
LEVERAGE_DEFAULT = "medium"

# ── accuracy_baseline_kind → best 方向（单一真相源）────────
# §4 doc sweep：原三处消费者（measure_student / viz_kd / kd-select.select_and_report）已删；
# 当前消费者：viz_kd_stage（标轴方向）+ kd_common.compute_met_accuracy_absolute（met 判定）+
# finalize_kd（report 文本）。单一真相源防「-20dB 误判比 -22dB 好」式方向反转
# （符号判定不可靠，必须显式 kind）。
# 越高越好（best=max）：snr / acc；越低越好（best=min）：mse / nmse / ber / db。
HIGHER_BETTER_KINDS = {"snr", "acc"}
LOWER_BETTER_KINDS = {"mse", "nmse", "ber", "db"}


def accuracy_direction(kind: str) -> str:
    """accuracy kind → best 方向：``"max"``（越高越好）/ ``"min"``（越低越好）/ ``""``（未知）。

    单一真相源（DRY）：viz_kd_stage 标轴方向 + kd_common.compute_met_accuracy_absolute 判
    met_accuracy + finalize_kd 报告文本全部经此函数。未知 kind → 空串（caller 须 fail loud /
    低置信，**不** auto 猜方向）。
    """
    k = (kind or "").strip().lower()
    if k in HIGHER_BETTER_KINDS:
        return "max"
    if k in LOWER_BETTER_KINDS:
        return "min"
    return ""


def to_float(v: Any) -> float | None:
    """宽松转 float（坐标图过滤用）：``None`` / bool / NaN / 非数值 → ``None``。

    单一真相源（DRY）：viz_kd_stage 与 finalize_kd 共用，防两份字节级副本漂移。
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
# 全部终态。
ALL_TERMINAL_STATUSES = TRAIN_DONE_STATUSES | {"FAIL_latency", "FAIL_export"}


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

    单一真相源（DRY）：原 ``pick_variant._validate_variant`` 守所有 receiver KB variant；
    迁到 ``kd_common`` 后 receiver variant 校验 + receiver KB 生成时自查共用，防止两份字节
    级副本漂移。返回 ``(dummy_input, knobs)``；``knobs={}`` 表示该 variant 不可调
    （latency 超阈即 FAIL_latency）。
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


# ── student accuracy 解析 + 绝对基线对比（§3 迁移：原 measure_student.py 不变量）───────
# 单一真相源：teacher_setup / finalize_kd / viz_kd_stage / 旧 measure_student 共用方向语义。
# measure_student 删除后，绝对基线对比的不变量留 kd_common——防三处字节级副本漂移。

_ACC_PATTERNS = [
    (re.compile(r"\baccuracy\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "acc"),
    (re.compile(r"\bNMSE\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", re.I), "nmse"),
    (re.compile(r"\bMSE\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", re.I), "mse"),
    (re.compile(r"\bBER\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "ber"),
    (re.compile(r"\bSNR[_-]?dB\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)", re.I), "snr"),
    (re.compile(r"\bSNR\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)", re.I), "snr"),
]


def parse_accuracy(stdout: str) -> tuple[float, str, str]:
    """从 eval stdout 解析精度（无 STUDENT_ACCURACY 协议时用）。返回 (value, kind, confidence)。

    单一真相源（DRY）：原 ``measure_student._parse_accuracy``；优先级为
    JSON-line → 裸 ``accuracy/NMSE/MSE/BER/SNR`` regex（无 STUDENT_ACCURACY 协议前缀）。
    解析失败 → (0.0, "unknown", "low")。teacher_setup / 复用 STUDENT_ACCURACY 协议的 eval
    走其本地优先级（含 TEACHER_/STUDENT_ACCURACY 前缀），**不**复用本函数（语义不同）。
    """
    for line in stdout.splitlines()[::-1]:
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                for k, kind in (("accuracy", "acc"), ("acc", "acc"),
                                ("nmse", "nmse"), ("mse", "mse"),
                                ("ber", "ber"), ("snr", "snr"), ("snr_db", "snr")):
                    if k in d and isinstance(d[k], (int, float)):
                        return float(d[k]), kind, "high"
            except json.JSONDecodeError:
                pass
    for pat, kind in _ACC_PATTERNS:
        m = pat.search(stdout)
        if m:
            return float(m.group(1)), kind, "high"
    return 0.0, "unknown", "low"


def compute_met_accuracy_absolute(
    student_acc: float, detected_kind: str, baseline: float, kind_override: str,
) -> tuple[bool, str, str]:
    """绝对精度基线对比。返回 (met_accuracy, used_kind, confidence)。

    单一真相源（DRY）：原 ``measure_student._compute_met_accuracy_absolute``。

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
# §4 doc sweep：原消费者 gate_all / train_pool 已删；``append_ledger_row`` 的「主线程持
# orca.lock + 逐行 write+flush」是 crash-safety 契约，留在 kd_common 供未来脚本复用
# （当前活跃消费者：gen_student / distill 间接经 ledger_reducer append）。


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
