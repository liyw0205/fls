# FLS 会话交接文档

生成时间：2026-07-03
当前阶段：阶段 3，基础自动化测试

## 本阶段完成进度

完成度：阶段 3 已完成，准备进入阶段 4。

已经完成：

- 新增 `tests/test_auth_backup.py`，使用标准库 `unittest` 和临时 `FLS_BASE_DIR` 隔离真实运行数据。
- 覆盖鉴权核心分支：
  - 未设置 Token 时 `/api/status` 返回 JSON 403。
  - 未设置 Token 时页面请求 `/tasks` 跳转 `/setup`。
  - `X-Token` 命中时 `/api/status` 正常放行。
  - 错误 Query Token 返回 403。
  - 正确 Query Token 写入 session，并跳转到移除 token 后的干净 URL。
- 覆盖备份安全基础分支：
  - `backup_safe_file()` 将路径穿越式文件名收敛到备份目录。
  - `safe_extract_zip()` / `safe_extract_tar()` 接受正常相对路径。
  - `safe_extract_zip()` / `safe_extract_tar()` 拒绝 `../` 路径穿越成员。
- 新增 `tests/test_command_scheduler.py`，覆盖命令与调度核心分支：
  - `normalize_script_type()` 常见别名。
  - `command_list_to_shell()` 参数引用。
  - `.py` 的 `task` 解析和 `build_command()`。
  - 混合命令中 `.py` / `.mjs` 的展开。
  - 5 位和 6 位 Cron 表达式。
  - `virtual_to_real_time()` / `real_to_virtual_time()` 在 offset 下互逆。
- 更新 `DEVELOPMENT.md`，将 `python -B -m unittest discover -s tests` 纳入常规验证清单，并记录阶段 3 开发日志。
- 更新 `docs/DEVELOPMENT_PROGRESS.md`，补充阶段 3 完成项、验证记录、受限验证和后续候选阶段。

已验证：

- `python -B -m unittest tests.test_auth_backup` 通过，10 tests OK。
- `python -B -m unittest tests.test_command_scheduler` 通过，6 tests OK。
- `python -B -m unittest discover -s tests` 通过，16 tests OK。
- `python -B tools/responsive_smoke.py` 通过。
- `git diff --check -- tests/test_auth_backup.py tests/test_command_scheduler.py` 通过。

未完成或受限：

- 当前环境没有 Playwright/Chromium，仍未做真实浏览器截图检查。
- Python 3.14 下 `tarfile.extractall()` 会输出 DeprecationWarning；当前测试通过，后续建议显式处理 `filter` 参数并补充链接/特殊文件成员安全测试。
- 本阶段尚未覆盖真实任务子进程、停止、超时、失败重试、通知发送、日志清理和代理环境注入。
- 工作区仍存在本阶段外的既有未提交业务修改，后续提交必须继续只纳入当前阶段相关文件。

## 子代理协作情况

- 子代理 A：实现命令/调度测试，只新增 `tests/test_command_scheduler.py`。
- 子代理 B：只读审查鉴权/备份测试风险，提示测试前设置 `FLS_BASE_DIR`、patch 使用方模块、清理 scheduler 和全局状态。
- 主代理：实现鉴权/备份测试，集成验证，更新开发文档、进度文档和交接文档。

子代理结论摘要：

- 测试必须在导入 `fls_manager.*` 前设置临时 `FLS_BASE_DIR`，避免读写真实 `data/`、`scripts/`、`log/`。
- route 测试如果需要 mock，应 patch 使用方模块，而不是源定义模块。
- 涉及 `BACKUP_JOBS`、登录失败状态、scheduler 等全局状态的测试要主动清理。
- 复杂安全边界如 symlink、hardlink、device member、zip bomb、并发任务可放到后续阶段。

## 下阶段实现目标

阶段 4 建议目标：数据 schema 文档与读取时迁移函数。

具体任务：

1. 梳理并文档化核心 JSON 数据结构：
   - `data/tasks.json`
   - `data/config.json`
   - `data/global_env.json`
   - `data/proxies.json`
   - `data/collections.json`
2. 给 `models.py`、`config.py` 或对应功能域增加读取时归一化/迁移函数，优先处理旧字段、缺省字段和类型不一致。
3. 为迁移函数补充标准库 `unittest`，继续使用临时 `FLS_BASE_DIR`。
4. 评估是否抽取分页组件、消息结果卡、摘要网格；如果与 schema 阶段冲突，优先保证数据兼容测试。
5. 有浏览器环境时，按 390px、768px、1024px、1440px 宽度检查 `/`、`/tasks`、`/task/new`、`/logs`、`/pull`、`/online-scripts`、`/config`、`/panel/status`。
6. 阶段结束时继续更新 `DEVELOPMENT.md`、`docs/DEVELOPMENT_PROGRESS.md`、`docs/SESSION_HANDOFF.md`，并提交新的阶段 commit。

## 下一会话启动提示

请从 `docs/SESSION_HANDOFF.md` 开始，继续阶段 4。先读取 `DEVELOPMENT.md`、`docs/DEVELOPMENT_PROGRESS.md` 和当前 `git status`，不要还原本阶段外的既有修改。优先做数据 schema 文档、读取时兼容迁移和对应单元测试。
