"""tests/exec/test_error_kind_mapping.py —— ErrorKind 枚举与映射表一致性守门测试。

覆盖（ADR §8.1 守门 / phase-11 §6 验收 7/8/9）：
  - ErrorKind 共 11 值（3 transport + 3 protocol + 4 business + unknown）
  - _DEFAULT_KIND_FOR_PHASE 表与 ADR §4.1.1 一致（1:1 phase 映射，stream 默认 PROTOCOL_PARSE）
  - _LEGACY_ERROR_TYPE_TO_KIND 表与 SPEC §4.6 反向映射一致（9 旧值覆盖）
  - from_failed_data 读兼容期：先 kind 后 error_type，error_type 经反向映射回 kind
"""

from __future__ import annotations

import pytest

from orca.exec.error import ExecError
from orca.exec.error_kinds import (
    ErrorKind,
    _DEFAULT_KIND_FOR_PHASE,
    _DEFAULT_RETRYABLE,
    _KIND_LAYER_PREFIX,
    _LEGACY_ERROR_TYPE_TO_KIND,
)


# ── 11 值枚举覆盖 ─────────────────────────────────────────────────────────────


def test_error_kind_has_eleven_values():
    """SPEC §2：ErrorKind 共 11 值（3 transport + 3 protocol + 4 business + unknown）。"""
    assert len(list(ErrorKind)) == 11


def test_error_kind_layer_counts():
    """3 transport / 3 protocol / 4 business / 1 unknown。"""
    transport = [k for k in ErrorKind if k.value.startswith("transport_")]
    protocol = [k for k in ErrorKind if k.value.startswith("protocol_")]
    business = [k for k in ErrorKind if k.value.startswith("business_")]
    unknown = [k for k in ErrorKind if k.value.startswith("unknown")]
    assert len(transport) == 3
    assert len(protocol) == 3
    assert len(business) == 4
    assert len(unknown) == 1


def test_error_kind_str_value_serializable():
    """str mixin：``ErrorKind.X.value`` 是字符串（可 JSON 序列化进 event data["kind"]）。"""
    assert ErrorKind.TRANSPORT_NETWORK.value == "transport_network"
    assert ErrorKind.UNKNOWN.value == "unknown"


# ── ADR §4.1.1 phase → 默认 kind 映射 ────────────────────────────────────────


@pytest.mark.parametrize(
    "phase,expected_kind",
    [
        ("timeout", ErrorKind.TRANSPORT_TIMEOUT),
        ("spawn", ErrorKind.TRANSPORT_PROCESS),
        ("stream", ErrorKind.PROTOCOL_PARSE),        # 默认；classifier 据 raw 精分
        ("result_parse", ErrorKind.PROTOCOL_PARSE),
        ("schema", ErrorKind.PROTOCOL_SCHEMA),
        ("render", ErrorKind.BUSINESS_CONFIG),
        ("config", ErrorKind.BUSINESS_CONFIG),
        ("validator", ErrorKind.PROTOCOL_SCHEMA),    # ADR v2：validator 归 PROTOCOL_SCHEMA
        ("interrupted", ErrorKind.BUSINESS_GATE),
        ("max_iterations", ErrorKind.BUSINESS_CONFIG),
        ("route_deadlock", ErrorKind.BUSINESS_CONFIG),
    ],
)
def test_phase_default_kind_mapping(phase, expected_kind):
    """ADR §4.1.1：ExecError.phase ↔ ErrorKind 映射表（1:1）。"""
    assert _DEFAULT_KIND_FOR_PHASE[phase] is expected_kind


def test_exec_error_phase_default_kind_via_constructor():
    """ExecError(phase=..., message=...) 不传 kind → 按 phase 默认表派生。"""
    assert ExecError(phase="timeout", message="x").kind is ErrorKind.TRANSPORT_TIMEOUT
    assert ExecError(phase="render", message="x").kind is ErrorKind.BUSINESS_CONFIG
    assert ExecError(phase="validator", message="x").kind is ErrorKind.PROTOCOL_SCHEMA


def test_exec_error_kind_override_takes_precedence():
    """显式 kind 覆盖默认（classifier 精分 stream → BUSINESS_AGENT 时用）。"""
    e = ExecError(phase="stream", message="x", kind=ErrorKind.BUSINESS_AGENT)
    assert e.kind is ErrorKind.BUSINESS_AGENT


def test_unknown_phase_defaults_to_unknown_kind():
    """未知 phase → UNKNOWN（容错，不 ValueError，ADR §4.1 决策 1.4）。"""
    assert ExecError(phase="bogus_phase", message="x").kind is ErrorKind.UNKNOWN


# ── SPEC §4.6 旧 error_type → kind 反向映射 ──────────────────────────────────


