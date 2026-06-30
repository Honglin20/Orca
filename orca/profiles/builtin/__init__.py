"""orca.profiles.builtin —— 内置 CLI profiles。

每个 ``*.py`` 导出一个 ``PROFILE: CliProfile``，registry 扫描本目录自动发现。
新增 backend = 丢一个文件，零 exec/factory/schema/compile 改动（OCP，SPEC §4.1）。
"""
