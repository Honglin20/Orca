"""orca.iface.cli.screens —— Textual ModalScreen（gate 弹窗等）。

shell 的「模态交互」层。GateModal 是阻塞式人工确认弹窗（SPEC §4.5）。
"""

from __future__ import annotations

from orca.iface.cli.screens.gate_modal import GateModal

__all__ = ["GateModal"]
