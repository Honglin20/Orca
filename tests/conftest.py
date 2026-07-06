"""tests/conftest.py —— 顶层共享 fixtures。

phase-11-process §1.2（ADR §4.7）：``process_local`` fixture 给每测试注入独立的
``ProcessRegistry`` 实例（避免测试间共享 default singleton 状态污染 / xdist 并行问题）。

用法（任何测试想隔离进程注册表状态时）::

    def test_x(process_local):
        # process_local 是空 registry，default singleton 不被改
        runner = CLIRunner(cfg, registry=process_local, ...)
        ...

若测试不注入 ``process_local``，``CLIRunner`` 默认用 ``get_default_registry()``——
对不关心 registry 状态的现有测试保持向后兼容（spawn 真实 proc 的测试 release 后状态干净）。
"""

from __future__ import annotations

import pytest

from orca.exec.registry import ProcessRegistry


@pytest.fixture
def process_local() -> ProcessRegistry:
    """每测试独立 ``ProcessRegistry`` 实例（DI 可测试性，ADR §4.7 闭环 B8）。

    与 ``get_default_registry()`` 模块级 singleton 隔离：测试 acquire/release/kill
    的副作用不污染其他测试；并行 pytest (xdist) 各 worker 进程也有自己的 default singleton。
    """
    return ProcessRegistry()
