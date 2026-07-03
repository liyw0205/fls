# FLS 开发进度

更新时间：2026-07-04

本文记录阶段开发进度。每个阶段结束前必须更新本文件，并生成或更新 `docs/SESSION_HANDOFF.md`。

## 阶段 1：开发制度与响应式基础

状态：已完成

目标：

- 建立后续开发文档、阶段进度文档和会话交接文档。
- 让前端明确支持手机、平板、电脑三类设备。
- 保持项目开箱即用，暂不引入 npm 构建链。

已完成：

- 新增 `DEVELOPMENT.md`，记录项目架构、数据模型、任务执行链路、前端约定、验证清单和后续方向。
- 在 `DEVELOPMENT.md` 增加产品参考对象：青龙面板、呆呆面板、白虎面板。
- 在 `DEVELOPMENT.md` 增加阶段会话要求：主代理加子代理协作、阶段结束更新进度/交接文档、提交阶段 commit。
- 新增 `.gitignore`，忽略 Python 缓存、虚拟环境和本地运行数据。
- 更新 `fls_manager/static/fls.js`，增加 `fls-phone`、`fls-tablet`、`fls-desktop` 设备分类，保留 `fls-mobile` 兼容行为。
- 更新 `fls_manager/static/fls.css`，补充手机、小平板、横屏平板/中宽桌面的布局规则。
- 根据子代理审查修正 `fls-mobile` 判断：宽度大于 900px 的横屏平板不再因为 UA 被强制进入抽屉移动布局。
- 更新 `fls_manager/ui/layout.py`，提升 CSS/JS 静态资源版本到 `20260703-1`。

验证记录：

- `node --check fls_manager/static/fls.js`：通过。
- `python -B` AST 解析 `fls_manager/ui/layout.py`：通过。
- Flask test client 渲染 `/tasks`、`/config`、`/panel/status`、`/logs`：均无 500。
- 临时预览服务验证新静态资源版本：通过。

受限验证：

- 当前环境没有 Playwright/Chromium，未能做真实浏览器截图检查。
- 需要后续在真实手机、平板或带浏览器自动化环境中复测 390px、768px、1024px、1440px 宽度。

开源复用结论：

- 本阶段不引入 Bootstrap、Tailwind、Flowbite、htmx 等新依赖。
- 原因：当前项目无 `package.json` 和构建链，面向 Linux/Windows/Termux 开箱即用；直接引入构建式方案会增加部署复杂度。
- 后续如果页面组件重复明显增加，可优先评估“无构建 CDN 可用”的方案，或先抽取 `fls_manager/ui/` 组件。
- 青龙面板、呆呆面板、白虎面板作为产品和交互参考，不作为本阶段代码依赖。

子代理审查结论：

- 响应式风险审查指出原实现会把 1024px iPad/Android 平板按 UA 强制归为 `fls-mobile`，与横屏平板压缩桌面布局目标冲突；本阶段已修复。
- 仍需人工重点检查 667px、720px、768px 表单页，以及 1024px `/tasks`、`/pull`、`/proxy`、`/config` 表格换行情况。

## 阶段 2：响应式验收、同类面板对标与组件抽取准备

状态：已完成

目标：

- 弥补缺少浏览器截图环境时的页面结构验证能力。
- 对标青龙面板、呆呆面板、白虎面板，形成 FLS 自身的功能需求池。
- 找出后续可低风险抽取到 `fls_manager/ui/` 的重复组件。

已完成：

- 新增 `docs/PANEL_REFERENCE.md`，记录同类面板参考边界、可借鉴项和候选需求池。
- 核验青龙面板、呆呆面板公开仓库链接；白虎面板主仓库来源仍需后续继续核验。
- 新增 `tools/responsive_smoke.py`，在没有真实浏览器环境时检查核心页面结构、静态资源版本和响应式关键 token。
- 新增 `fls_manager/ui/components.py`，提供低风险纯 HTML 组件 `page_header_card()` 和 `table_card()`。
- 在全局变量、通知、代理、脚本管理页面接入上述组件，保留原有业务行渲染和 JS 行为。
- 子代理 A 完成组件抽取候选审查，确认阶段 2 优先抽取 header、pagination、table、message 等纯 HTML 外壳。
- 子代理 B 完成 `tools/responsive_smoke.py` 并通过验证。

验证记录：

- `python -B tools/responsive_smoke.py`：通过。
- Python AST 检查 `components.py`、4 个接入页面和烟测工具：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查留到后续有浏览器环境时执行。

组件抽取结论：

- 已落地：`page_header_card()`、`table_card()`。
- 后续优先：分页组件、消息结果卡、摘要网格。
- 暂缓：批量工具栏、实时日志页、复杂配置表单和鉴权页面。

## 阶段 3：基础自动化测试

状态：已完成

目标：

- 新增轻量自动化测试目录，不引入 pytest 或 npm/浏览器依赖。
- 优先覆盖命令解析、Cron 虚拟时间、页面/API 鉴权和备份安全解压。
- 将单元测试和响应式 smoke 固化为阶段验证入口。

已完成：

