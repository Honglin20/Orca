"""orca.skills —— 随包分发的 skill 产物（``teams skill install`` 拷到 CC/opencode）。

子目录每个是一个 skill（如 ``create-workflow/``）。``importlib.resources.files("orca.skills")``
定位打包后的路径，wheel / venv / editable 安装都解析对（非 Python 文件靠 pyproject
``force-include`` 进 wheel）。
"""
