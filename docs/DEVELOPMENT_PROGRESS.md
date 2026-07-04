# FLS 开发进度

更新时间：2026-07-05

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
- `load_tasks()` / `save_tasks()` 现在会统一清洗任务结构，迁移旧 `notify_ids`，归一化 `random_delay`、`retry_count`、`run_count`、`enabled`、`pinned` 和任务环境变量。
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
- 覆盖 `task_retry_count()` 的坏类型、负数、超上限和正常字符串值。
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
- 更新 `DEVELOPMENT.md`：
  - 将 `table_card()` 可选结构测试纳入已覆盖项。
  - 将 `/deps`、`/deps/refresh`、`/panel/status` 表格卡接入纳入路由组件覆盖。
  - 增加阶段 16 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_components tests.test_ui_route_components`：通过，23 tests OK。
- `python -B -m unittest discover -s tests`：通过，101 tests OK。
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

## 下一阶段候选

- 阶段 17：继续查找未脏页面中的纯文本提示卡或稳定表格卡，避免复杂 JS 状态、富文本结果和带按钮的完整错误页。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/online-scripts` 的摘要项和脚本拉取页面。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 17：仪表盘环境表格卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和稳定表格结构。
- 复用已有 `table_card()`，不新增组件 API。
- 避开任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外改动。

已完成：

- 更新 `fls_manager/routes/dashboard.py`：
  - 导入 `table_card()`。
  - 将仪表盘“环境状态”表格接入 `table_card()`。
  - 保留峰值 CPU 自动重置说明和当前峰值统计周期说明。
  - 保留原有 `env_rows` 数据构造和字段转义逻辑。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/` 仪表盘环境状态表格卡渲染。
  - 断言表格标题、表头、说明文案和关键环境行存在。
- 更新 `DEVELOPMENT.md`：
  - 将 `/` 仪表盘环境状态表格卡纳入路由组件覆盖。
  - 增加阶段 17 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，12 tests OK。
- `python -B -m compileall fls_manager/routes/dashboard.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，102 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 仪表盘环境表格没有既有表格 ID 或 JS 选择器依赖，因此未设置 `table_id`。

组件策略结论：

- `table_card()` 可以继续接入稳定只读表格；动态 JS 表格、复杂工具栏和特殊折叠结构仍不强行统一。
- 接入表格卡前需确认是否存在 CSS/JS 依赖的表格 ID；本阶段目标表格无该依赖。
- 后续仍优先选择未脏页面的小范围组件化，避免与长期业务改动交叉。

## 下一阶段候选

- 阶段 18：继续查找未脏页面中的纯文本提示卡或稳定表格卡，例如关于页的只读信息表格，但避免嵌套卡片和折叠更新日志。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/`、`/online-scripts`、`/pull/fetch`、`/pull/import`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 18：关于页面板信息表格卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和稳定只读表格。
- 复用已有 `table_card()`，不新增组件 API。
- 避开关于页折叠更新日志、时间校准嵌套卡片和长期阶段外脏文件。

已完成：

- 更新 `fls_manager/routes/about/page.py`：
  - 导入 `table_card()`。
  - 将“面板信息”只读表格接入 `table_card()`。
  - 保留项目仓库链接、主进程名、任务进程标识前缀、目录路径和控制脚本字段。
  - 保留动态字段和路径字段的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/about` 面板信息表格卡渲染。
  - 断言表格标题、表头、项目仓库链接和控制脚本字段存在。
- 更新 `tools/responsive_smoke.py`：
  - 将 `/about` 纳入页面结构 smoke。
- 更新 `DEVELOPMENT.md`：
  - 将 `/about` 面板信息表格卡纳入路由组件覆盖。
  - 增加阶段 18 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，13 tests OK。
- `python -B -m compileall fls_manager/routes/about/page.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，103 tests OK。
- `python -B tools/responsive_smoke.py`：通过，包含 `/about`。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 关于页的更新日志折叠表格和时间校准嵌套卡片结构暂不组件化，避免扩大改动面。

组件策略结论：

- `table_card()` 适合只读、稳定、无 JS 选择器依赖的表格。
- 带链接的单元格可以作为调用方已构造 HTML 传入，但用户输入仍必须在调用前显式转义。
- 后续继续优先选择未脏页面的小范围改动；复杂折叠、嵌套卡片和动态 JS 区域暂缓。

## 下一阶段候选

- 阶段 19：继续查找未脏页面中的稳定表格卡或纯文本提示卡；可评估后台任务日志页、通知测试结果页等小页面。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/about`、`/`、`/online-scripts`、`/pull/fetch`、`/pull/import`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 19：通知测试结果表格卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型稳定页面。
- 复用已有 `table_card()`，不新增组件 API。
- 保留通知测试结果的返回操作，不把带动作按钮的页面硬塞进 `message_card()`。

已完成：