- 新增 `tests/test_auth_backup.py`，使用标准库 `unittest` 和临时 `FLS_BASE_DIR` 隔离真实数据。
- 覆盖未设置 Token 时 API 返回 JSON 403、页面跳转 `/setup`。
- 覆盖 `X-Token` API 放行、错误 Query Token 拒绝、正确 Query Token 写入 session 并重定向到清理后的 URL。
- 覆盖 `backup_safe_file()` 文件名收敛到备份目录。
- 覆盖 `safe_extract_zip()` / `safe_extract_tar()` 正常路径解压和 `../` 路径穿越拒绝。
- 新增 `tests/test_command_scheduler.py`，覆盖脚本类型归一、命令参数引用、`.py` 任务命令构建、混合命令展开。
- 覆盖 5 位和 6 位 Cron 解析，以及虚拟时间与真实时间在 offset 下互逆。
- 子代理 A 负责命令/调度测试补充；子代理 B 负责鉴权/备份测试风险审查；主代理负责鉴权/备份测试、集成验证、文档和提交。

验证记录：

- `python -B -m unittest tests.test_auth_backup`：通过，10 tests OK。
- `python -B -m unittest tests.test_command_scheduler`：通过，6 tests OK。
- `python -B -m unittest discover -s tests`：通过，16 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `git diff --check -- tests/test_auth_backup.py tests/test_command_scheduler.py`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- `tarfile.extractall()` 在 Python 3.14 输出 DeprecationWarning，当前测试通过；后续可以显式使用 `filter` 参数并补充链接/特殊文件成员测试。
- 本阶段只覆盖核心纯函数和轻量 Flask test client 分支，尚未覆盖真实任务子进程、失败重试、通知发送和日志轮转。

测试策略结论：

- 阶段 3 不引入 pytest，保持标准库可运行，符合 Termux/Windows/Linux 开箱即用目标。
- 测试导入 `fls_manager.*` 前必须先设置临时 `FLS_BASE_DIR`，避免读取真实 `data/`、`scripts/`、`log/`。
- 涉及 app 创建和 scheduler 的测试结束后要关闭 scheduler，并清理 `sys.modules` 中的 `fls_manager.*`。

## 阶段 4：数据 schema 文档与读取迁移

状态：已完成

目标：

- 文档化核心 JSON 数据结构和读取迁移规则。
- 给 `models.py`、`config.py` 增加最小读取时归一化函数。
- 为旧字段、缺省字段和坏类型补充标准库单元测试。

已完成：

- 新增 `docs/DATA_SCHEMA.md`，记录 `tasks.json`、`config.json`、`global_env.json`、`proxies.json`、`collections.json` 的规范字段和读取迁移规则。
- 在 `fls_manager/models.py` 新增任务、全局变量、代理、合集归一化函数。
- `load_tasks()` / `save_tasks()` 现在会统一清洗任务结构，迁移旧 `notify_ids` 和旧 `retry_count`，归一化 `notify`、`random_delay`、`retry`、`run_count`、`enabled`、`pinned` 和任务环境变量。
- `load_global_env()` / `save_global_env()` 会清洗空键并把值转成字符串。
- `load_proxies()` / `save_proxies()` 会清洗代理类型、布尔状态、缺失 ID 和文本字段。
- `load_collections()` / `save_collections()` 保留原有缺失 ID 丢弃策略，并把文本字段统一归一。
- 在 `fls_manager/config.py` 新增 `normalize_config_data()`，集中处理默认配置合并、布尔转换、数值钳制、在线脚本源兜底和 `task_types` 过滤。
- 新增 `tests/test_schema_migration.py`，覆盖任务旧字段迁移、全局变量清洗、代理归一、合集归一和配置钳制。
- 根据子代理审查将面板时区偏移范围收紧为 `-23..23`，并同步修复时间同步页选项和 helper。
- 子代理 A 尝试做 schema 只读审查，但因 429 限流失败；子代理 B 负责本阶段 diff 只读风险审查。

验证记录：

- `python -B -m unittest tests.test_schema_migration`：通过，5 tests OK。
- `python -B -m unittest discover -s tests`：通过，21 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只覆盖核心 JSON 读取迁移，尚未覆盖在线脚本缓存、运行时安装状态、备份 job 状态等非核心临时数据。
- `load_tasks()` 对缺失 ID 的任务会生成新 ID 并写回；如果用户手工维护了外部引用，需要以写回后的 ID 为准。
- 缺失 `notify` 且没有旧 `notify_ids` 的任务按旧运行时行为迁移为 `none`，不会自动开启默认通知。

迁移策略结论：

- 不引入 Pydantic、Marshmallow 或数据库迁移工具，继续保持标准库优先。
- 顶层未知配置字段暂时保留，避免破坏后续扩展或用户自定义字段。
- 对运行时有副作用的迁移只在核心模型读取入口做，路由层不直接读写原始 JSON。

## 阶段 5：任务运行链路测试

状态：已完成

目标：

- 为任务运行链路补充可控单元测试，不真实执行用户脚本。
- 覆盖任务运行状态、停止、失败重试、日志收尾、代理环境注入和通知 mock。
- 继续使用标准库 `unittest`，保持 Termux/Windows/Linux 开箱即用。

已完成：

- 新增 `tests/test_task_runtime.py`，使用临时 `FLS_BASE_DIR` 和 `sys.modules` 清理隔离真实数据。
- 覆盖 `increase_run_count()` 对 `run_count`、`last_run_at`、`updated_at` 的持久化更新。
- 覆盖 `task_random_delay_seconds()` 的 none/default/custom/坏类型分支，并 mock `random.randint()`。
- 覆盖 `task_retry_config()` 的坏类型、负数、超上限和正常字符串值。
- 覆盖 `run_task_now()` 的任务不存在、已运行、命令解析失败和正常提交启动状态。
- 覆盖 `stop_task_now()` 的运行状态清理、手动停止标记和日志追加。
- 覆盖 `_start_task_worker()` 的环境合并顺序：系统环境、全局变量、任务变量、代理变量和 `FLS_TASK_*`。
- 覆盖 `task_finish_watcher()` 的不通知、发送通知和失败后进入重试分支，通知发送全程 mock。
- 覆盖 `proxy.py` 的代理 URL 构造、requests 代理字典、SOCKS 环境注入、GitHub 代理不注入任务环境、禁用代理过滤。
- 覆盖 `notify.py` 的新旧任务通知字段、默认通知去重、`__none__` 跳过发送和 `send_by_ids()` mock 发送。
- 子代理完成任务运行/代理/通知测试点只读审查，主代理据此补充 watcher 重试和通知分支。

