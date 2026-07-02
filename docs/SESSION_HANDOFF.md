# FLS 会话交接文档

生成时间：2026-07-03
当前阶段：阶段 9，备份 tar 解压兼容与安全边界

## 本阶段完成进度

完成度：阶段 9 已完成，准备进入阶段 10。

已经完成：

- 更新 `fls_manager/routes/backup/_common.py`：
  - 新增 `_archive_member_target()`，统一 tar/zip 成员路径校验。
  - 拒绝 `/absolute`、`C:/drive`、`..\\backslash` 等跨平台绝对或穿越路径。
  - `safe_extract_tar()` 解压前继续校验成员路径 containment。
  - `safe_extract_tar()` 拒绝 symlink、hardlink。
  - `safe_extract_tar()` 只允许普通文件和目录，拒绝 FIFO、字符设备、块设备等特殊成员。
  - `safe_extract_tar()` 优先调用 `tar.extractall(path, filter="data")`。
  - 对不支持 `filter` 参数的旧 Python fallback 到已完成手动校验后的 `extractall(path)`。
- 扩展 `tests/test_auth_backup.py`：
  - 新增 `add_tar_member()` helper，用标准库构造 tar 特殊成员。
  - 覆盖 zip 绝对路径、Windows drive path、反斜杠穿越路径拒绝。
  - 覆盖 tar 正常文件解压且不产生 `DeprecationWarning`。
  - 覆盖 tar `../` 路径穿越继续拒绝。
  - 覆盖 tar 绝对路径和 Windows drive path 拒绝。
  - 覆盖 tar symlink、hardlink 拒绝。
  - 覆盖 tar FIFO、字符设备、块设备拒绝。
- 更新 `DEVELOPMENT.md`：
  - 将备份安全解压 filter 兼容和特殊成员测试纳入“已覆盖”。
  - 从后续优先项移除 tar 解压告警处理。
  - 增加阶段 9 开发日志。
- 更新 `docs/DEVELOPMENT_PROGRESS.md`：
  - 新增阶段 9 完成块、验证记录、受限验证和安全策略结论。

已验证：

- `python -B -m unittest tests.test_auth_backup` 通过，15 tests OK。
- `python -B -m unittest discover -s tests` 通过，78 tests OK。
- `python -B tools/responsive_smoke.py` 通过。
- `python -B -m compileall fls-manager.py fls_manager tests` 通过。

未完成或受限：

- 当前环境没有 Playwright/Chromium，仍未做真实浏览器截图检查。
- 本阶段没有跑真实备份恢复 UI 流程，只覆盖安全解压核心函数。
- 工作区仍存在本阶段外的既有未提交业务修改，后续提交必须继续只纳入当前阶段相关文件。

## 子代理协作情况

- 子代理 A：只读审查 `safe_extract_tar()`、`safe_extract_zip()`、备份路径函数和现有备份测试，输出 Python 3.14 `filter` 参数兼容方案和最小实现范围。
- 子代理 B：只读审查 tar 特殊成员测试构造方式，输出 symlink、hardlink、FIFO、设备文件和 DeprecationWarning 捕获的 unittest 写法。
- 主代理：实现安全解压加固、补充测试、运行验证、更新开发文档和交接文档。

子代理结论摘要：

- `safe_extract_tar()` 不能只依赖 `member.name` containment，symlink/hardlink/special file 仍可能有风险。
- Python 3.14 默认 filter 行为变化不应作为隐式依赖，应显式传 `filter="data"`。
- 老 Python fallback 前必须手动拒绝特殊 tar 成员。
- tar 特殊成员可以通过 `tarfile.TarInfo` 的 `type`、`linkname` 用标准库构造，不需要真实设备文件。
- ZIP 路径校验应覆盖 Windows drive path 和反斜杠穿越，避免跨平台行为差异。

## 下阶段实现目标

阶段 10 建议目标：低风险 UI 组件抽取。

具体任务：

1. 先只读审查当前页面重复结构：
   - 分页组件候选：脚本列表、日志列表、在线脚本列表、任务列表等。
   - 消息结果卡候选：备份/依赖/运行时/在线脚本操作结果。
   - 摘要网格候选：仪表盘、状态页、配置概览。
2. 选择一个低风险切片落地，建议优先分页组件：
   - 在 `fls_manager/ui/components.py` 或独立 `fls_manager/ui/pagination.py` 增加纯 HTML helper。
   - 只替换 1-2 个页面，保持已有路由参数和样式类不变。
   - 不引入 npm、前端构建链或大型 UI 依赖。
3. 补充验证：
   - `python -B tools/responsive_smoke.py`。
   - 相关页面 Flask test client 渲染无 500。
   - `python -B -m unittest discover -s tests`。
4. 阶段 10 结束时继续更新 `DEVELOPMENT.md`、`docs/DEVELOPMENT_PROGRESS.md`、`docs/SESSION_HANDOFF.md`，并提交新的阶段 commit。

## 后续候选

- 有浏览器环境时，按 390px、768px、1024px、1440px 宽度检查 `/`、`/tasks`、`/task/new`、`/logs`、`/pull`、`/online-scripts`、`/config`、`/panel/status`。
- 继续拆分过长路由中的业务逻辑到 helper/service。
- 评估配置、任务、代理、通知 JSON 写入备份或回滚机制。

## 下一会话启动提示

请从 `docs/SESSION_HANDOFF.md` 开始，继续阶段 10。先读取 `DEVELOPMENT.md`、`docs/DEVELOPMENT_PROGRESS.md`、`fls_manager/ui/components.py`、相关列表页路由和当前 `git status`，不要还原本阶段外的既有修改。优先做低风险 UI 组件抽取，保持 Flask + 原生 CSS/JS 和无 npm 构建链。