- 更新 `fls_manager/routes/notify/test.py`：
  - 导入 `table_card()`。
  - 将通知测试结果详情接入 `table_card()`。
  - 使用 badge 展示成功/失败状态。
  - 通过 `actions_html` 保留“返回通知管理”按钮。
  - 保留通知名称、渠道名称和返回消息的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/notify/test/<id>` 通知测试结果表格卡渲染。
  - mock `send_one()`，避免真实发送通知。
  - 断言通知名称、渠道、失败 badge、返回消息转义和返回按钮存在。
- 更新 `DEVELOPMENT.md`：
  - 将 `/notify/test/<id>` 通知测试结果表格卡纳入路由组件覆盖。
  - 增加阶段 19 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，14 tests OK。
- `python -B -m compileall fls_manager/routes/notify/test.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，104 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只 mock 通知发送出口，不真实调用任何通知渠道。

组件策略结论：

- 带短操作区的稳定结果详情页适合 `table_card(actions_html=...)`。
- `message_card()` 仍只用于纯文本提示，不承载带字段列表和动作按钮的结果页。
- 后续继续优先选择未脏页面的小范围改动；动态日志和复杂 JS 区域暂缓。

## 下一阶段候选

- 阶段 20：继续查找未脏页面中的稳定表格卡或纯文本提示卡；可评估后台任务日志不存在页等小页面，但避免实时日志主体。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/about`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 20：后台任务日志头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型稳定页面。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换后台任务日志页头部卡，不触碰实时日志主体和自动刷新脚本。

已完成：

- 更新 `fls_manager/routes/about/jobs.py`：
  - 导入 `page_header_card()`。
  - 将后台任务记录不存在提示接入头部卡，保留“返回”和“查看日志管理”入口。
  - 将存在记录时的任务状态头部接入头部卡，保留状态、日志文件、更新时间和三个操作按钮。
  - 保留实时日志 `<pre id="log">`、日志控制条和 `loadAboutJobLog()` 自动刷新逻辑。
  - 保留动态标题、状态、日志文件和返回地址的 HTML 转义。
- 扩展 `tests/test_ui_components.py`：
  - 覆盖 `page_header_card()` 的标题转义、说明区和操作区渲染。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/about/job-log/<id>` 不存在记录提示卡渲染。
  - 覆盖 `/about/job-log/<id>` 存在记录头部卡渲染、动态字段转义、操作按钮和实时日志主体保留。
- 更新 `DEVELOPMENT.md`：
  - 将 `/about/job-log/<id>` 后台任务日志头部卡纳入路由组件覆盖。
  - 增加阶段 20 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_components`：通过，13 tests OK。
- `python -B -m unittest tests.test_ui_route_components`：通过，16 tests OK。
- `python -B -m compileall fls_manager/routes/about/jobs.py tests/test_ui_components.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，107 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证页面初始渲染和静态日志 shell，不通过真实浏览器执行后台任务日志自动刷新脚本。

组件策略结论：

- `page_header_card()` 适合小型说明加操作按钮的页面头部。
- 实时日志主体、自动滚动和轮询脚本保持原样，不强行组件化。
- 后续继续优先选择未脏页面的小范围改动；复杂 JS 状态和嵌套卡片暂缓。

## 下一阶段候选

- 阶段 21：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估关于页面板控制结果页，但避免影响真实启停行为。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/about`、`/about/job-log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 21：面板控制结果头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型结果页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换面板重启/停止结果页渲染外壳，不改变真实重启/停止行为。

已完成：

- 更新 `fls_manager/routes/about/panel_control.py`：
  - 导入 `page_header_card()`。
  - 将重启失败、正在重启、停止失败、正在停止四个结果卡接入头部卡。
  - 保留控制脚本缺失时的红色强调提示和平台路径说明。
  - 保留成功分支中的系统类型、当前 PID、控制脚本路径、返回/日志入口。
  - 保留重启成功页 10 秒后返回仪表盘的 `setTimeout()` 脚本，并修正普通字符串中的 JS 单花括号。
  - 保留所有动态路径字段 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/about/restart-panel` 控制脚本缺失提示卡渲染与路径转义。
  - 覆盖 `/about/restart-panel` 成功结果卡渲染，并 mock `threading.Thread`，避免真实重启。
  - 覆盖 `/about/stop-panel` 控制脚本缺失提示卡渲染与路径转义。
  - 覆盖 `/about/stop-panel` 成功结果卡渲染，并 mock `threading.Thread`，避免真实停止。
- 更新 `DEVELOPMENT.md`：
  - 将 `/about/restart-panel`、`/about/stop-panel` 面板控制结果头部卡纳入路由组件覆盖。
  - 增加阶段 21 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，20 tests OK。
- `python -B -m compileall fls_manager/routes/about/panel_control.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，111 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段通过 mock 验证重启/停止结果页渲染，没有执行真实面板重启或停止。

组件策略结论：

- `page_header_card()` 适合小型结果页和说明加操作按钮的控制结果页。
- 带真实副作用的路由测试必须 mock 外部动作出口，只验证渲染和调度提交，不触发真实启停。
- 后续继续优先选择未脏页面的小范围改动；复杂 JS 状态、认证/API 和任务/日志脏文件暂缓。

## 下一阶段候选

- 阶段 22：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估在线脚本安装确认页、安装日志页等，但避开复杂安装状态和富文本结果。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/about`、`/about/job-log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 22：在线脚本安装日志头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型日志页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换在线脚本安装日志页头部卡，不触碰实时日志主体、自动刷新脚本和安装状态逻辑。

