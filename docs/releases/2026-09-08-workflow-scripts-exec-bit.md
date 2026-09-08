# Release Note：workflow 脚本 exec 位治本（git 索引 + 守卫）

- **日期**：2026-09-08
- **Commits**：`c8f4807`（mode 治本）+ `4bc8a2c`（守卫测试）
- **来源**：用户反馈「很多脚本没 chmod +x 导致不可执行」；方案已确认（治本、排除 deprecated）

## 根因

Windows 开发环境（NTFS + `core.filemode=false`）下工作树 `chmod +x` 既不落盘也不进 git → 全部 workflow 脚本以 100644 入库 → **Linux 服务器 clone 后所有脚本不可执行**。直接执行调用点（agent prompt 指示的 `./x.sh`、`$ART/scripts/x.sh`）集体 Permission denied；`bash x.sh` / `python3 x.py` 显式解释器调用不受影响。prof-opt 因 `deploy_scripts.sh:64` 部署时补 chmod 而自愈，其他 workflow 无此兜底。

## 交付

1. **治本（`c8f4807`）**：workflows/ 下（排除 `deprecated/`）带 shebang 的已跟踪 .sh/.py 共 **163 个**，`git update-index --chmod=+x` 翻转为 100755——只改索引零内容 diff，Linux 重新 clone 即全部可执行。无 shebang 的 237 个（库模块/数据）保持 644。
2. **守卫（`4bc8a2c`）**：`tests/test_workflow_scripts_exec.py`——不变量「shebang ⇔ 索引 100755」双向断言 + 守卫活性检查（防扫描集漂移哑绿）。**查 git 索引而非工作树**，Windows 本地/CI 均可跑；新脚本忘设 exec 位会在测试层红。

## 边界与不做

- `deprecated/`（110 个 shebang 脚本）按拍板排除——mode-only diff 对废弃代码是噪音。
- 未做：把「deploy 时对 .sh 补 chmod」推广进 create-workflow 契约（用户未选第 3 层）；prof-opt 既有 `deploy_scripts.sh` 行为保持不变。
- 仓库根 `scripts/`（开发期工具）不在本批范围。

## 验证

- 守卫测试 WSL `.venv` pytest：**2 passed**。
- mode-only 核验：`git diff --stat` 163 files / 0 insertions / 0 deletions；deprecated 零误翻（grep 计数 0）。
- 统计对账：翻转 163 = 全仓 shebang 273 − deprecated 110。
