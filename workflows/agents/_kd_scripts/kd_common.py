"""kd_common.py —— KD-NAS 新三脚本（pick_variant / tune_latency / recorder）共享的确定性 helper。

无 LLM、无网络、不读时钟、不读随机。fail loud。

关键约定（见 docs/plans/2026-07-24-kd-nas-distill-redesign.md）：
  - variant_id = ``.py`` 文件名 stem；done 谓词跨 run 复用的核心。
  - ``variant_sha256`` = 变体 ``.py`` 字节 sha256（文件改了 → 重做，BLK-12）。
  - ``latency_provider_id`` = ``"<path::func>|<sha16>"``（换 latency 脚本 → 重做，HI-12）。
  - ``target_latency_ms`` 浮点归一比较（MED-3）。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

# BLK-1：KNOBS leverage 缩容优先级（高→低）。禁止字母序（"low"<"medium"<"high" 反了）。
RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
VALID_LEVERAGE = set(RANK)
LEVERAGE_DEFAULT = "medium"

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
    """BLK-13：单写者护栏——防并发 run 写同一 kd_artifacts_dir。

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
    换 provider 脚本内容 → 身份变 → done 谓词判重做（HI-12）。
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


def feq(a: Any, b: Any, rel: float = 1e-6) -> bool:
    """浮点近似相等（MED-3：target_latency_ms 字符串 vs float 比较）。非数 → False。"""
    try:
        af, bf = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(af - bf) <= rel * max(1.0, abs(af), abs(bf))


def read_ledger(path: str) -> list[dict[str, Any]]:
    """读 ledger.jsonl。**fail loud**（BLK-16）：任一行非合法 JSON → raise（不 warn-跳过，
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
                # BLK-16：坏行 raise（ledger 是 domain 真相源，corruption → 误 skip/重跑）。
                raise ValueError(
                    f"ledger {path} 第 {lineno} 行非合法 JSON：{e}\n原文：{s[:200]!r}"
                ) from e
            if isinstance(obj, dict):
                out.append(obj)
    return out


def is_variant_done(
    rows_for_v: list[dict[str, Any]],
    cur_target_latency_ms: float,
    cur_provider_id: str,
    variant_sha256: str,
) -> bool:
    """done 谓词（跨 run 复用核心）。

    任一**有效行**（variant_sha256 匹配 BLK-12 + latency_provider_id 匹配 HI-12）满足：
      - status ∈ {SUCCESS, FAIL_accuracy, FAIL_train}：
          * SUCCESS：还须 ckpt 文件存在且非空（BLK-11）且 latency_ms_median ≤ 当前 target
            （MED-4：target 调低到低于该 variant latency → 不再算 done，需重试更小 cfg）。
          * FAIL_accuracy / FAIL_train：训练已发生、结果已记 → done（「记账后下一个」，不重训）。
      - status == FAIL_latency：仅当 row.target_latency_ms ≈ 当前 target（同 target 才算 done；
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
                lat = r.get("latency_ms_median")
                ckpt_ok = bool(ckpt) and os.path.isfile(ckpt) and os.path.getsize(ckpt) > 0
                lat_ok = lat is not None and float(lat) <= float(cur_target_latency_ms)
                if ckpt_ok and lat_ok:
                    return True
                # SUCCESS 但 ckpt 丢了 / latency 超 new target → 不算 done，落空继续找
            else:
                return True
        elif status == "FAIL_latency":
            if feq(r.get("target_latency_ms"), cur_target_latency_ms):
                return True
    return False