已完成：

- 更新 `fls_manager/routes/online_scripts/_common.py`：
  - 导入 `page_header_card()`，供在线脚本子路由通配导入复用。
- 更新 `fls_manager/routes/online_scripts/logs.py`：
  - 将安装记录不存在提示接入头部卡，保留“返回”和“查看日志管理”入口。
  - 将存在记录时的安装状态头部接入头部卡，保留状态、日志文件、返回、脚本管理、任务管理和停止安装按钮。
  - 保留实时日志 `<pre id="log">`、日志控制条和 `loadLog()` 自动刷新逻辑。
  - 保留动态脚本名称、状态、日志文件、安装 ID 和返回地址的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/online-scripts/log/<id>` 不存在记录提示卡渲染。
  - 覆盖 `/online-scripts/log/<id>` 存在且运行中记录头部卡渲染、动态字段转义、停止安装按钮和实时日志主体保留。
- 更新 `DEVELOPMENT.md`：
  - 将 `/online-scripts/log/<id>` 在线脚本安装日志头部卡纳入路由组件覆盖。
  - 增加阶段 22 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，22 tests OK。
- `python -B -m compileall fls_manager/routes/online_scripts/_common.py fls_manager/routes/online_scripts/logs.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，113 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证在线脚本安装日志页初始渲染和静态日志 shell，不通过真实浏览器执行自动刷新脚本，也不执行真实在线脚本安装。

组件策略结论：

- `page_header_card()` 适合小型日志页头部，动态日志主体和轮询脚本保持原样。
- 运行中任务的停止按钮可以作为调用方构造的 `actions_html` 传入，但安装 ID 必须在调用方先转义。
- 后续继续优先选择未脏页面的小范围改动；涉及真实安装、下载或复杂 JS 状态的流程暂缓。

## 下一阶段候选

- 阶段 23：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估在线脚本安装目标已存在确认页，但避免改变实际安装提交逻辑。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/online-scripts/log/<id>`、`/about`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 23：在线脚本安装确认头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型确认页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换在线脚本安装目标路径非法和目标已存在确认页头部，不改变实际安装提交逻辑。

已完成：

- 更新 `fls_manager/routes/online_scripts/install.py`：
  - 将目标路径非法错误页接入 `page_header_card()`，保留返回在线脚本入口。
  - 将目标已存在确认页的说明头部接入 `page_header_card()`。
  - 保留继续安装表单、`force=1` 隐藏字段、导入任务隐藏字段、代理选择和确认继续按钮。
  - 保留目标路径、错误消息和脚本 ID 的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/online-scripts/install/<id>` 目标路径非法提示卡渲染。
  - 覆盖 `/online-scripts/install/<id>` 目标已存在确认页渲染、目标路径转义、继续安装表单和隐藏字段保留。
  - 测试只写入本地缓存和本地目标文件，不触发真实下载、安装线程或网络请求。
- 更新 `DEVELOPMENT.md`：
  - 将 `/online-scripts/install/<id>` 在线脚本安装确认头部卡纳入路由组件覆盖。
  - 增加阶段 23 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，24 tests OK。
- `python -B -m compileall fls_manager/routes/online_scripts/install.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，115 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证确认页初始渲染，不执行真实在线脚本下载、Git 拉取、任务导入或后台安装线程。

组件策略结论：

- `page_header_card()` 适合小型错误/确认页头部，实际业务表单继续由原路由保留。
- 带副作用的确认页测试应停在确认页面渲染，不提交 `force=1` 进入真实安装流程。
- 后续继续优先选择未脏页面的小范围改动；真实安装、下载和复杂 JS 状态流程暂缓。

## 下一阶段候选

- 阶段 24：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估在线脚本文档无 doc_link 提示或安装选择页小型说明块，但避免复杂任务选择 JS。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/online-scripts/install/<id>` 确认页、`/online-scripts/log/<id>`、`/about`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 24：在线脚本文档无链接提示头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和无副作用小页面。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换在线脚本文档无 `doc_link` 提示页，不触碰文档下载、渲染和 iframe 逻辑。

已完成：

- 更新 `fls_manager/routes/online_scripts/docs.py`：
  - 将无 `doc_link` 的提示卡接入 `page_header_card()`。
  - 保留“返回在线脚本”入口。
  - 文档下载、Markdown 渲染、网页 iframe 和错误消息逻辑均保持原样。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/online-scripts/doc/<id>` 无文档链接提示卡渲染。
  - 断言标题、提示文字、操作区和返回入口存在。
- 更新 `DEVELOPMENT.md`：
  - 将 `/online-scripts/doc/<id>` 无文档链接提示卡纳入路由组件覆盖。
  - 增加阶段 24 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，25 tests OK。
- `python -B -m compileall fls_manager/routes/online_scripts/docs.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，116 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证无文档链接提示页初始渲染，不真实请求文档地址。

组件策略结论：

- `page_header_card()` 适合带单个返回操作的小型空状态/提示页。
- 文档正文渲染、iframe 和错误回退仍保持原有结构，不强行组件化。
- 后续继续优先选择未脏页面的小范围改动；复杂任务选择 JS 和真实安装流程暂缓。