验证记录：

- `python -B -m unittest tests.test_task_runtime`：通过，12 tests OK。
- `python -B -m unittest discover -s tests`：通过，33 tests OK。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段不真实执行用户脚本，不真实调用 `subprocess.Popen()`，不发送网络通知。
- `task_finish_watcher()` 的超时强杀分支、`_start_task_attempt()` 的 Popen 参数、`send_one()` 各渠道网络出口仍待后续 mock 覆盖。

测试策略结论：

- 任务运行链路测试应优先 mock `threading.Thread`、`subprocess.Popen`、`requests`、`smtplib` 和通知/调度器出口。
- 不要让测试线程真实启动 worker，也不要给 `force_kill_process()` 传真实进程。
- 代理与通知测试优先覆盖纯转换和选择逻辑；网络质量检测与真实通知发送只做 mock 测试。

## 阶段 6：任务运行链路深测与日志/通知出口 mock

状态：已完成

目标：

- 继续深化任务运行链路测试，覆盖 Popen 参数、watcher 超时、日志清理和通知出口。
- 保持所有测试不真实启动用户脚本、不真实发网络请求、不真实连接 SMTP。
- 补充 GitHub 代理可用性缓存测试。

已完成：

- 扩展 `tests/test_task_runtime.py`，新增 `_start_task_attempt()` 测试。
- 覆盖 `subprocess.Popen()` 的 `cmd`、`shell`、`cwd`、`stdout`、`stderr`、`env`，POSIX 下额外断言 `preexec_fn is os.setsid`。
- 覆盖 `_start_task_attempt()` 对 `RUNNING` 的 `process`、`pid`、`status`、`attempt`、`total_attempts` 更新。
- 覆盖 `increase_run_count()` 调用和 watcher 线程提交，但不真实启动 watcher。
- 覆盖 `task_finish_watcher()` 超时分支：`TimeoutExpired`、`force_kill_process()`、不重试、不发送通知、清理 `RUNNING` 和日志内容。
- 覆盖 `logs.cleanup_logs()`：超大日志删除、同一任务按日志头分组保留最近 N 个、不同任务互不影响。
- 覆盖 `notify.send_one()` 的 webhook、Bark、SMTP SSL 分支，全部 mock 网络/SMTP 出口。
- 覆盖 `proxy.github_proxy_available()`：首次检测、TTL 内缓存命中、过期重新检测、失败缓存、非 GitHub 代理跳过、`use_cache=False` 绕过缓存。
- 子代理完成阶段 6 只读风险审查，提示 `_start_task_attempt()` 参数位置、GitHub 代理缓存 key 和 TTL 边界等细节；主代理已补对应断言。

验证记录：

- `python -B -m unittest tests.test_task_runtime`：通过，20 tests OK。
- `python -B -m unittest discover -s tests`：通过，41 tests OK。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 仍未覆盖全部通知渠道，例如 Telegram、Server 酱、PushPlus、企业微信、钉钉、飞书、Ntfy、WxPusher、Gotify、PushDeer。
- 仍未覆盖 GitHub URL 改写、Git 临时配置参数、代理质量检测并发聚合。
- `tarfile.extractall()` 在 Python 3.14 输出 DeprecationWarning，后续安全测试阶段仍需处理。

测试策略结论：

- `_start_task_attempt()` 适合用假 `Popen` 和假 `Thread` 覆盖提交行为，不适合跑真实子进程。
- `send_one()` 分支测试必须 patch `requests` 或 `smtplib` 出口，并优先覆盖请求构造。
- 日志清理测试必须固定 mtime，不依赖文件创建顺序。

## 阶段 7：storage、通知渠道、代理质量检测和日志边界补测

状态：已完成

目标：

- 为 `storage.py` 补充异常读写和临时文件替换边界。
- 为更多 `notify.send_one()` 渠道补无网络 mock 测试。
- 覆盖 GitHub URL 改写、Git 临时配置参数、代理质量检测 URL 解析和普通代理并发聚合。
- 补充 `logs.tail_file()` 和 `parse_task_name_from_log()` 边界测试。

已完成：

