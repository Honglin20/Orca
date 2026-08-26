"""test_tars_skill.py — mechanical pin of the tars skill's verbatim driver
protocol.

The skill is a product md with no unit-test surface of its own, but its
driver-protocol clauses are load-bearing for every in-session run: the output
chain is a two-hop LLM chain (subagent -> driver -> `orca next --output`), and
any paraphrase at the driver hop pollutes schema-bound outputs (narrative
prefixed to the JSON, fields lost in abridgement). The round-3 prof-opt E2E
proved the unconstrained hop loses the node prompt's verbatim-output
discipline (first-dispatch outputs bounced recoverable as prose+JSON). These
greps keep the three hard clauses from silently regressing out of the skill.
"""
from __future__ import annotations

from pathlib import Path

_TARS_SKILL = (Path(__file__).resolve().parents[1] / "orca" / "skills" / "tars"
               / "SKILL.md")


def test_skill_pins_verbatim_driver_protocol():
    text = _TARS_SKILL.read_text(encoding="utf-8")
    # the rule set exists and is named
    assert "逐字传递铁律" in text
    # clause 1 — dispatch forwards the node prompt verbatim, no paraphrase
    assert "不许转述、摘编、改写" in text
    # clause 2 — the final message is carried to --output byte-for-byte
    assert "逐字节搬运、一字不改" in text
    # clause 3 — JSON-mandating nodes return the single-line JSON verbatim
    assert "单行 JSON 原文" in text
    # the driving loop re-anchors the rule at both hops (dispatch + output)
    assert text.count("【逐字传递铁律】") >= 2
    # the success checklist carries the same contract (zero附加 before/after)
    assert "前后零附加" in text