## 下一阶段候选

- 阶段 25：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估在线脚本安装选择页中的静态说明块，但避免复杂任务选择 JS。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/online-scripts/doc/<id>` 无文档链接页、`/online-scripts/install/<id>` 确认页、`/online-scripts/log/<id>`、`/about`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 25：在线脚本安装选择页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和静态说明块。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换在线脚本安装选择页顶部说明卡，不触碰任务选择列表、分页和 JS 状态逻辑。

已完成：

- 更新 `fls_manager/routes/online_scripts/install_select.py`：
  - 将顶部“选择任务并安装”说明卡接入 `page_header_card()`。
  - 保留脚本 ID、脚本类型、保存名、任务总数和 `selectedTaskCount` 动态计数节点。
  - 保留返回在线脚本入口。
  - 保留安装选项、搜索任务、任务选择列表、分页、底部提交按钮和全部任务选择 JS。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/online-scripts/install-select/<id>` 顶部头部卡渲染。
  - 断言脚本名称和保存名 HTML 转义、`selectedTaskCount`、表单 ID、`select_mode=all` 隐藏字段、任务选择区和分页 JS 保留。
- 更新 `DEVELOPMENT.md`：
  - 将 `/online-scripts/install-select/<id>` 安装选择页头部卡纳入路由组件覆盖。
  - 增加阶段 25 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，26 tests OK。
- `python -B -m compileall fls_manager/routes/online_scripts/install_select.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，117 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证安装选择页初始渲染，不通过真实浏览器执行任务选择 JS，也不提交安装或导入任务表单。

组件策略结论：

- `page_header_card()` 可以承载带动态计数节点的静态头部，但调用方必须保留已有元素 ID。
- 复杂任务选择 JS、分页和批量选择工具栏保持原有结构，不强行组件化。
- 后续继续优先选择未脏页面的小范围改动；真实安装、下载和复杂 JS 状态流程暂缓。

## 下一阶段候选

- 阶段 26：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估在线脚本安装选择页中“搜索任务”静态表单外壳，但避免改任务列表和选择 JS。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>` 无文档链接页、`/online-scripts/install/<id>` 确认页、`/online-scripts/log/<id>`、`/about`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 26：关于页版本失败结果头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和失败结果页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换刷新更新日志和更新版本的失败结果页，不触发真实 Git 后台任务。

已完成：

- 更新 `fls_manager/routes/about/version.py`：
  - 导入 `page_header_card()`。
  - 将刷新更新日志时未安装 Git、非 Git 仓库两个失败页接入头部卡。
  - 将更新版本时版本号非法、未安装 Git、非 Git 仓库三个失败页接入头部卡。
  - 保留返回关于页入口。
  - 保留版本号、工作目录等动态字段 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/about/refresh-log` 未安装 Git 失败页渲染。
  - 覆盖 `/about/refresh-log` 非 Git 仓库失败页渲染。
  - 覆盖 `/about/update-version` 非法版本号失败页渲染和输入转义。
  - 通过 mock Git 可用性/仓库状态避免启动真实后台任务。
- 更新 `DEVELOPMENT.md`：
  - 将 `/about/refresh-log`、`/about/update-version` 版本失败头部卡纳入路由组件覆盖。
  - 增加阶段 26 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，29 tests OK。
- `python -B -m compileall fls_manager/routes/about/version.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，120 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证失败结果页初始渲染，没有执行真实 Git refresh/update 后台任务。

组件策略结论：

- `page_header_card()` 适合带单个返回操作的小型失败结果页。
- 带后台任务副作用的路由测试应停在失败分支，或 mock 外部条件，不进入真实任务启动。
- 后续继续优先选择未脏页面的小范围改动；真实更新、安装、下载和复杂 JS 状态流程暂缓。

## 下一阶段候选

- 阶段 27：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估备份导入完成页或备份导入失败页，但避免执行真实大范围恢复。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 27：备份导入完成页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型结果页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换备份导入成功结果页，不改变上传、解压、安全检查、恢复和依赖安装逻辑。

已完成：

- 更新 `fls_manager/routes/backup/restore.py`：
  - 导入 `page_header_card()`。
  - 将 `/backup/import` 成功结果页接入头部卡。
  - 保留已恢复内容、依赖恢复状态、依赖日志路径、返回备份恢复和查看日志入口。
  - 保留恢复内容、依赖消息和日志路径的 HTML 转义。
  - 未改动未上传文件、未选择恢复内容和失败分支的既有响应形态。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/backup/import` 导入小型 tar.gz 后的成功结果页渲染。
  - 测试使用临时 `FLS_BASE_DIR` 和内存归档，只恢复测试目录内的 `data/config.json`。
  - mock `reload_scheduler()`，避免触发真实调度器刷新副作用。
- 更新 `DEVELOPMENT.md`：
  - 将 `/backup/import` 备份导入完成页头部卡纳入路由组件覆盖。
  - 增加阶段 27 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，30 tests OK。
- `python -B -m compileall fls_manager/routes/backup/restore.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，121 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证小型隔离备份的 `data` 恢复成功路径，没有执行真实大范围恢复、脚本目录恢复或依赖安装。