- 新增 `tests/test_storage_notify_proxy.py`，使用标准库 `unittest`、临时 `FLS_BASE_DIR` 和模块清理隔离真实数据。
- 覆盖 `storage.read_json()`：文件不存在、JSON 损坏返回传入默认值。
- 覆盖 `storage.write_json()`：自动创建父目录、临时文件替换后内容完整、替换失败时旧文件保持不变且异常向外抛出。
- 覆盖 `notify.send_one()` 的 Server 酱、PushPlus、Telegram、企业微信、钉钉、飞书、Ntfy、Gotify、PushDeer 分支，全部 mock `requests.post()`。
- 覆盖钉钉和飞书签名分支，固定 `time.time()` 保持断言稳定。
- 覆盖 `proxy.github_proxy_url_from_proxy()`：非 GitHub URL、非 github 类型、`verify=False` 和健康检查失败回退。
- 覆盖 `proxy.github_git_config_args_from_proxy()`：非 github 类型、`verify=False` 生成 insteadOf 参数、健康检查失败返回空列表。
- 覆盖 `proxy.parse_quality_urls()`：空值默认、英文/中文逗号、空白分隔、自动补 `https://`、去重且保序。
- 覆盖 `proxy.quality_proxy_object()` 普通代理并发聚合：所有请求均 mock，单个 URL 异常不影响其他结果，最终结果按输入 URL 顺序返回。
- 覆盖 `logs.tail_file()`：缺失文件、尾部行读取、坏 UTF-8 字节 replace。
- 覆盖 `logs.parse_task_name_from_log()`：有启动头、无启动头、缺失文件。
- 子代理完成阶段 7 只读审查，提示通知出口 mock、代理并发结果顺序、GitHub 代理全局缓存、日志 mtime 和 tail 断言等风险点；主代理据此补充断言。

验证记录：

- `python -B -m unittest tests.test_storage_notify_proxy`：通过，16 tests OK。
- `python -B -m unittest discover -s tests`：通过，57 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段不真实发网络请求，不真实执行 git，不真实连接 SMTP。
- `tarfile.extractall()` 在 Python 3.14 输出 DeprecationWarning，后续安全测试阶段仍需处理。

测试策略结论：

- 通知渠道测试优先断请求构造和 `ok` 结果，避免过度依赖 `str(dict)` 的完整字符串。
- 代理质量检测测试必须 mock `requests.get()`，并只断最终结果按输入 URL 保序。
- storage 替换失败边界当前会保留 `.tmp` 文件，符合现有实现；后续如增加清理逻辑需同步调整测试。

## 下一阶段候选

## 阶段 8：通知配置、GitHub 代理质量检测和日志清理边界补测

状态：已完成

目标：

- 继续补齐 `notify.py` 配置工具、内容分片、WxPusher 和多分片发送顺序测试。
- 覆盖 `proxy.py` GitHub 质量检测 concat/Git insteadOf 细分分支和 Git 命令代理 helper。
- 覆盖 `logs.py` 最新日志选择和日志清理更多边界。

已完成：

- 扩展 `tests/test_storage_notify_proxy.py`，该文件从 16 个测试增加到 32 个测试。
- 覆盖 `notify.notify_items()`：
  - 移除非 dict 和未知 channel。
  - 为缺失字段补 `id`、`enabled`、`config`、`name`。
  - 清理结果持久化写回配置。
- 覆盖 `notify.default_notify_ids()` 和 `save_default_notify_ids()`：
  - 过滤禁用项和不存在项。
  - 去重并保持输入顺序。
  - 非 list 配置返回空列表。
- 覆盖 `notify.split_content()`：
  - 空内容、`None`、刚好 limit、无分隔符超长、有分隔符裁切和首尾空白清理。
- 覆盖 `notify.send_one()` 的 WxPusher 分支：
  - Topic ID 转整数。
  - UID 列表解析。
  - HTML 内容转义。
  - 无 topic/uid 时不发请求并返回失败。
- 覆盖 `notify.send_by_ids()` 多 chunk、多通知 item 顺序：
  - 外层按 chunk，内层按通知 item。
  - 标题追加 `[1/2]`、`[2/2]`。
  - 返回结果顺序与发送顺序一致。
- 覆盖 `proxy.quality_github_proxy_object()`：
  - GitHub 代理地址为空。
  - concat 成功但未安装 git。
  - concat 请求异常。
  - Git insteadOf 成功。
  - Git insteadOf 返回失败码。
  - Git insteadOf 超时异常。
- 覆盖 `proxy.build_git_command_with_github_proxy()` 和 `github_git_proxy_used()`：
  - 启用 GitHub 代理插入 `-c url...insteadOf`。
  - HTTP、禁用、缺失代理不插入 GitHub 临时配置。
- 覆盖 `logs.latest_log_for_task()`：
  - 按 mtime 返回匹配任务的最新日志。
  - 无匹配时返回空字符串。
- 覆盖 `logs.cleanup_logs()`：
  - `log_keep_per_task=0` 时当前实现会删除该任务全部日志。
  - 非法数值配置当前实现会抛 `ValueError`。
  - 无启动头日志归入“其他日志”分组并按保留数清理。
  - `unlink()` 异常会被吞掉，不中断清理流程。
- 子代理 A 完成通知侧只读审查；子代理 B 完成代理/日志侧只读审查。主代理根据审查结论补测试并修正文档里的配置异常预期。

验证记录：

- `python -B -m unittest tests.test_storage_notify_proxy`：通过，32 tests OK。
- `python -B -m unittest discover -s tests`：通过，73 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段不真实发网络请求，不真实执行 git，不真实连接 SMTP。
- `tarfile.extractall()` 在 Python 3.14 输出 DeprecationWarning，后续安全测试阶段仍需处理。

测试策略结论：

- `notify_items()` 会原地补齐 item 字段，测试不要复用原始 dict 做后续期望。
- `quality_github_proxy_object()` concat 成功条件是 `HTTP 200` 且正文长度大于 10。
- `cleanup_logs()` 当前没有保护非法配置的 `int()` 转换，测试按当前行为断言抛出。

## 阶段 9：备份 tar 解压兼容与安全边界

状态：已完成

目标：

- 处理 `safe_extract_tar()` 在 Python 3.14 下的 `tarfile.extractall()` DeprecationWarning。
- 显式控制 tar 解压 filter 行为，保持旧 Python 兼容。
- 补充 tar 特殊成员和 zip 跨平台路径边界测试。