@pytest.mark.parametrize(
    "legacy_error_type,expected_kind",
    [
        ("ExecTimeout", ErrorKind.TRANSPORT_TIMEOUT),
        ("CliExitNonZero", ErrorKind.TRANSPORT_PROCESS),
        ("ClaudeStreamError", ErrorKind.PROTOCOL_PARSE),
        ("NoResultEvent", ErrorKind.PROTOCOL_PARSE),
        ("SchemaValidationError", ErrorKind.PROTOCOL_SCHEMA),
        ("ConfigError", ErrorKind.BUSINESS_CONFIG),
        ("RenderError", ErrorKind.BUSINESS_CONFIG),
        ("validator_failed", ErrorKind.PROTOCOL_SCHEMA),
        ("Interrupted", ErrorKind.BUSINESS_GATE),
    ],
)
def test_legacy_error_type_to_kind_mapping(legacy_error_type, expected_kind):
    """SPEC §4.6：旧 error_type → ErrorKind 反向映射表（9 旧值覆盖）。"""
    assert _LEGACY_ERROR_TYPE_TO_KIND[legacy_error_type] is expected_kind


def test_from_failed_data_reads_kind_first():
    """读兼容期：data 含 kind → 直接用之（不读 error_type）。"""
    e = ExecError.from_failed_data({
        "kind": "business_agent",
        "error_type": "LegacyValue",
        "message": "x",
        "phase": "stream",
    })
    assert e.kind is ErrorKind.BUSINESS_AGENT


def test_from_failed_data_falls_back_to_error_type_legacy():
    """读兼容期：data 无 kind → 经 _LEGACY_ERROR_TYPE_TO_KIND 反向映射。"""
    e = ExecError.from_failed_data({
        "error_type": "ExecTimeout",
        "message": "x",
        "phase": "timeout",
    })
    assert e.kind is ErrorKind.TRANSPORT_TIMEOUT


def test_from_failed_data_unknown_legacy_error_type():
    """读兼容期：error_type 不在反向映射表 → UNKNOWN（raw 必须保留）。"""
    e = ExecError.from_failed_data({
        "error_type": "UnknownNewType",
        "message": "x",
        "phase": "stream",
    })
    assert e.kind is ErrorKind.UNKNOWN
    assert e.raw is not None


def test_from_failed_data_claude_stream_error_legacy_note():
    """SPEC §4.6：ClaudeStreamError 1:N 不可精确还原，默认 PROTOCOL_PARSE，raw 标注释。"""
    e = ExecError.from_failed_data({
        "error_type": "ClaudeStreamError",
        "message": "x",
        "phase": "stream",
    })
    assert e.kind is ErrorKind.PROTOCOL_PARSE
    assert e.raw is not None
    assert "_legacy_note" in e.raw


def test_from_failed_data_no_kind_no_error_type_unknown():
    """data 既无 kind 也无 error_type → UNKNOWN（raw 保留）。"""
    e = ExecError.from_failed_data({"message": "x", "phase": "node_failed"})
    assert e.kind is ErrorKind.UNKNOWN


# ── _DEFAULT_RETRYABLE 表（SPEC §2.1）─────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,expected_retryable",
    [
        (ErrorKind.TRANSPORT_NETWORK, True),
        (ErrorKind.TRANSPORT_PROCESS, False),
        (ErrorKind.TRANSPORT_TIMEOUT, False),
        (ErrorKind.PROTOCOL_PARSE, False),
        (ErrorKind.PROTOCOL_MCP, False),
        (ErrorKind.PROTOCOL_SCHEMA, False),
        (ErrorKind.BUSINESS_GATE, False),
        (ErrorKind.BUSINESS_AGENT, False),
        (ErrorKind.BUSINESS_CONFIG, False),
        (ErrorKind.BUSINESS_RATE_LIMIT, True),
        (ErrorKind.UNKNOWN, False),
    ],
)
def test_default_retryable_table(kind, expected_retryable):
    """SPEC §2.1：kind 默认 retryable 策略表。"""
    assert _DEFAULT_RETRYABLE[kind] is expected_retryable


# ── _KIND_LAYER_PREFIX 表 ─────────────────────────────────────────────────────


def test_kind_layer_prefix_all_layers_covered():
    """4 layer 全覆盖（transport / protocol / business / unknown）。"""
    assert set(_KIND_LAYER_PREFIX.values()) == {"transport", "protocol", "business", "unknown"}
    assert "transport" in _KIND_LAYER_PREFIX
    assert "protocol" in _KIND_LAYER_PREFIX
    assert "business" in _KIND_LAYER_PREFIX
    assert "unknown" in _KIND_LAYER_PREFIX