组件策略结论：

- `page_header_card()` 适合备份导入完成这类简短结果页，恢复详情作为 `help_html` 保留即可。
- 对有文件系统副作用的路由测试必须使用临时 `FLS_BASE_DIR`，并优先构造最小归档。
- 备份失败和早期参数错误分支仍保持现状，避免一次阶段同时改变 HTTP 状态和响应形态。

## 下一阶段候选

- 阶段 28：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估备份导入早期错误/失败提示是否适合接入组件，但需确认是否接受从纯文本改为 HTML。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/backup`、备份导入完成页、`/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 28：脚本调试日志头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型日志页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换脚本调试日志页头部卡，不触碰真实调试启动、停止、日志轮询和 API 逻辑。

已完成：

- 更新 `fls_manager/routes/scripts/debug.py`：
  - 导入 `page_header_card()`。
  - 将 `/scripts/debug-log/<id>` 调试记录不存在提示接入头部卡，保留返回和日志管理入口。
  - 将存在记录时的状态头部接入头部卡，保留运行状态、PID、脚本路径、日志文件、停止调试按钮、返回和脚本管理入口。
  - 保留实时日志 `<pre id="log">`、日志浮动控制和 `loadScriptDebugLog()` 自动刷新逻辑。
  - 保留动态 PID、脚本路径、日志文件、调试 ID 和返回地址的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/scripts/debug-log/<id>` 不存在记录提示卡渲染。
  - 覆盖 `/scripts/debug-log/<id>` 存在且运行中记录头部卡渲染、动态字段转义、停止调试按钮和实时日志主体保留。
- 更新 `DEVELOPMENT.md`：
  - 将 `/scripts/debug-log/<id>` 脚本调试日志头部卡纳入路由组件覆盖。
  - 增加阶段 28 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，32 tests OK。
- `python -B -m compileall fls_manager/routes/scripts/debug.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，123 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证脚本调试日志页初始渲染和静态日志 shell，不启动真实脚本调试进程，也不通过真实浏览器执行自动刷新脚本。

组件策略结论：

- `page_header_card()` 适合小型实时日志页头部，动态日志主体、浮动日志控制和轮询脚本保持原样。
- 运行中调试记录的停止按钮可以作为调用方构造的 `actions_html` 传入，但调试 ID 和返回地址必须在调用方先转义。
- 后续继续优先选择未脏页面的小范围改动；真实调试启动、安装、下载和复杂 JS 状态流程暂缓。

## 下一阶段候选

- 阶段 29：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估依赖安装日志页头部卡，但避免真实 pip 安装。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/scripts/debug-log/<id>`、`/backup`、备份导入完成页、`/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 29：依赖安装日志头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型日志页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换依赖安装日志页头部卡，不触碰真实 pip 安装、日志轮询和 API 逻辑。

已完成：

- 更新 `fls_manager/routes/deps.py`：
  - 导入 `page_header_card()`。
  - 将 `/deps/install-log/<id>` 状态头部接入头部卡。
  - 保留安装中/已结束状态、日志文件、返回依赖管理和刷新依赖入口。
  - 保留实时日志 `<pre id="log">` 和 `loadLog()` 自动刷新逻辑。
  - 保留包名、日志文件、安装 ID 和返回地址的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/deps/install-log/<id>` 不存在记录时的头部卡和日志 shell 渲染。
  - 覆盖 `/deps/install-log/<id>` 运行中假记录的头部卡渲染、动态字段转义和实时日志主体保留。
  - 测试直接写入假 `DEPS_RUNNING` 记录，不提交 `/deps/install`，避免真实 pip 安装。
- 更新 `DEVELOPMENT.md`：
  - 将 `/deps/install-log/<id>` 依赖安装日志头部卡纳入路由组件覆盖。
  - 增加阶段 29 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，34 tests OK。
- `python -B -m compileall fls_manager/routes/deps.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，125 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证依赖安装日志页初始渲染和静态日志 shell，不启动真实 pip 安装进程，也不通过真实浏览器执行自动刷新脚本。

组件策略结论：

- `page_header_card()` 适合依赖安装日志这类短状态头部，实时日志主体和轮询脚本保持原样。
- 带外部安装副作用的路由测试应绕开提交入口，直接构造进程状态假记录。
- 后续继续优先选择未脏页面的小范围改动；真实安装、下载和复杂 JS 状态流程暂缓。

## 下一阶段候选

- 阶段 30：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估依赖刷新完成页头部卡或依赖卸载结果页，但避免真实 pip 卸载。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/deps/install-log/<id>`、`/scripts/debug-log/<id>`、`/backup`、备份导入完成页、`/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 30：依赖刷新完成页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型结果页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换依赖刷新完成页头部卡，不改变依赖检测逻辑和核心依赖结果表格。

已完成：