已完成：

- 更新 `fls_manager/routes/backup/_common.py`：
  - 新增 `_archive_member_target()`，统一校验 tar/zip 成员路径。
  - 拒绝 `/absolute`、`C:/drive`、`..\\backslash` 等跨平台绝对或穿越路径。
  - `safe_extract_tar()` 解压前继续校验成员路径 containment。
  - `safe_extract_tar()` 拒绝 symlink、hardlink。
  - `safe_extract_tar()` 只允许普通文件和目录，拒绝 FIFO、字符设备、块设备等特殊成员。
  - `safe_extract_tar()` 优先调用 `tar.extractall(path, filter="data")`。
  - 对不支持 `filter` 参数的旧 Python fallback 到已完成手动校验后的 `extractall(path)`。
- 扩展 `tests/test_auth_backup.py`：
  - 新增 tar 特殊成员 helper。
  - 覆盖 zip 绝对路径、Windows drive path、反斜杠穿越路径拒绝。
  - 覆盖 tar 正常文件解压且不产生 `DeprecationWarning`。
  - 覆盖 tar `../` 路径穿越继续拒绝。
  - 覆盖 tar 绝对路径和 Windows drive path 拒绝。
  - 覆盖 tar symlink、hardlink 拒绝。
  - 覆盖 tar FIFO、字符设备、块设备拒绝。
- 子代理 A 完成备份安全实现审查；子代理 B 完成 tar 特殊成员测试构造审查。

验证记录：

- `python -B -m unittest tests.test_auth_backup`：通过，15 tests OK。
- `python -B -m unittest discover -s tests`：通过，78 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有跑真实备份恢复 UI 流程，只覆盖安全解压核心函数。

安全策略结论：

- tar 成员类型采用白名单：只允许普通文件和目录。
- 路径校验在调用 `extractall()` 前完成，避免旧 Python fallback 重新暴露特殊成员风险。
- zip 解压仍使用 `zipfile.extractall()`，但先做统一路径校验，补齐跨平台路径边界。

## 下一阶段候选

## 阶段 10：低风险分页组件抽取

状态：已完成

目标：

- 在不引入 npm/构建链的前提下抽取一个纯 HTML 分页组件。
- 保留现有响应式结构和按钮样式。
- 避开当前工作区已有未提交改动的任务、日志相关文件。

已完成：

- 在 `fls_manager/ui/components.py` 新增 `pagination_card()`：
  - 保留 `.card`、`.help`、`.action-row`、`.btn` 结构。
  - 支持链接分页 `href_for`。
  - 支持按钮分页 `onclick_for`。
  - 支持禁用上一页/下一页、当前页高亮、首页/尾页和省略号。
  - 对 URL、onclick、按钮文案和 label 做 HTML escape。
- 替换 `fls_manager/routes/online_scripts/_common.py` 两处分页：
  - `online_scripts_page_links()` 使用链接分页。
  - `install_task_page_links()` 使用按钮分页，并保留 `flsInstallGoTaskPage(...)` 行为。
- 新增 `tests/test_ui_components.py`：
  - 覆盖单页返回空。
  - 覆盖链接分页、禁用态、省略号、active 样式和 href 转义。
  - 覆盖按钮分页和自定义 label。
- 子代理 A 完成分页候选审查，建议避开当前脏的 logs/tasks 页面，本阶段已只替换在线脚本相关分页。
- 子代理 B 完成响应式结构审查，确认组件需保留 `.card`、`.help`、`.action-row`、`.btn`，本阶段已按此约束实现。

验证记录：

- `python -B -m unittest tests.test_ui_components`：通过，4 tests OK。
- `python -B -m unittest discover -s tests`：通过，82 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段未替换任务/日志分页，因为相关文件存在阶段外未提交业务修改。

组件策略结论：

- 分页组件只负责稳定外壳和通用页码算法，具体 URL 或 onclick 仍由调用方提供。
- 在线脚本页是较低风险切片；任务、日志分页后续应在相关业务改动收束后再接入。

## 阶段 11：低风险消息卡组件抽取

状态：已完成

目标：

- 在不引入模板引擎或前端构建链的前提下抽取成功/失败/普通提示卡。
- 集中处理提示文字 HTML 转义，减少路由内联字符串拼接。
- 先接入当前未被长期脏改动影响的在线脚本页面。

已完成：

- 在 `fls_manager/ui/components.py` 新增 `message_card()`：
  - 空消息返回空字符串，调用方不需要重复判断。
  - 纯空白消息按空消息处理。
  - 支持 `success`、`error`、`info` 三类颜色。
  - 未知 `kind` 回退到 `info` 颜色。
  - 支持 `strong=True` 加粗强调。
  - 统一对提示文字做 HTML escape。
- 更新 `fls_manager/routes/online_scripts/_common.py`，让在线脚本子路由可通过通配导入复用 `message_card()`。
- 替换 `fls_manager/routes/online_scripts/pages.py` 的成功/失败消息提示卡。
- 替换 `fls_manager/routes/online_scripts/source_json.py` 的成功/失败消息提示卡。
- 扩展 `tests/test_ui_components.py`：
  - 覆盖空消息和纯空白消息返回空。
  - 覆盖成功、错误、普通提示颜色。
  - 覆盖未知 `kind` 回退到普通提示颜色。
  - 覆盖加粗样式。
  - 覆盖消息内容 HTML 转义。
- 子代理 A/B 在阶段开始前完成只读审查，建议本阶段优先抽取 `message_card()`，暂缓摘要网格和复杂页面；主代理按此收敛范围实施。