- 更新 `fls_manager/routes/deps.py`：
  - 将 `/deps/refresh` 刷新完成提示接入 `page_header_card()`。
  - 保留刷新时间说明。
  - 保留核心依赖检测 `table_card()`、状态 badge 和返回依赖管理入口。
  - 保留依赖名、版本/错误和刷新时间的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/deps/refresh` 头部卡和核心依赖检测表格渲染。
  - 通过 mock `refresh_dependency_cache()` 固定刷新时间、依赖名称和错误消息，避免依赖真实运行环境状态。
  - 断言动态字段 HTML 转义和异常 badge 保留。
- 更新 `DEVELOPMENT.md`：
  - 将 `/deps/refresh` 依赖刷新完成页头部卡纳入路由组件覆盖。
  - 增加阶段 30 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，35 tests OK。
- `python -B -m compileall fls_manager/routes/deps.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，126 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证依赖刷新完成页初始渲染，没有依赖真实包版本或真实浏览器执行。

组件策略结论：

- `page_header_card()` 适合短结果提示，详细检测结果继续由 `table_card()` 承载。
- 环境状态类页面测试应 mock 检测函数，避免本机依赖差异导致测试不稳定。
- 后续继续优先选择未脏页面的小范围改动；真实安装、卸载、下载和复杂 JS 状态流程暂缓。

## 下一阶段候选

- 阶段 31：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估依赖卸载结果页，但测试必须 mock `pip_cmd()`，避免真实 pip 卸载。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/deps/refresh`、`/deps/install-log/<id>`、`/scripts/debug-log/<id>`、`/backup`、备份导入完成页、`/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 31：依赖卸载结果页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和小型结果页。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换依赖卸载结果页头部卡，不改变 pip 卸载调用、输出展示和错误捕获逻辑。

已完成：

- 更新 `fls_manager/routes/deps.py`：
  - 将 `/deps/uninstall` 卸载结果提示接入 `page_header_card()`。
  - 保留返回依赖管理入口。
  - 保留卸载输出 `<pre class="log">` 和输出内容 HTML 转义。
  - 未改动依赖名为空时的既有纯文本 400 响应。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/deps/uninstall` 卸载结果页头部卡和日志输出渲染。
  - 通过 mock `pip_cmd()` 避免真实 pip 卸载。
  - 断言传给 `pip_cmd()` 的卸载参数、返回入口和输出 HTML 转义。
- 更新 `DEVELOPMENT.md`：
  - 将 `/deps/uninstall` 依赖卸载结果页头部卡纳入路由组件覆盖。
  - 增加阶段 31 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，36 tests OK。
- `python -B -m compileall fls_manager/routes/deps.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，127 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证依赖卸载结果页初始渲染，没有执行真实 pip 卸载。

组件策略结论：

- `page_header_card()` 适合短结果页头部，命令输出仍保留在独立日志块中。
- 有破坏性或外部环境副作用的路由测试必须 mock 执行出口，只验证渲染和参数。
- 早期参数错误分支仍保持纯文本响应，避免一次阶段同时改变响应形态。

## 下一阶段候选

- 阶段 32：继续查找未脏页面中的纯文本提示卡或小型头部卡；可评估运行环境安装错误/跳转前结果页，但避免真实系统包安装。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/deps/uninstall`、`/deps/refresh`、`/deps/install-log/<id>`、`/scripts/debug-log/<id>`、`/backup`、备份导入完成页、`/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。

## 阶段 32：任务变量导入页头部和表格卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和 GET 渲染无副作用页面。
- 复用已有 `page_header_card()` 和 `table_card()`，不新增组件 API。
- 只替换从任务变量导入到全局变量页的顶部说明和可导入变量表格，不改变导入提交逻辑。

已完成：

- 更新 `fls_manager/routes/env/actions.py`：
  - 导入 `page_header_card()` 和 `table_card()`。
  - 将 `/env/import` 顶部说明接入头部卡。
  - 保留“允许覆盖已有全局变量”复选框。
  - 将可导入变量表格接入 `table_card()`。
  - 保留底部“导入所选变量”和返回入口。
  - 保留任务名、变量名、变量值和导入状态的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/env/import` 头部卡和可导入变量表格渲染。
  - 使用临时 `FLS_BASE_DIR` 写入测试任务和全局变量数据。
  - 断言覆盖/新增状态 badge、覆盖复选框、提交按钮和动态字段转义。
- 更新 `DEVELOPMENT.md`：
  - 将 `/env/import` 任务变量导入页头部卡和表格卡纳入路由组件覆盖。
  - 增加阶段 32 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，37 tests OK。
- `python -B -m compileall fls_manager/routes/env/actions.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，128 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证任务变量导入页 GET 初始渲染，没有提交导入表单。
- 运行环境安装入口当前没有独立 HTML 结果页，安装失败会写日志并跳转到已组件化的依赖安装日志页，因此本阶段改选 `/env/import`。

组件策略结论：

- `page_header_card()` 可以承载说明文字中的轻量复选框，但提交按钮仍应留在原表单底部。
- `table_card()` 适合带复选框列和 badge 的简单服务端表格，行内容继续由领域 helper 生成。
- 后续继续优先选择未脏页面的小范围改动；真实提交、安装、卸载、下载和复杂 JS 状态流程暂缓。

## 阶段 33：全局变量表单页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和 GET 渲染无副作用页面。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换全局变量查看全部、新增和编辑页面的说明头部，不改变保存逻辑和 POST 校验纯文本响应。