验证记录：

- `python -B -m unittest tests.test_ui_components`：通过，8 tests OK。
- `python -B -m unittest discover -s tests`：通过，86 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只替换在线脚本列表页和脚本源 JSON 页，没有触碰存在阶段外未提交改动的任务、日志等页面。

组件策略结论：

- `message_card()` 只接收纯文本消息，组件内部负责转义；未知类型回退到 `info`，如后续需要动作按钮或富文本，需要新建明确的安全接口。
- 摘要网格暂不抽通用 `summary_grid`，因为现有页面结构差异较大；后续可以先抽更小粒度的 `summary_item()`。

## 阶段 12：消息卡第二批接入与路由渲染测试

状态：已完成

目标：

- 继续把 `message_card()` 接入低风险、未处于长期脏改动的页面。
- 保持组件只处理纯文本消息，不处理富文本或复杂 JS 状态。
- 补充路由层测试，验证组件接入后的页面渲染和 HTML 转义。

已完成：

- 替换 `fls_manager/routes/online_scripts/docs.py` 的文档加载失败提示卡：
  - 保留 `err` 为空时不渲染提示卡的行为。
  - 使用 `message_card(..., "error", strong=True)` 统一错误色、加粗和转义。
- 替换 `fls_manager/routes/scripts/files.py` 的 3 个普通提示卡：
  - `/pull/new` 新建脚本页的“暂无操作/新建失败”提示。
  - `/scripts/view` 查看编辑文件页的“暂无保存操作/保存结果”提示。
  - `/scripts/rename` 改名页的“暂无操作/改名失败”提示。
- 更新 `tools/responsive_smoke.py`：
  - 将 `/pull/new` 纳入页面结构 smoke，覆盖脚本新建页的基础渲染。
- 新增 `tests/test_ui_route_components.py`：
  - 覆盖 `/pull/new` 渲染默认 info 消息卡。
  - 覆盖 `/online-scripts/doc/<id>` 文档加载失败时错误消息卡渲染和 HTML 转义。
- 子代理 A 完成消息卡候选只读审查，提示不要扩大到复杂 JS 状态或富文本结果；主代理已据此只收束当前改动。
- 子代理 B 完成摘要 item 只读审查，结论为可做但收益较小，本阶段未采用。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，2 tests OK。
- `python -B -m unittest tests.test_ui_components`：通过，8 tests OK。
- `python -B -m unittest discover -s tests`：通过，88 tests OK。
- `python -B tools/responsive_smoke.py`：通过，包含 `/pull/new`。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- `online_scripts/install.py`、`about/version.py` 等错误提示与标题/按钮或结果详情绑定更紧，本阶段未用 `message_card()` 强拆。

组件策略结论：

- `message_card()` 适合替换纯文本提示卡；复杂状态卡、富文本结果和带动作按钮的错误页暂不纳入。
- 路由层测试可以低成本覆盖组件接入后的真实渲染，比只测组件字符串更能防止导入和条件渲染回归。
- `summary_item()` 可以作为后续小范围候选，但当前未脏页面只有在线脚本页 3 个 item，复用收益较低。

## 阶段 13：脚本拉取结果卡接入

状态：已完成

目标：

- 继续把 `message_card()` 接入脚本拉取/导入结果卡。
- 保留原结果卡的“结果”标题，不引入卡片套卡。
- 用显式状态变量控制成功、失败和空状态样式，避免依赖文案推断。

已完成：

- 扩展 `fls_manager/ui/components.py`：
  - `message_card()` 新增可选 `title` 参数。
  - 标题同样使用 `h()` 做 HTML escape。
  - 未传标题时保持原输出结构兼容。
- 更新 `fls_manager/routes/scripts/pull.py`：
  - 新增 `pull_result_card()` 复用 `message_card(..., title="结果")`。
  - `/pull/fetch` 使用 `msg_kind` / `msg_strong` 显式区分空状态、拉取成功、校验失败和拉取异常。
  - `/pull/import` 使用 `msg_kind` / `msg_strong` 显式区分空状态、导入成功、校验失败和导入异常。
  - 保留原表单、代理、下载、Git、解压和路径安全逻辑不变。