已完成：

- 更新 `fls_manager/routes/env/pages.py`：
  - 将 `/env/view` 顶部说明接入 `page_header_card()`。
  - 将 `/env/new` 新增变量说明接入 `page_header_card()`。
  - 将 `/env/edit/<key>` 编辑变量说明接入 `page_header_card()`。
  - 表单字段、textarea、保存/返回按钮继续保留原有普通卡片结构。
  - 保留全文变量内容、变量名和变量值的 HTML 转义。
  - 保留 `/env/new`、`/env/edit/<key>` POST 空变量名纯文本 400 响应。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/env/view` 头部卡、textarea 内容转义和保存/返回入口。
  - 覆盖 `/env/new` 头部卡、表单字段、保存/返回入口和空变量名校验响应。
  - 覆盖 `/env/edit/<key>` 头部卡、动态变量名/变量值转义和保存/返回入口。
- 更新 `DEVELOPMENT.md`：
  - 将 `/env/view`、`/env/new`、`/env/edit/<key>` 全局变量页面头部卡纳入路由组件覆盖。
  - 增加阶段 33 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，40 tests OK。
- `python -B -m compileall fls_manager/routes/env/pages.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，131 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证全局变量表单页 GET 初始渲染和 `/env/new` 空变量名校验，没有提交真实保存成功流程。

组件策略结论：

- `page_header_card()` 适合承载表单页标题和轻量说明；具体输入控件仍留在独立表单卡片，避免组件承担过多表单布局职责。
- 对仍返回纯文本的历史校验分支，当前阶段保持响应形态不变，避免扩大兼容影响。
- 后续继续优先选择未脏页面的小范围头部卡或提示卡；复杂表单整卡抽取可等模板化方案明确后再处理。

## 阶段 34：代理表单页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和 GET 渲染无副作用页面。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换代理新增和编辑页面的说明头部，不改变保存逻辑、实时测试 JS 和代理质量检测流程。

已完成：

- 更新 `fls_manager/routes/proxy/_common.py`：
  - 导入 `page_header_card()`。
  - 将 `/proxy/new` 顶部说明接入头部卡。
  - 将 `/proxy/edit/<id>` 顶部说明接入头部卡，并转义当前代理名。
  - 保留代理字段卡、自定义质量检测地址、保存/测试/质量检测按钮和返回入口。
  - 保留实时测试/质量检测结果卡与前端 JS。
  - 保留代理名称、Host、用户名、密码、GitHub 代理 URL 等字段的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/proxy/new` 头部卡、表单字段、实时检测壳和返回入口。
  - 覆盖 `/proxy/edit/<id>` 头部卡、动态代理名/字段值转义、选中类型和实时检测壳。
- 更新 `DEVELOPMENT.md`：
  - 将 `/proxy/new`、`/proxy/edit/<id>` 代理表单头部卡纳入路由组件覆盖。
  - 增加阶段 34 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，42 tests OK。
- `python -B -m compileall fls_manager/routes/proxy/_common.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，133 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证代理表单页 GET 初始渲染，没有提交保存代理或触发真实代理检测请求。

组件策略结论：

- `page_header_card()` 适合承载代理表单的标题和上下文说明；字段卡、质量检测配置和实时结果卡仍由页面自身管理。
- 对带 JS 实时结果的页面，本阶段只移动静态头部，避免改动 `innerHTML` 状态更新和请求流程。
- 后续继续优先选择未脏页面的小范围头部卡或纯文本提示卡；涉及真实网络检测或复杂表单提交的流程保持原状。

## 阶段 35：脚本文件表单页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和 GET 渲染无副作用页面。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换脚本新建、查看/编辑和改名页面的说明头部，不改变文件创建、保存和改名流程。

已完成：

- 更新 `fls_manager/routes/scripts/files.py`：
  - 导入 `page_header_card()`。
  - 将 `/pull/new` 顶部说明接入头部卡。
  - 将 `/scripts/view` 文件查看/编辑头部接入头部卡，并保留保存文件、调试运行、改名和返回按钮。
  - 将 `/scripts/rename` 顶部说明接入头部卡。
  - 保留 CodeMirror textarea、文件内容、输入字段和 `message_card()` 提示卡。
  - 保留脚本路径、文件名和文件内容的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/pull/new` 头部卡和空操作提示卡。
  - 覆盖 `/scripts/view` 头部卡、操作按钮、CodeMirror textarea、文件内容转义和提示卡。
  - 覆盖 `/scripts/rename` 头部卡、动态路径/文件名转义和提示卡。
- 更新 `DEVELOPMENT.md`：
  - 将 `/pull/new`、`/scripts/view`、`/scripts/rename` 脚本文件表单页头部卡纳入路由组件覆盖。
  - 增加阶段 35 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，44 tests OK。
- `python -B -m compileall fls_manager/routes/scripts/files.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，135 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证脚本文件表单页 GET 初始渲染，没有提交创建、保存或改名操作。

组件策略结论：