- 扩展 `tests/test_ui_components.py`：
  - 覆盖 `message_card(title=...)` 标题渲染和标题 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/pull/fetch` 空 URL 错误卡。
  - 覆盖 `/pull/fetch` 文件拉取成功卡，使用 mock 避免真实网络。
  - 覆盖 `/pull/fetch` 拉取异常消息 HTML 转义。
  - 覆盖 `/pull/import` 无文件错误卡。
  - 覆盖 `/pull/import` 普通文件导入成功卡，使用临时 `FLS_BASE_DIR`。
- 更新 `tools/responsive_smoke.py`：
  - 将 `/pull/fetch` 和 `/pull/import` 纳入页面结构 smoke。
- 子代理 A 完成 `scripts/pull.py` 只读审查，建议只替换两处纯文本结果卡，并改为显式状态变量；本阶段已采纳。
- 子代理 B 完成 `summary_item()` 只读审查，认为该方向收益低于本阶段消息卡接入，本阶段未采用。

验证记录：

- `python -B -m unittest tests.test_ui_components tests.test_ui_route_components`：通过，16 tests OK。
- `python -B -m unittest discover -s tests`：通过，94 tests OK。
- `python -B tools/responsive_smoke.py`：通过，包含 `/pull/fetch` 和 `/pull/import`。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只覆盖普通文件拉取/导入分支；Git 仓库拉取、zip/tar 导入继续依赖既有逻辑和安全测试。

组件策略结论：

- `message_card(title=...)` 适合带短标题的纯文本结果卡，可以避免卡片套卡并保留页面语义。
- 路由中应显式维护消息状态，不应依赖中文文案包含“成功”来推断样式。
- `summary_item()` 仍可作为后续小范围候选，但当前复用收益较低。

## 阶段 14：在线脚本摘要项组件抽取

状态：已完成

目标：

- 抽取小粒度 `summary_item(label, value)`。
- 只替换在线脚本页 3 个同构 `.fls-summary-item`。
- 保留外层 `.fls-summary-grid`，不抽通用 summary grid。

已完成：

- 在 `fls_manager/ui/components.py` 新增 `summary_item(label, value)`：
  - 输出 `.fls-summary-item`、`.fls-summary-label`、`.fls-summary-num` 结构。
  - 统一对 label 和 value 做 HTML escape。
  - 支持数字 value。
- 更新 `fls_manager/routes/online_scripts/_common.py`：
  - 将 `summary_item()` 暴露给在线脚本子路由。
- 更新 `fls_manager/routes/online_scripts/pages.py`：
  - 替换“缓存脚本数”“可导入任务”“有安装命令”三个统计项。
  - 保留外层 `.fls-summary-grid` 和页面其它结构。
- 扩展 `tests/test_ui_components.py`：
  - 覆盖 `summary_item()` 输出结构。
  - 覆盖 label/value HTML 转义。
  - 覆盖数字 value 渲染。
- 子代理尝试并行审查 `summary_item()` 与未脏页面候选，但本轮两个子代理均因 429 限流失败；主代理按阶段 13 交接文档和现有代码完成低风险实现。

验证记录：

- `python -B -m unittest tests.test_ui_components`：通过，11 tests OK。
- `python -B -m unittest discover -s tests`：通过，96 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- `summary_item()` 当前只在在线脚本页复用；任务页的 `.fls-summary-item` 结构更复杂，仍不接入。

组件策略结论：

- `summary_item()` 只负责最小 label/value 统计项，不承担网格布局职责。
- 不抽通用 `summary_grid`，避免把结构差异较大的页面硬统一。
- 后续组件抽取继续优先选择未脏文件和纯文本/稳定结构，避免复杂 JS 状态。

## 阶段 15：任务配置保存结果卡接入

状态：已完成

目标：

- 继续查找未脏页面中的纯文本提示卡。
- 复用已有 `message_card()`，不新增组件 API。
- 避开复杂 JS 状态、富文本结果、带按钮的完整错误页，以及当前长期脏改动文件。

已完成：

- 更新 `fls_manager/routes/tasks/config_file.py`：
  - 导入 `message_card()`。
  - 将任务配置保存成功/失败的两段内联结果卡替换为 `message_card()`。
  - 保留成功绿色、失败红色、加粗强调和空消息不渲染行为。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/task/config/<id>` 保存成功后渲染绿色加粗消息卡。
  - 覆盖配置文件实际写入内容。
  - 覆盖写入失败后渲染红色加粗消息卡，并对异常消息做 HTML 转义。
- 更新 `DEVELOPMENT.md`：
  - 将 `summary_item()` 补入当前组件列表。
  - 将任务配置保存结果卡纳入已覆盖路由组件接入。
  - 增加阶段 15 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，9 tests OK。
- `python -B -m unittest discover -s tests`：通过，98 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段仅覆盖任务配置保存结果卡；无 `config_path` 和路径非法的完整错误页仍保留原结构，因为它们带返回/编辑按钮。

组件策略结论：

- `message_card()` 继续只处理纯文本消息和短标题，不承载动作按钮。
- 对写文件失败等异常消息，路由层可以直接传纯文本给组件，由组件集中做 HTML 转义。
- 后续接入仍应优先选择未脏文件里的稳定结构，避免为了抽组件而拆散完整操作页。

## 阶段 16：表格卡组件增强与依赖/状态页接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和稳定表格结构。
- 扩展现有 `table_card()`，避免新增重复表格外壳。
- 保留运行环境页既有 `runtimeTable` ID，避免破坏移动端响应式 CSS。

已完成：

- 更新 `fls_manager/ui/components.py`：
  - `table_card()` 新增可选 `help_html`。
  - `table_card()` 新增可选 `actions_html`，使用 `.action-row` 承载操作按钮。
  - `table_card()` 新增可选 `table_id`，用于保留页面已有响应式表格选择器。
  - 保持旧调用兼容，继续转义 title 和 headers。
- 更新 `fls_manager/routes/deps.py`：
  - `/deps` 的“已安装依赖”表格接入 `table_card()`。
  - `/deps/refresh` 的“核心依赖检测”表格接入 `table_card()`，返回按钮放入操作区。
- 更新 `fls_manager/routes/status.py`：
  - `/panel/status` 的运行环境表格接入 `table_card()`。
  - 通过 `table_id="runtimeTable"` 保留移动端 CSS 依赖。
- 扩展 `tests/test_ui_components.py`：
  - 覆盖 `table_card()` 可选说明区、操作区、表格 ID。
  - 覆盖标题和表头 HTML 转义。
  - 覆盖 `rows_html` 作为调用方已构造 HTML 透传。
- 扩展 `tests/test_ui_route_components.py`：
  - mock `pip_cmd()` 覆盖 `/deps` 依赖列表渲染和包名/版本转义。
  - mock `runtime_items()` 覆盖 `/panel/status` 表格 ID 保留和运行时字段转义。