- `page_header_card()` 可以承载表单页标题、路径说明和少量操作按钮；编辑器主体和消息卡继续由页面局部管理。
- 文件内容和路径仍必须在路由层传入组件前明确转义；组件只转义标题，不转义 `help_html` 和 `actions_html`。
- 后续继续优先选择未脏页面的小范围头部卡或纯文本提示卡；涉及真实文件写入的 POST 流程保持原状。

## 阶段 36：配置脚本类型表格卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和稳定表格结构。
- 复用已有 `table_card()`，不新增组件 API。
- 只替换配置页“task 可执行脚本类型”表格外壳，不改变配置保存逻辑、安全验证 JS 和表单字段。

已完成：

- 更新 `fls_manager/routes/config/page.py`：
  - 导入 `table_card()`。
  - 将 `/config` 页面“task 可执行脚本类型”表格接入 `table_card()`。
  - 保留脚本类型行内 checkbox 名称、值和选中状态。
  - 保留登录配置、安全验证、在线脚本源、日志清理、任务运行控制和保存按钮原有结构。
  - 保留 `flsToggleSecurityBox()` 安全验证显示切换脚本。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/config` 脚本类型表格卡标题、表头和 Python 行。
  - 使用临时 `FLS_BASE_DIR` 写入测试配置，断言启用/未启用 checkbox 状态。
  - 断言保存配置按钮和安全验证 JS 保留。
- 更新 `DEVELOPMENT.md`：
  - 将 `/config` 脚本类型表格卡纳入路由组件覆盖。
  - 增加阶段 36 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，45 tests OK。
- `python -B -m compileall fls_manager/routes/config/page.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，136 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证配置页 GET 初始渲染，没有提交保存配置表单。

组件策略结论：

- `table_card()` 适合承载表单内的稳定 checkbox 表格；行 HTML 继续由配置页生成，避免组件感知表单语义。
- 配置页包含安全验证嵌套卡和 JS 切换状态，本阶段只移动最底部稳定表格，避免扩大影响面。
- 后续继续优先选择未脏页面的小范围表格卡、头部卡或纯文本提示卡；复杂配置表单整体组件化暂缓。

## 阶段 37：脚本拉取导入表单页头部卡接入

状态：已完成

目标：

- 继续低风险 UI 组件抽取，优先未脏文件和 GET 渲染无副作用页面。
- 复用已有 `page_header_card()`，不新增组件 API。
- 只替换脚本拉取和脚本导入表单页的说明头部，不改变拉取、导入、代理选择和结果消息逻辑。

已完成：

- 更新 `fls_manager/routes/scripts/pull.py`：
  - 导入 `page_header_card()`。
  - 将 `/pull/fetch` 顶部说明接入头部卡。
  - 将 `/pull/import` 顶部说明接入头部卡。
  - 保留拉取类型、URL、保存名、代理选择、文件上传和保存路径字段。
  - 保留开始拉取、开始导入、返回脚本管理入口。
  - 保留 `pull_result_card()` 和 POST 成功/失败结果消息渲染。
  - 保留当前目录的 HTML 转义。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/pull/fetch` GET 头部卡、表单字段、返回入口、空结果提示和目录转义。
  - 覆盖 `/pull/import` GET 头部卡、multipart 表单、文件/保存名字段、返回入口、空结果提示和目录转义。
- 更新 `DEVELOPMENT.md`：
  - 将 `/pull/fetch`、`/pull/import` 表单头部卡纳入路由组件覆盖。
  - 增加阶段 37 开发日志。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，47 tests OK。
- `python -B -m compileall fls_manager/routes/scripts/pull.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，138 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触碰任务列表、日志、认证/API、`ui/tables.py` 等长期阶段外未提交业务修改。
- 本阶段只验证脚本拉取/导入表单页 GET 初始渲染，没有触发真实网络拉取、Git 克隆或文件上传导入。

组件策略结论：

- `page_header_card()` 适合承载脚本操作表单页的标题和当前目录说明；具体表单字段与结果消息仍由页面自身管理。
- 对带真实网络、Git 和文件上传副作用的 POST 分支，本阶段只覆盖既有 mock/空输入路径，不扩大运行面。
- 后续继续优先选择未脏页面的小范围头部卡、表格卡或纯文本提示卡；复杂 JS/上传/网络流程保持原状。

## 下一阶段候选

- 阶段 38：继续查找未脏页面中的纯文本提示卡、小型头部卡或稳定表格卡；可评估在线脚本源 JSON、脚本命令示例或其它只读说明块，但避免复杂 JS 状态和 POST 纯文本错误响应形态变化。
- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/pull/fetch`、`/pull/import`、`/config`、`/pull/new`、`/scripts/view`、`/scripts/rename`、`/proxy/new`、`/proxy/edit/<id>`、`/env/view`、`/env/new`、`/env/edit/<key>`、`/env/import`、`/deps/uninstall`、`/deps/refresh`、`/deps/install-log/<id>`、`/scripts/debug-log/<id>`、`/backup`、`/about` 版本失败页、`/online-scripts/install-select/<id>`、`/online-scripts/doc/<id>`、`/online-scripts/install/<id>`、`/online-scripts/log/<id>`、`/`、`/online-scripts`、`/notify`、`/deps`、`/panel/status`。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。