- 更新 `tests/test_task_runtime.py`：
  - 合并远端任务运行历史改动后，测试改为覆盖当前 `task_retry_config()` 和 `schedule_task_retry()`。
  - 启动链路测试改为直接覆盖 `_start_task_worker()` 内的环境合并、`subprocess.Popen()` 参数和 watcher 线程提交。
- 更新 `DEVELOPMENT.md`：
  - 将 `table_card()` 可选结构测试纳入已覆盖项。
  - 将 `/deps`、`/deps/refresh`、`/panel/status` 表格卡接入纳入路由组件覆盖。
  - 增加阶段 16 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_components tests.test_ui_route_components`：通过，23 tests OK。
- `python -B -m unittest discover -s tests`：通过，100 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- `table_card()` 的 `help_html` 和 `actions_html` 是调用方构造的 HTML 片段；用户输入仍必须在调用前显式转义。

组件策略结论：

- `table_card()` 适合稳定表格外壳，复杂批量工具栏、实时状态和动态 JS 表格仍不强行接入。
- 页面已有 CSS/JS 依赖的表格 ID 必须通过 `table_id` 保留。
- 后续可继续在未脏页面接入 `table_card()`，但任务/日志分页仍等相关长期改动收束后再处理。

## 阶段 17：脏文件功能收束与批量操作回归测试

状态：已完成

目标：

- 按用户要求优先处理长期脏文件方向，确认远端基线已承接的功能，不重复搬运旧实现。
- 修正旧 `retry_count` 与当前 `retry` 配置的读取迁移缺口。
- 为任务复制、任务批量操作、合集批量加入和日志分组删除补回归测试。

已完成：

- 更新 `fls_manager/models.py`：
  - 新增 `normalize_task_retry()`。
  - 读取旧 `retry_count` 时迁移到 `retry.attempts`，默认 `retry.interval_seconds=60`。
  - 保存归一化任务时移除旧 `retry_count`，保持和任务表单、运行器的当前 `retry` 结构一致。
- 新增 `tests/test_bulk_workflows.py`：
  - 覆盖 `/api/task/action/copy/<id>` 复制任务时重置 `run_count`、`pinned`、`last_run_at`，并保留 `retry` 配置。
  - 覆盖 `/api/task/bulk-action` 的去重禁用、批量取出合集、批量删除和空选择拒绝。
  - 覆盖 `/collection/add-task/<collection_id>` 一次加入多个任务，并检查合集页存在多选和批量工具栏 UI。
  - 覆盖 `/api/logs/groups/delete` 删除选中日志分组，以及空选择返回 400。
- 更新 `tests/test_schema_migration.py`：
  - 覆盖旧 `retry_count` 到新 `retry` 的读取迁移和写回清理。
- 更新 `DEVELOPMENT.md` 与 `docs/DATA_SCHEMA.md`：
  - 将任务重试规范字段改为 `retry`。
  - 明确 `retry_count` 仅作为旧数据兼容字段。

验证记录：

- `python -B -m unittest tests.test_schema_migration tests.test_bulk_workflows`：通过，11 tests OK。
- `python -B -m unittest discover -s tests`：通过，106 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有在原长期脏工作区执行 destructive reset；开发和提交继续基于干净临时 worktree。

收束结论：

- 原工作区的大部分脏文件能力已经存在于远端基线，本阶段主要补齐可验证的兼容迁移和回归测试。
- 旧 `retry_count` 不能再作为新实现入口；后续任务重试相关开发统一使用 `retry.attempts` 和 `retry.interval_seconds`。

## 阶段 18：CSRF 与破坏性路由安全回归测试

状态：已完成

目标：

- 继续按用户要求处理长期脏文件剩余方向，识别旧实现回退风险。
- 固化 CSRF、防 GET 破坏性操作和 `X-Token` API 调用边界。
- 避免后续清理原脏工作区时误把安全约束改回旧状态。

已完成：

- 扩展 `tests/test_auth_backup.py`：
  - 覆盖页面 `layout()` 输出 `<meta name="csrf-token">` 并向 POST 表单注入隐藏 `csrf_token`。
  - 覆盖普通 session POST 缺少 CSRF token 时返回 400，且不写入任务数据。
  - 覆盖普通 session POST 携带有效 CSRF token 时可正常创建任务。
  - 覆盖 `X-Token` 管理请求可绕过 CSRF，继续支持脚本/API 客户端。
  - 覆盖 `/logfile/delete/<filename>`、`/collection/delete/<id>`、`/task/pin/<id>` 拒绝 GET，并保持原数据不变。
- 更新 `DEVELOPMENT.md`：
  - 明确 `create_app()` 同时注册 `csrf_before_request` 和 `auth_before_request`。
  - 记录 CSRF token 注入、fetch 自动带 token、`X-Token` 豁免和破坏性页面路由必须 POST 的约定。

验证记录：

- `python -B -m unittest tests.test_auth_backup`：通过，20 tests OK。
- `python -B -m unittest discover -s tests`：通过，111 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补安全回归测试和开发文档，没有改动路由实现。

收束结论：

- 原脏 diff 中移除 CSRF、把删除/置顶改回 GET、回退 POST 表单的方向不应继续迁入。
- 后续处理原工作区时，可以优先丢弃这些旧实现回退，只保留已经在远端基线中通过测试覆盖的功能。

## 下一阶段候选

- 阶段 19：继续查找未脏页面中的纯文本提示卡或稳定表格卡，避免复杂 JS 状态、富文本结果和带按钮的完整错误页。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/online-scripts` 的摘要项和脚本拉取页面。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。
