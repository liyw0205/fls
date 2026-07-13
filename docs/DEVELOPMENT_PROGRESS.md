# FLS 开发进度

更新时间：2026-07-14

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

## 阶段 19：任务表单返回路径与 retry 回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中任务表单相关的旧实现回退。
- 固化当前任务编辑页的 `back` 返回路径、外部 URL 清洗和新 `retry` 表单结构。
- 防止后续把旧 `retry_count` 表单和固定返回 `/tasks` 的行为重新迁入。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/task/edit/<id>?back=...` 渲染隐藏 `back` 字段，并保留返回按钮链接。
  - 覆盖任务编辑表单渲染 `retry_attempts` 和 `retry_interval_seconds`，并不渲染旧 `retry_count`。
  - 覆盖提交任务编辑时保存当前 `retry` 结构。
  - 覆盖外部 `back` URL 被清洗，合集任务默认回到 `/collections#collection-<id>`。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 19 对任务表单返回路径和 retry 结构的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，13 tests OK。
- `python -B -m unittest discover -s tests`：通过，113 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补路由回归测试和开发文档，没有改动路由实现。

收束结论：

- 旧 `retry_count` 表单和提交后固定返回 `/tasks` 的方向不应继续迁入。
- 任务编辑页需要保留安全清洗后的 `back`，尤其是从合集进入任务编辑后回到对应合集锚点。

## 阶段 20：合集页任务卡片回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中合集页任务卡片相关的旧实现回退。
- 固化合集页任务卡片的长命令折叠、POST 破坏性操作表单和带合集锚点的 `back` 参数。
- 防止后续把合集页任务操作退回 GET 链接或丢失合集锚点。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖合集页长命令渲染为 `fls-collapsible-code` 折叠代码块。
  - 覆盖任务配置、编辑链接携带 `/collections?...#collection-<id>` 的编码后 `back` 参数。
  - 覆盖停止、置顶、取出任务继续使用 POST 表单。
  - 覆盖添加任务到合集和删除合集的表单 action 保留当前查询参数与合集锚点。
  - 覆盖停止、置顶、取出任务、删除合集没有回退成 GET 链接。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 20 对合集页任务卡片行为的回归测试。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows`：通过，7 tests OK。
- `python -B -m unittest discover -s tests`：通过，114 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补合集页渲染回归测试和开发文档，没有改动路由实现。

收束结论：

- 合集页任务卡片不应回退到未折叠长命令和 GET 破坏性链接。
- 合集页任务操作需要保留带锚点的 back 参数，确保操作或编辑后能回到对应合集位置。

## 阶段 21：普通任务列表动作回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中普通任务列表相关的旧实现回退。
- 固化 `/tasks` 页面长命令折叠、批量工具栏、更多菜单和 AJAX 单任务动作。
- 防止后续把普通任务列表回退成 GET 停止/置顶/删除链接，或丢失编辑、配置入口的 `/tasks` 返回参数。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖普通任务列表长命令渲染为 `fls-collapsible-code` 折叠代码块，并保留 `fls-value-preview` 预览。
  - 覆盖批量工具栏和批量按钮继续渲染，并保留 `taskBulkAction('enable')`、`taskBulkAction('delete')` 调用。
  - 覆盖单任务更多菜单继续渲染，并保留复制、置顶、停止等 `taskAjaxAction(...)` 动作。
  - 覆盖编辑和配置链接继续携带 `/tasks` back 参数。
  - 覆盖任务列表没有回退成 `/task/pin/<id>`、`/stop/<id>`、`/task/delete/<id>` 这类 GET 链接。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 21 对普通任务列表当前行为的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，14 tests OK。
- `python -B -m unittest discover -s tests`：通过，115 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补普通任务列表路由回归测试和开发文档，没有改动页面实现。

收束结论：

- 普通任务列表不应回退到未折叠长命令、GET 破坏性链接或丢失 `/tasks` back 参数。
- 当前批量和单任务动作入口应继续走 AJAX POST/API 流程，便于刷新局部任务块并保留 CSRF 边界。

## 阶段 22：日志管理页分组动作回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中日志管理页相关的旧实现回退。
- 固化 `/logs` 页面分组批量选择、分组批量删除、单组删除入口和单文件 POST 删除表单。
- 防止后续把日志删除入口回退成 GET 链接，或丢失日志分组名 HTML 转义。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖日志管理页继续渲染 `log-bulk-toolbar`、分组选框、全选控件和删除选中分组按钮。
  - 覆盖页面脚本继续调用 `/api/logs/groups/delete` 执行分组删除。
  - 覆盖单组删除按钮继续调用 `flsLogsDeleteGroups([this.dataset.group])`。
  - 覆盖日志分组名在 `data-log-group`、`value` 和标题中正确 HTML 转义。
  - 覆盖单文件删除继续使用 POST 表单，且没有回退成 `/logfile/delete/<file>` 的 GET 链接。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 22 对日志管理页当前行为的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，15 tests OK。
- `python -B -m unittest discover -s tests`：通过，116 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补日志管理页路由回归测试和开发文档，没有改动页面实现。

收束结论：

- 日志管理页不应回退到 GET 删除链接或丢失分组批量删除 UI。
- 分组名来自日志内容，页面属性和值与标题都需要继续 HTML 转义。

## 阶段 23：日志文件详情页返回路径回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中日志查看页相关的返回路径和删除入口风险。
- 固化 `/logfile/<filename>` 页面合法站内 `back`、外部 `back` 清洗、日志删除 POST 表单和日志 API 拉取路径。
- 防止后续把日志文件删除入口回退成 GET 链接，或让外部 URL 进入返回按钮和删除表单。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/logfile/live.log?back=/history` 渲染返回按钮到 `/history`。
  - 覆盖日志文件删除表单继续使用 POST，并保留安全 `back=/history`。
  - 覆盖日志详情页前端继续从 `/api/logfile/live.log?lines=1500` 拉取内容。
  - 覆盖外部 `back=https://example.invalid/evil` 被清洗回 `/logs`。
  - 覆盖页面没有回退成 `/logfile/delete/<file>` 的 GET 删除链接。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 23 对日志文件详情页返回路径和 POST 删除入口的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，16 tests OK。
- `python -B -m unittest discover -s tests`：通过，117 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补日志文件详情页路由回归测试和开发文档，没有改动页面实现。

收束结论：

- 日志文件详情页必须继续清洗外部 `back`，并将删除操作限制在 POST 表单。
- 日志查看页的实时拉取 API 路径应继续使用当前日志文件名，不依赖用户可控外部地址。

## 阶段 24：任务日志页历史表格回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中任务运行历史和任务日志详情页相关的旧实现回退。
- 固化 `/log/<task_id>` 页面最近运行历史表格、状态徽标、日志链接、操作入口和安全 `back` 行为。
- 防止后续移除任务运行历史、把停止操作回退成 GET 链接，或让外部 `back` 进入任务日志页操作入口。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖任务日志页展示 `最近运行历史` 表格。
  - 覆盖历史记录状态徽标、来源、说明和日志链接渲染，并对任务名、命令和说明做 HTML 转义。
  - 覆盖运行、停止、配置和返回入口继续携带安全站内 `back=/history`。
  - 覆盖停止任务继续使用 POST 表单，且没有回退成 `/stop/<id>` 的 GET 链接。
  - 覆盖外部 `back=https://example.invalid/evil` 被清洗回 `/tasks`。
  - 覆盖任务日志页前端继续从 `/api/log/<task_id>?lines=1200` 拉取内容。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 24 对任务日志页历史表格和安全 back 行为的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，17 tests OK。
- `python -B -m unittest discover -s tests`：通过，118 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务日志页路由回归测试和开发文档，没有改动页面实现。

收束结论：

- 任务运行历史不应从任务日志页退化移除，历史记录中的用户可控文本必须继续转义。
- 任务日志页的停止操作和返回路径需要继续保留当前安全边界。

## 阶段 25：全局运行历史页筛选回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中全局运行历史页相关的旧实现回退。
- 固化 `/history` 页关键词筛选、状态筛选、历史表格字段、日志链接和 HTML 转义。
- 防止后续移除运行历史入口、弱化筛选能力，或让任务名、命令、说明等历史字段未转义进入页面。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖 `/history?q=...&status=success` 只显示匹配关键词和状态的历史记录。
  - 覆盖关键词输入框保留转义后的查询值。
  - 覆盖状态筛选下拉框保留选中的 `success` 状态。
  - 覆盖全局历史表格继续展示任务、命令、状态徽标、来源、重试、说明和日志链接。
  - 覆盖未匹配的历史记录不会出现在页面中。
  - 覆盖任务名、命令和说明等用户可控历史字段继续 HTML 转义。
- 更新 `DEVELOPMENT.md`：
  - 记录阶段 25 对全局运行历史页筛选和历史字段转义的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，18 tests OK。
- `python -B -m unittest discover -s tests`：通过，119 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补全局运行历史页路由回归测试和开发文档，没有改动页面实现。

收束结论：

- 全局运行历史页不应退化为无筛选或无历史表格的静态列表。
- 历史记录来自运行过程和用户任务配置，所有展示字段都需要继续 HTML 转义。

## 阶段 26：任务运行历史数据模型回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中任务运行历史数据模型相关的旧实现回退。
- 固化 `task_history.json` 读取过滤、按任务筛选、按 ID 更新、新记录置顶和历史上限裁剪行为。
- 防止后续移除任务运行历史或让坏数据破坏历史页、任务日志页和仪表盘历史摘要。

已完成：

- 扩展 `tests/test_schema_migration.py`：
  - 覆盖 `load_task_history()` 过滤非对象历史记录。
  - 覆盖 `task_history_for_task()` 按 `task_id` 筛选并遵守 limit。
  - 覆盖 `update_task_history()` 按历史 ID 更新并写回文件。
  - 覆盖空 ID 和不存在 ID 更新返回 `False`。
  - 覆盖 `add_task_history()` 把新记录插入顶部，并通过 `TASK_HISTORY_LIMIT` 裁剪旧记录。
- 更新 `docs/DATA_SCHEMA.md`：
  - 新增 `data/task_history.json` 字段、读取规则和写入裁剪规则。
  - 通用规则补充 `task_history.json` 由 `models.py` 规范化读取。
- 更新 `DEVELOPMENT.md`：
  - 记录 `data/task_history.json` 为主要数据文件。
  - 记录阶段 26 对任务运行历史数据模型和 schema 文档的回归测试。

验证记录：

- `python -B -m unittest tests.test_schema_migration`：通过，6 tests OK。
- `python -B -m unittest discover -s tests`：通过，120 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务运行历史数据模型回归测试和文档，没有改动模型实现。

收束结论：

- `task_history.json` 是运行历史、任务日志页和仪表盘历史摘要的共同数据源，不应被后续旧实现回退移除。
- 历史文件读取需要容忍坏行，写入需要保持最新记录在前并控制文件规模。

## 阶段 27：仪表盘历史摘要回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中任务运行历史展示相关的旧实现回退。
- 固化仪表盘 `/` 的最近运行和最近异常摘要，避免后续改动移除任务历史入口。
- 覆盖历史摘要中的状态徽标、说明、日志链接和用户可控文本 HTML 转义。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖仪表盘最近运行区继续展示 `task_history.json` 中的成功历史。
  - 覆盖仪表盘最近异常区继续展示失败历史。
  - 覆盖成功/失败状态徽标、说明列和日志文件详情链接。
  - 覆盖任务名和历史消息中的 HTML 特殊字符被转义。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 27。
  - 记录阶段 27 对仪表盘历史摘要当前行为的回归测试。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，19 tests OK。
- `python -B -m unittest discover -s tests`：通过，121 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补仪表盘历史摘要回归测试，没有改动仪表盘运行时代码。

收束结论：

- 仪表盘应继续把 `task_history.json` 的最近运行和异常历史作为首页可见摘要。
- 历史摘要中的任务名、说明和日志入口属于用户可见数据，必须持续保持转义与安全站内链接。

## 阶段 28：批量任务 API 状态字段

状态：已完成

目标：

- 继续收束原长期脏文件中批量任务操作相关的 API 行为。
- 让 `/api/task/bulk-action` 在保留 `ok` / `msg` 兼容字段的同时，返回脚本客户端和前端可直接使用的结构化状态字段。
- 覆盖批量启用/禁用、取出合集、删除、运行和停止时的计数、跳过和失败明细。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 新增 `_bulk_payload()`，统一批量成功响应的 `action`、`count` 和扩展字段。
  - 批量启用/禁用、取出合集返回 `updated_count`。
  - 批量删除返回 `deleted_count`。
  - 批量运行返回 `submitted_count`、`failed_count` 和 `failures`。
  - 批量停止返回 `stopped_count`、`skipped_count`、`failed_count` 和 `failures`。
  - 未知操作、空选择和任务缺失错误也返回 `action` / `count`，任务缺失时额外返回 `missing_count` / `missing_ids`。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖重复任务 ID 去重后的 `count`。
  - 覆盖批量禁用、取出合集、删除的结构化计数字段。
  - 覆盖批量运行的提交成功数、失败数和失败明细。
  - 覆盖批量停止的结束数、跳过数、失败数和失败明细。
  - 覆盖空选择错误仍返回 `action` 和 `count`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 28。
  - 记录批量任务 API 响应字段约定和阶段 28 开发日志。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows`：通过，8 tests OK。
- `python -B -m unittest discover -s tests`：通过，122 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只扩展批量任务 API 的 JSON 字段，没有调整任务列表或合集页前端交互。

收束结论：

- 批量任务 API 客户端不应解析中文 `msg` 来判断局部成功、跳过或失败。
- 后续改动需要继续保留 `ok` / `msg` 兼容字段，并维护新增结构化字段的稳定性。

## 阶段 29：任务启动失败历史收尾回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中任务运行历史相关的旧实现回退。
- 固化 `_start_task_worker()` 在启动进程失败时的历史收尾行为。
- 防止任务启动失败后卡在 `starting`、缺失 `start_failed` 历史或遗留打开的日志句柄。

已完成：

- 扩展 `tests/test_task_runtime.py`：
  - 模拟 `subprocess.Popen()` 抛出 `OSError`。
  - 覆盖 `_start_task_worker()` 把历史记录更新为 `start_failed`。
  - 覆盖启动失败消息、结束时间和日志文件路径写入 `task_history.json`。
  - 覆盖启动失败日志写入任务日志文件。
  - 覆盖日志句柄关闭、`RUNNING` 运行态清理和失败重试调度入口调用。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 29。
  - 记录阶段 29 对任务启动失败历史收尾的回归测试。

验证记录：

- `python -B -m unittest tests.test_task_runtime`：通过，20 tests OK。
- `python -B -m unittest discover -s tests`：通过，123 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务启动失败历史收尾回归测试，没有改动任务运行时代码。

收束结论：

- 任务启动失败应作为完整运行历史收尾，不应停留在 `starting`。
- 启动失败路径需要同时维护历史记录、任务日志、日志句柄和 `RUNNING` 状态清理。

## 阶段 30：任务失败不重试通知边界回归测试

状态：已完成

目标：

- 继续收束原长期脏文件中任务运行失败、历史记录和通知出口相关的旧实现回退。
- 固化 `task_finish_watcher()` 在任务失败且未计划重试时的收尾行为。
- 防止失败任务跳过通知、缺失 `failed` 历史、丢失退出码或仍被误判为待重试。

已完成：

- 扩展 `tests/test_task_runtime.py`：
  - 模拟任务进程返回非零退出码且 `schedule_task_retry()` 不计划重试。
  - 覆盖 `task_history.json` 写入 `failed` 状态、退出码、结束时间、日志路径和失败消息。
  - 覆盖失败任务仍使用用户脚本日志内容发送通知。
  - 覆盖 `RUNNING` 运行态清理，以及任务日志继续记录退出码和通知结果。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 30。
  - 记录阶段 30 对任务失败不重试通知边界的回归测试。

验证记录：

- `python -B -m unittest tests.test_task_runtime`：通过，21 tests OK。
- `python -B -m unittest discover -s tests`：通过，124 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务失败不重试通知边界回归测试，没有改动任务运行时代码或通知实现。

收束结论：

- 失败任务只有在已经计划重试时才应跳过完成通知。
- 未计划重试的失败任务需要完整收尾历史和日志，并继续按任务通知配置发送结果。

## 阶段 31：批量 API 前端消费回归测试

状态：已完成

目标：

- 继续收束阶段 28 批量任务 API 结构化字段的前端消费闭环。
- 让普通任务列表和合集页批量操作优先使用结构化计数字段生成提示，而不是只展示后端 `msg`。
- 防止后续前端回退到解析或透传中文消息，丢失局部成功、跳过和失败明细。

已完成：

- 更新 `fls_manager/static/fls.js`：
  - 新增 `flsBulkActionMessage()`，根据 `action`、`count`、`updated_count`、`deleted_count`、`submitted_count`、`stopped_count`、`skipped_count`、`failed_count` 和 `failures` 生成批量操作提示。
  - 新增计数和失败明细辅助函数，失败明细最多展示前三条并保留总失败数。
- 更新 `fls_manager/ui/tables.py`：
  - 普通任务列表批量操作成功/失败提示改用 `flsBulkActionMessage()`。
- 更新 `fls_manager/routes/tasks/collections.py`：
  - 合集页任务批量操作成功/失败提示改用 `flsBulkActionMessage()`。
- 更新 `fls_manager/ui/layout.py`：
  - 静态资源版本提升到 `20260704-31`，确保浏览器加载新的 `fls.js`。
- 扩展测试：
  - `tests/test_ui_route_components.py` 覆盖静态 JS 中存在结构化批量消息函数和普通任务页调用。
  - `tests/test_bulk_workflows.py` 覆盖合集页批量入口调用结构化消息函数。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 31。
  - 记录阶段 31 对批量 API 前端消费的接入。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，20 tests OK。
- `python -B -m unittest tests.test_bulk_workflows`：通过，8 tests OK。
- `node --check fls_manager/static/fls.js`：通过。
- `python -B -m unittest discover -s tests`：通过，125 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只接入前端提示格式化，没有改变批量 API 的字段或操作语义。

收束结论：

- 批量任务 API 的结构化字段现在已有普通任务页和合集页前端消费路径。
- 后续如果新增批量动作，应同步扩展 `flsBulkActionMessage()` 和对应 API 字段测试。

## 阶段 32：任务表单错误提示渲染

状态：已完成

目标：

- 继续收束原长期脏文件中错误提示渲染相关的 UI 边界。
- 让新建/编辑任务校验失败时仍显示完整任务表单，而不是返回纯文本错误。
- 保留用户已填写内容和安全 `back`，同时确保用户输入继续 HTML 转义。

已完成：

- 更新 `fls_manager/routes/tasks/pages.py`：
  - 新增 `_task_from_post()`，统一从 POST 表单构建任务草稿。
  - 新增 `_task_form_error()`，使用 `message_card()` 渲染错误提示并返回任务表单。
  - 新建任务和编辑任务的必填、Cron、合集校验失败改为返回错误卡片页面，HTTP 状态码仍为 400。
  - 编辑任务校验失败不会写回 `tasks.json`。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖新建任务命令为空时渲染错误卡片、保留安全 `back` 和转义任务名。
  - 覆盖编辑任务 Cron 错误时渲染错误卡片、外部 `back` 清洗回 `/tasks`、保留表单值且不保存。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 32。
  - 记录阶段 32 对任务表单错误提示渲染的改进。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，22 tests OK。
- `python -B -m unittest discover -s tests`：通过，127 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只改任务新建/编辑表单的校验错误渲染，没有调整任务保存字段语义。

收束结论：

- 任务表单错误不应脱离后台布局直接返回纯文本。
- 校验失败路径应保留用户输入、保留安全返回路径，并继续对用户可控字段做 HTML 转义。

## 阶段 33：脚本操作失败提示渲染

状态：已完成

目标：

- 继续收束原长期脏文件中脚本管理相关的 UI 边界。
- 让脚本新建、编辑保存和改名失败时使用明确的错误提示样式。
- 失败后保留用户刚提交的名称或内容，并继续转义异常文本和表单内容。

已完成：

- 更新 `fls_manager/routes/scripts/files.py`：
  - `/pull/new` 失败时使用 `message_card(..., "error", strong=True)`，并保留新建类型、名称和文件内容。
  - `/scripts/view` 保存成功时使用成功卡片，保存失败时使用错误卡片并保留本次提交的编辑内容。
  - `/scripts/rename` 失败时使用错误卡片，并在输入框保留用户提交的新名称。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖脚本新建失败时错误卡片、异常文本转义、名称/内容回填和不落盘。
  - 覆盖脚本编辑保存失败时错误卡片、异常文本转义、保留本次提交内容且原文件不变。
  - 覆盖脚本改名失败时错误卡片、异常文本转义、保留提交的新名称且原文件不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 33。
  - 记录阶段 33 对脚本操作失败提示渲染的改进。

验证记录：

- `python -B -m unittest tests.test_ui_route_components`：通过，25 tests OK。
- `python -B -m unittest discover -s tests`：通过，130 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有改变脚本路径校验、保存路径、改名路径或删除下载语义，只调整失败提示和失败回填。

收束结论：

- 脚本操作失败路径现在和其它表单页一致，使用明显错误卡片而不是普通灰色提示。
- 写入或改名异常不会让用户刚输入的内容从页面上丢失。

## 阶段 34：合集加入任务兼容边界

状态：已完成

目标：

- 继续收束原长期脏文件中合集任务操作相关的兼容边界。
- 固化合集“放入任务”对多选 `task_ids` 和旧单选 `task_id` 的兼容行为。
- 保证重复选择会去重，混入不存在任务时不会部分写入。

已完成：

- 更新 `fls_manager/routes/tasks/collections.py`：
  - 新增 `_collection_task_ids_from_form()`，集中解析加入合集表单中的任务 ID。
  - 同时接受 `task_ids` 多选字段和旧版 `task_id` 单选字段。
  - 去除空值和重复 ID，保持空选择重定向行为。
  - 保留所有选择 ID 必须存在的校验，缺失任务时返回 404 且不进入写入循环。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖 `task_ids` 与 `task_id` 同时存在时去重并正确加入合集。
  - 覆盖仅提交旧版 `task_id` 字段时仍能加入合集。
  - 覆盖选择中包含不存在任务时返回 404，且已有任务不会被部分加入合集。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 34。
  - 记录阶段 34 对合集加入任务兼容边界的固化。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows`：通过，10 tests OK。
- `python -B -m unittest discover -s tests`：通过，132 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有改变合集批量操作 API、合集页批量工具栏或任务排序语义，只固化加入任务表单解析和异常边界。

收束结论：

- 合集加入任务入口现在对旧表单和新多选表单都有明确测试约束。
- 混入无效任务 ID 时不会出现一部分任务已被加入、一部分失败的状态。

## 阶段 35：单项任务删除 API 缺失边界

状态：已完成

目标：

- 继续收束原长期脏文件中任务操作 API 相关的边界风险。
- 让单项任务删除 API 与复制、置顶、切换和批量任务 API 一样，对不存在任务返回明确失败。
- 保证缺失任务删除请求不会停止任务、写回任务文件或重载调度器。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - `/api/task/action/delete/<id>` 先读取任务列表并确认目标存在。
  - 目标不存在时返回 `404` 和 `{"ok": false, "msg": "任务不存在"}`。
  - 目标存在时复用已读取任务列表删除目标，避免删除路径重复读取。
  - 保持成功删除时停止任务、保存任务列表、重载调度器和返回 `{"ok": true, "msg": "已删除"}` 的原有行为。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖单项删除存在任务时会调用 `stop_task_now()`、删除目标并重载调度器。
  - 覆盖单项删除不存在任务时返回 404，且不调用 `stop_task_now()`、`save_tasks()`、`reload_scheduler()`，任务文件保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 35。
  - 在 API 开发注意中记录单项删除缺失任务的 404 和无副作用约定。
  - 增加阶段 35 开发日志。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows`：通过，12 tests OK。
- `python -B -m unittest discover -s tests`：通过，134 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只改单项任务删除 API 的缺失任务边界，没有调整任务列表前端、批量任务 API、任务停止语义或调度器行为。

收束结论：

- 单项删除任务 API 不再对不存在任务返回“已删除”的成功假象。
- 删除缺失任务不会产生无意义的文件写入或调度器重载。

## 阶段 36：单项任务运行停止缺失边界

状态：已完成

目标：

- 继续收束任务操作 API 的缺失任务状态边界。
- 让单项 `run` / `stop` API 与其它单项任务操作及批量 API 一样，对不存在任务返回明确 404。
- 保留“任务存在但未运行”这个 stop 业务失败场景的原有 200 + `ok:false` 行为。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 新增 `_task_action_result()`，统一把 `{"ok": false, "msg": "任务不存在"}` 映射为 HTTP 404。
  - 新增 `_task_exists()`，用于 stop 前区分缺失任务和已存在但未运行任务。
  - `/api/task/action/run/<id>` 在 `run_task_now()` 返回“任务不存在”时返回 404。
  - `/api/task/action/stop/<id>` 对缺失任务先返回 404，且不调用 `stop_task_now()`。
  - `/api/task/action/stop/<id>` 对已存在但未运行任务继续返回 200 + `{"ok": false, "msg": "任务未运行"}`。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖 run 缺失任务返回 404 且任务文件不变。
  - 覆盖 stop 缺失任务返回 404 且不调用停止逻辑。
  - 覆盖 stop 已存在但未运行保留 200 失败响应。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 36。
  - 在 API 开发注意中记录单项 run/stop 缺失任务边界。
  - 增加阶段 36 开发日志。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows`：通过，15 tests OK。
- `python -B -m unittest discover -s tests`：通过，137 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只改单项 run/stop API 的缺失任务 HTTP 状态边界，没有调整任务运行、停止、批量操作或前端交互语义。

收束结论：

- 单项任务操作 API 对缺失任务的状态码现在更一致：run、stop、delete、copy、toggle、pin 都能返回明确 404。
- 前端仍可把已存在但未运行的 stop 当作普通业务失败处理，不会被误判为资源缺失。

## 阶段 37：批量删除停止失败边界

状态：已完成

目标：

- 继续收束任务批量 API 的局部失败边界。
- 避免批量删除在任务停止失败时仍把任务从 `tasks.json` 删除。
- 保持批量 API 既有 200 + `ok:true` 兼容语义，同时补充结构化失败明细供前端和脚本客户端使用。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 批量删除前逐个调用 `stop_task_now()`。
  - `stop_task_now()` 成功或返回“任务未运行”时，任务允许删除。
  - 其它停止失败场景会保留对应任务，不写入删除集合。
  - 响应新增 `failed_count` 和 `failures`，保留 `deleted_count`。
  - 仅在实际删除了任务时写回任务文件并重载调度器。
- 更新 `fls_manager/static/fls.js`：
  - `flsBulkActionMessage()` 的 delete 分支显示删除失败摘要。
- 扩展 `tests/test_bulk_workflows.py`：
  - 正常批量删除断言 `failed_count=0`、`failures=[]`。
  - 覆盖停止失败时只删除已停止/未运行任务，失败任务留在 `tasks.json`。
- 扩展 `tests/test_ui_route_components.py`：
  - 固化静态 JS 中删除失败提示字段。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 37。
  - 在 API 开发注意中记录批量删除停止失败边界。
  - 增加阶段 37 开发日志。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows tests.test_ui_route_components.UiRouteComponentTests.test_static_js_formats_structured_bulk_action_messages`：通过，17 tests OK。
- `python -B -m compileall fls_manager/routes/api.py fls_manager/static/fls.js tests/test_bulk_workflows.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，143 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有改变批量删除的 HTTP 成功状态兼容语义，局部失败仍通过结构化字段表达。
- 本阶段没有触发真实任务进程，只通过 mock `stop_task_now()` 验证删除边界。

收束结论：

- 批量删除不再把停止失败的任务从任务配置中移除。
- 前端批量提示可以展示删除失败摘要，避免用户误以为所有任务都已删除。

## 阶段 38：单项删除停止失败边界

状态：已完成

目标：

- 继续收束任务删除 API 的停止失败边界。
- 让单项删除与阶段 37 的批量删除保持一致：任务停止失败时不能从 `tasks.json` 删除。
- 兼容保留的页面 POST 删除入口也应遵守同一边界。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 新增 `_delete_stop_failed()`，集中判断停止结果是否阻止删除。
  - `/api/task/action/delete/<id>` 删除存在任务前检查 `stop_task_now()` 返回值。
  - 停止成功或返回“任务未运行”时继续删除任务。
  - 其它停止失败场景返回 `409` 和 `{"ok": false, "msg": "删除失败：..."}`，不写回任务文件、不重载调度器。
  - 批量删除分支复用同一判断逻辑，减少单项和批量边界漂移。
- 更新 `fls_manager/routes/tasks/actions.py`：
  - 兼容页面 POST `/task/delete/<id>` 先确认任务存在。
  - 停止失败时返回 409 错误文本和安全返回链接，保留任务并避免写入或调度器重载。
  - 成功删除后使用 `get_back_url("/tasks")` 返回，继续清洗 back 参数。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖单项 API 删除停止成功路径。
  - 覆盖“任务未运行”仍允许删除。
  - 覆盖单项 API 停止失败时返回 409、不保存、不重载、任务文件不变。
  - 覆盖兼容页面 POST 删除入口停止失败时同样不保存、不重载、任务文件不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 38。
  - 在 API 开发注意和测试覆盖列表中记录单项删除停止失败边界。
  - 增加阶段 38 开发日志。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows`：通过，19 tests OK。
- `python -B -m compileall fls_manager/routes/api.py fls_manager/routes/tasks/actions.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，146 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只通过 mock `stop_task_now()` 验证停止失败、未运行和成功路径。
- 本阶段没有改变批量删除的 200 + `ok:true` 兼容语义。

收束结论：

- 单项删除和批量删除现在共享同一停止失败删除边界。
- 停止失败不会再导致任务配置被删除，避免运行态任务失去面板侧配置记录。

## 阶段 39：兼容任务切换入口 POST 边界

状态：已完成

目标：

- 继续收束任务页面遗留动作入口的 HTTP 方法边界。
- 避免 `/task/toggle/<id>` 通过 GET 修改任务启用状态。
- 目标任务不存在时不写回任务文件、不重载调度器。

已完成：

- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/task/toggle/<id>` 改为 `methods=["POST"]`，GET 自动返回 405。
  - 切换前记录是否命中目标任务；目标不存在时 `abort(404)`。
  - 目标不存在时不调用 `save_tasks()`，也不调用 `reload_scheduler()`。
  - 成功切换后使用 `get_back_url("/tasks")` 返回，继续清洗 back 参数。
- 扩展 `tests/test_auth_backup.py`：
  - 将 `/task/toggle/t1` 纳入破坏性路由拒绝 GET 的回归测试。
  - 断言 GET 被拒绝后任务 `enabled` 不变。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖兼容页面 POST 切换任务启用状态并按安全 back 返回。
  - 覆盖目标任务不存在时返回 404，且不保存、不重载、任务文件不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 39。
  - 在 API/页面开发注意和测试覆盖列表中记录 `/task/toggle/<id>` POST-only 边界。
  - 增加阶段 39 开发日志。

验证记录：

- `python -B -m unittest tests.test_auth_backup.CsrfSafetyTests.test_destructive_routes_reject_get_requests tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_task_toggle_post_updates_task_and_uses_safe_back tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_task_toggle_missing_task_aborts_without_side_effects`：通过，3 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_auth_backup.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，148 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有改变普通任务列表当前 AJAX API 切换入口，只收紧兼容保留的页面路由。

收束结论：

- 遗留 `/task/toggle/<id>` 不再允许 GET 修改状态。
- 兼容页面切换入口与其它破坏性页面动作保持 POST-only 和缺失任务无副作用约束。

## 阶段 40：兼容任务运行入口 POST 边界

状态：已完成

目标：

- 继续收束任务页面遗留动作入口的 HTTP 方法边界。
- 避免 `/run/<id>` 通过 GET 启动任务。
- 让日志页、合集页和配置页的运行入口都走 POST，并保留安全 back 返回。

已完成：

- 更新本地 Git 提交身份：
  - `user.name=liyw0205`
  - `user.email=2650115317@qq.com`
- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/run/<id>` 改为 `methods=["POST"]`，GET 自动返回 405。
  - 成功运行后继续跳转到任务日志页，并保留清洗后的 `back`。
  - `run_task_now()` 返回“任务不存在”时映射为 404，其它运行失败仍返回 400。
- 更新 `fls_manager/routes/tasks/logs.py`：
  - 任务日志页的“运行”按钮由 GET 链接改为 POST 表单。
- 更新 `fls_manager/routes/tasks/collections.py`：
  - 合集任务卡片的“运行”按钮由 GET 链接改为 POST 表单。
- 更新 `fls_manager/routes/tasks/config_file.py`：
  - 配置编辑页在外层保存表单内使用 `formaction="/run/<id>..."` 和 `formmethod="post"`，避免嵌套表单。
- 扩展 `tests/test_auth_backup.py`：
  - 将 `/run/t1` 纳入破坏性路由拒绝 GET 的回归测试。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖兼容 `/run/<id>` POST 成功提交任务并按安全 back 跳转日志页。
  - 覆盖缺失任务返回 404 且任务文件不变。
  - 覆盖合集任务卡片运行入口使用 POST 表单，且不再渲染 GET 运行链接。
- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖任务配置页使用 `formaction` + POST 运行按钮，不再渲染 GET 运行链接。
  - 覆盖任务日志页使用 POST 运行表单，不再渲染 GET 运行链接。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 40。
  - 在 API/页面开发注意和测试覆盖列表中记录 `/run/<id>` POST-only 边界。
  - 增加阶段 40 开发日志。

验证记录：

- `python -B -m unittest tests.test_auth_backup.CsrfSafetyTests.test_destructive_routes_reject_get_requests tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_run_route_post_submits_task_and_uses_safe_back tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_run_route_missing_task_returns_404_without_write tests.test_bulk_workflows.BulkWorkflowTests.test_collection_task_cards_keep_post_actions_collapsed_command_and_anchor_back tests.test_ui_route_components.UiRouteComponentTests.test_task_config_save_success_renders_message_card tests.test_ui_route_components.UiRouteComponentTests.test_task_log_page_keeps_history_table_actions_and_safe_back`：通过，6 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py fls_manager/routes/tasks/logs.py fls_manager/routes/tasks/config_file.py fls_manager/routes/tasks/collections.py tests/test_auth_backup.py tests/test_bulk_workflows.py tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，150 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只通过 mock `run_task_now()` 验证兼容页面运行入口。
- 本阶段没有改变普通任务列表当前 AJAX API 运行入口，只收紧兼容保留的页面路由和相关页面按钮。

收束结论：

- 遗留 `/run/<id>` 不再允许 GET 启动任务。
- 日志页、合集页和配置页已经统一改为 POST 运行入口，继续保留安全 `back` 返回。

## 阶段 41：兼容任务停止入口缺失边界

状态：已完成

目标：

- 继续收束任务页面遗留动作入口的 HTTP 方法和缺失任务边界。
- 保证 `/stop/<id>` 对不存在任务返回 404，且不调用停止逻辑。
- 保留已存在但未运行任务的重定向兼容行为。

已完成：

- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/stop/<id>` POST 前先检查任务是否存在。
  - 目标任务不存在时 `abort(404)`。
  - 缺失任务不调用 `stop_task_now()`。
  - 目标任务存在时仍调用 `stop_task_now()` 并按安全 `back` 重定向。
- 扩展 `tests/test_auth_backup.py`：
  - 将 `/stop/t1` 纳入破坏性路由拒绝 GET 的回归测试，确认 GET 返回 405。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖已存在但未运行任务通过兼容 `/stop/<id>` POST 仍重定向返回。
  - 覆盖缺失任务返回 404，且不调用 `stop_task_now()`，任务文件不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 41。
  - 在 API/页面开发注意和测试覆盖列表中记录 `/stop/<id>` 缺失任务边界。
  - 增加阶段 41 开发日志。

验证记录：

- `python -B -m unittest tests.test_auth_backup.CsrfSafetyTests.test_destructive_routes_reject_get_requests tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_stop_route_existing_task_redirects_when_not_running tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_stop_route_missing_task_aborts_without_stop_call`：通过，3 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_auth_backup.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，152 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只通过 mock `stop_task_now()` 验证兼容页面停止入口。
- 本阶段没有改变普通任务列表当前 AJAX API 停止入口，只收紧兼容保留的页面路由。

收束结论：

- 兼容 `/stop/<id>` 页面入口现在与单项停止 API 的缺失任务边界一致。
- 缺失任务不会再被当作停止成功后重定向处理。

## 阶段 42：合集删除写入边界

状态：已完成

目标：

- 继续收束任务合集页面动作的写入边界。
- 删除不存在合集时不写入合集或任务文件。
- 删除空合集时避免无意义写回 `tasks.json`。
- 删除含任务合集时仍清空成员任务归属。

已完成：

- 更新 `fls_manager/routes/tasks/collections.py`：
  - `/collection/delete/<id>` 删除时记录成员任务归属是否实际变更。
  - 删除空合集时只保存 `collections.json`，不调用 `save_tasks()`。
  - 删除含任务合集时继续清空成员任务 `collection_id` 并写回任务文件。
  - 保留缺失合集 `abort(404)` 行为。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖删除空合集时跳过 `save_tasks()`，任务文件保持不变。
  - 覆盖删除含任务合集时清空成员任务归属。
  - 覆盖删除缺失合集返回 404，且不调用 `save_collections()` / `save_tasks()`，文件保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 42。
  - 在页面开发注意和测试覆盖列表中记录合集删除写入边界。
  - 增加阶段 42 开发日志。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_collection_delete_empty_collection_skips_task_write tests.test_bulk_workflows.BulkWorkflowTests.test_collection_delete_clears_member_tasks tests.test_bulk_workflows.BulkWorkflowTests.test_collection_delete_missing_collection_aborts_without_writes`：通过，3 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/collections.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，155 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有改变合集删除的页面交互，只缩小无成员任务时的写入范围。

收束结论：

- 合集删除现在只在确实需要清空任务归属时写 `tasks.json`。
- 缺失合集不会触发任何写回副作用。

## 阶段 43：单任务取出合集写入边界

状态：已完成

目标：

- 继续收束合集相关页面动作的写入边界。
- 目标任务不存在时不写入任务文件。
- 目标任务已经不在合集时避免无意义写回 `tasks.json`。
- 保留已归属合集任务的取出行为。

已完成：

- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/task/collection/clear/<id>` 增加 `changed` 标记。
  - 目标任务不存在时继续 `abort(404)`。
  - 目标任务 `collection_id` 为空时直接按安全 `back` 返回，不调用 `save_tasks()`。
  - 目标任务存在且归属合集时才清空 `collection_id`、更新 `updated_at` 并写回任务文件。
- 扩展 `tests/test_auth_backup.py`：
  - 将 `/task/collection/clear/t1` 纳入破坏性路由拒绝 GET 的回归测试，确认 GET 返回 405。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖已归属合集任务通过兼容入口取出并写回。
  - 覆盖未归属合集任务通过兼容入口返回但不调用 `save_tasks()`。
  - 覆盖缺失任务返回 404，且不调用 `save_tasks()`，任务文件保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 43。
  - 在页面开发注意和测试覆盖列表中记录单任务取出合集写入边界。
  - 增加阶段 43 开发日志。

验证记录：

- `python -B -m unittest tests.test_auth_backup.CsrfSafetyTests.test_destructive_routes_reject_get_requests tests.test_bulk_workflows.BulkWorkflowTests.test_task_collection_clear_existing_member_writes_task_file tests.test_bulk_workflows.BulkWorkflowTests.test_task_collection_clear_unassigned_task_skips_write tests.test_bulk_workflows.BulkWorkflowTests.test_task_collection_clear_missing_task_aborts_without_write`：通过，4 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_auth_backup.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，158 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有改变单任务取出合集的页面交互，只缩小未归属任务时的写入范围。

收束结论：

- 单任务取出合集现在只在实际清空归属时写 `tasks.json`。
- 缺失任务或已无归属的任务不会触发写回副作用。

## 阶段 44：重新生成开发文档

状态：已完成

目标：

- 将根目录 `DEVELOPMENT.md` 从阶段流水型文档重新整理为当前状态版开发文档。
- 保留当前架构、数据模型、鉴权安全、任务执行链路、前端约定、测试策略和开发流程。
- 把历史阶段流水继续留在 `docs/DEVELOPMENT_PROGRESS.md`，让根目录文档更适合下一轮快速接续。

已完成：

- 重写 `DEVELOPMENT.md`：
  - 基线推进到阶段 44。
  - 开头明确历史阶段流水、会话交接和数据 schema 的文档入口。
  - 收敛项目定位、启动入口、目录与模块边界、数据模型、鉴权安全、当前行为边界、任务执行链路、前端约定、测试策略、开发流程、已知约束和后续方向。
  - 明确记录任务 API、兼容页面动作、合集删除、单任务取出合集、日志删除、备份安全和安全 back 返回等当前关键边界。
  - 移除根文档中的冗长阶段开发日志，避免和本进度文档重复。
- 更新 `docs/DEVELOPMENT_PROGRESS.md`：
  - 新增阶段 44 完成块。
  - 将下一阶段候选推进到阶段 45。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 当前阶段推进到阶段 44。
  - 记录本轮文档重生成结果、验证记录、长期 stash 处理约束和下一阶段建议。

验证记录：

- `python -B -m unittest discover -s tests`：通过，158 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只重生成协作文档和交接文档，没有改动运行时代码或测试代码。

收束结论：

- `DEVELOPMENT.md` 现在是当前状态版开发入口，阶段流水继续由 `docs/DEVELOPMENT_PROGRESS.md` 承载。
- 下一轮可以直接从 `docs/SESSION_HANDOFF.md` 和根开发文档接续，不需要在根文档中翻长日志。

## 阶段 45：兼容任务置顶入口边界

状态：已完成

目标：

- 继续低风险收束原长期脏 diff 中尚未覆盖的页面动作兼容边界。
- 固化 `/task/pin/<id>` POST 入口的成功回跳、缺失任务无副作用和置顶上限失败渲染。
- 保持不整包恢复 `stash@{0}`，只做可验证的窄边界。

已完成：

- 只读查看 `stash@{0}` 涉及文件，未执行 `stash pop` 或 `stash apply`。
- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/task/pin/<id>` 超过 5 个置顶任务时不再返回纯文本。
  - 上限失败改为使用 `message_card(..., "error", strong=True, title="置顶失败")` 渲染错误卡片。
  - 错误页保留安全 `back` 返回链接，外部回跳继续由 `get_back_url()` 清洗。
  - 上限失败和缺失任务路径均不写回 `tasks.json`。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖兼容页面 POST 置顶成功，并清洗外部 `back` 回 `/tasks`。
  - 覆盖缺失任务返回 404，且不调用 `save_tasks()`。
  - 覆盖达到 5 个置顶上限时返回 400、渲染错误卡片、保留安全返回链接且不写回。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 45。
  - 在当前行为边界和测试覆盖重点中记录兼容置顶入口边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 当前阶段推进到阶段 45。
  - 记录本阶段验证结果、stash 约束和下一阶段建议。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_task_pin_post_updates_task_and_uses_safe_back tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_task_pin_missing_task_aborts_without_write tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_task_pin_limit_renders_error_card_without_write`：通过，3 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，161 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只验证页面动作和 JSON 写入边界。
- 本阶段没有调整普通任务列表当前 AJAX API 置顶入口，只收紧兼容保留的页面路由失败渲染和边界测试。

收束结论：

- 兼容 `/task/pin/<id>` 页面入口现在有成功、缺失任务和上限失败回归测试。
- 置顶超过上限时不会写回任务文件，并会在后台布局内显示统一错误卡片。

## 阶段 46：兼容任务删除失败提示渲染

状态：已完成

目标：

- 继续低风险收束原长期脏 diff 中尚未覆盖的错误提示渲染边界。
- 让兼容 `/task/delete/<id>` 页面入口在停止失败时使用统一后台错误卡片，而不是返回纯文本。
- 保持删除停止失败的 409 状态、任务保留、无写回和无调度器重载语义不变。

已完成：

- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/task/delete/<id>` 停止失败时复用 `message_card(..., "error", strong=True, title="删除失败")`。
  - 错误页使用现有 `layout()` 渲染，页面标题为“删除任务”。
  - 返回按钮继续使用 `get_back_url("/tasks")` 清洗回跳。
  - 停止失败路径继续不调用 `save_tasks()` 和 `reload_scheduler()`。
- 更新 `tests/test_bulk_workflows.py`：
  - 扩展兼容页面删除停止失败测试，覆盖错误卡片标题、错误色、中文文案、HTML 转义和安全回跳。
  - 保留停止失败不写任务文件、不重载调度器的断言。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 46。
  - 在当前行为边界和测试覆盖重点中记录兼容删除入口错误提示。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 当前阶段推进到阶段 46。
  - 记录本阶段验证结果和下一阶段建议。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_task_delete_stop_failure_keeps_task_without_reload`：通过，1 test OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，161 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只通过 mock `stop_task_now()` 验证兼容页面删除失败路径。
- 本阶段没有调整任务删除 API JSON 响应或普通任务列表 AJAX 删除入口。

收束结论：

- 兼容 `/task/delete/<id>` 页面入口的停止失败现在与阶段 45 的置顶失败一样，使用统一后台错误卡片。
- 删除失败文案保持中文，异常文本继续由 `message_card()` 转义，避免用户可控错误内容直接进入 HTML。

## 阶段 47：兼容任务运行失败提示渲染

状态：已完成

目标：

- 继续低风险收束原长期脏 diff 中尚未覆盖的错误提示渲染边界。
- 让兼容 `/run/<id>` 页面入口在运行失败时使用统一后台错误卡片，而不是返回纯文本。
- 保持缺失任务返回 404、普通运行失败返回 400、成功运行跳转日志页的语义不变。

已完成：

- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/run/<id>` 失败时复用 `message_card(..., "error", strong=True, title="运行失败")`。
  - 错误页使用现有 `layout()` 渲染，页面标题为“运行任务”。
  - 返回按钮继续使用 `get_back_url("/tasks")` 清洗回跳。
  - `run_task_now()` 返回“任务不存在”时仍映射为 404，其它失败仍返回 400。
- 更新 `tests/test_bulk_workflows.py`：
  - 扩展缺失任务测试，覆盖错误卡片标题、错误色、中文文案和外部 `back` 清洗。
  - 新增普通运行失败测试，覆盖 400 状态、错误卡片、HTML 转义和安全返回链接。
  - 保留成功运行跳转日志页的既有测试不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 47。
  - 在当前行为边界和测试覆盖重点中记录兼容运行入口错误提示。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 当前阶段推进到阶段 47。
  - 记录本阶段验证结果和下一阶段建议。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_run_route_missing_task_returns_404_without_write tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_run_route_failure_renders_error_card`：通过，2 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，162 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只通过 mock `run_task_now()` 验证兼容页面运行失败路径。
- 本阶段没有调整任务运行 API JSON 响应或普通任务列表 AJAX 运行入口。

收束结论：

- 兼容 `/run/<id>` 页面入口的失败路径现在与删除/置顶失败一样，使用统一后台错误卡片。
- 运行失败文案保持中文，异常文本继续由 `message_card()` 转义，避免用户可控错误内容直接进入 HTML。

## 阶段 48：兼容任务停止失败提示渲染

状态：已完成

目标：

- 继续低风险收束原长期脏 diff 中尚未覆盖的错误提示渲染边界。
- 让兼容 `/stop/<id>` 页面入口在真实停止失败时使用统一后台错误卡片，而不是静默重定向。
- 保持缺失任务返回 404、不调用停止逻辑，以及“任务未运行”仍重定向的兼容行为不变。

已完成：

- 更新 `fls_manager/routes/tasks/actions.py`：
  - `/stop/<id>` 现在读取 `stop_task_now()` 的返回值。
  - `stop_task_now()` 返回 `False, "任务未运行"` 时仍按兼容行为重定向。
  - 其它停止失败返回 409，并复用 `message_card(..., "error", strong=True, title="停止失败")`。
  - 错误页使用现有 `layout()` 渲染，页面标题为“停止任务”。
  - 返回按钮继续使用 `get_back_url("/tasks")` 清洗回跳。
- 更新 `tests/test_bulk_workflows.py`：
  - 保留已存在但未运行任务通过兼容 `/stop/<id>` POST 仍重定向返回。
  - 新增真实停止失败测试，覆盖 409 状态、错误卡片、HTML 转义、安全返回链接和任务文件不变。
  - 保留缺失任务返回 404 且不调用 `stop_task_now()` 的断言。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 48。
  - 在当前行为边界和测试覆盖重点中记录兼容停止入口错误提示。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 当前阶段推进到阶段 48。
  - 记录本阶段验证结果和下一阶段建议。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_stop_route_existing_task_redirects_when_not_running tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_stop_route_failure_renders_error_card tests.test_bulk_workflows.BulkWorkflowTests.test_legacy_stop_route_missing_task_aborts_without_stop_call`：通过，3 tests OK。
- `python -B -m compileall fls_manager/routes/tasks/actions.py tests/test_bulk_workflows.py`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只通过 mock `stop_task_now()` 验证兼容页面停止失败路径。
- 本阶段没有调整任务停止 API JSON 响应或普通任务列表 AJAX 停止入口。

收束结论：

- 兼容 `/stop/<id>` 页面入口现在区分“任务未运行”和真实停止失败。
- 真实停止失败会返回 409 并显示统一后台错误卡片；未运行任务仍保持原有重定向兼容行为。

## 阶段 49：批量取出合集写入边界

状态：已完成

目标：

- 继续低风险收束任务 API 兼容边界。
- 让 `/api/task/bulk-action` 的 `clear_collection` 只在任务实际有合集归属时写回。
- 保持批量接口 `ok/msg/action/count/updated_count` 结构化响应兼容。

已完成：

- 先补提交阶段 48 未提交改动：`d48bc90 Stage 48 render legacy stop errors`。
- 更新 `fls_manager/routes/api.py`：
  - `clear_collection` 现在只清理 `collection_id` 非空的选中任务。
  - 无实际变更时不调用 `save_tasks()`。
  - `updated_count` 和中文提示文案改为反映实际变更数量。
- 扩展 `tests/test_bulk_workflows.py`：
  - 新增批量取出合集在选中任务均未归属合集时的无写回断言。
  - 覆盖 `updated_count=0`、中文文案、`count` 保持选中数量以及任务文件不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 49。
  - 记录批量取出合集写入边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 49。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_disable_clear_collection_and_delete tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_clear_collection_skips_write_when_no_members`：通过，2 tests OK。
- `python -B -m compileall fls_manager/routes/api.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，164 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只验证批量任务 API 的 JSON 写入边界。
- 本阶段没有调整页面单任务取出合集入口；该入口已在阶段 43 收束。

收束结论：

- 批量取出合集现在与单任务取出合集一样，避免无实际变更时写 `tasks.json`。
- 响应中的 `count` 继续表示请求选中数量，`updated_count` 表示实际清理归属数量。

## 阶段 50：批量启用禁用写入边界

状态：已完成

目标：

- 继续低风险收束任务批量 API 写入边界。
- 让 `/api/task/bulk-action` 的 `enable` / `disable` 只在任务状态实际变化时写回。
- 保持批量接口 `ok/msg/action/count/updated_count` 结构化响应兼容。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 批量启用/禁用现在只更新 `enabled` 状态实际变化的选中任务。
  - 无实际变更时不调用 `save_tasks()` 或 `reload_scheduler()`。
  - `updated_count` 和中文提示文案改为反映实际变更数量。
- 扩展 `tests/test_bulk_workflows.py`：
  - 新增批量禁用已禁用任务时的无写回、无调度器重载断言。
  - 覆盖 `updated_count=0`、中文文案、`count` 保持选中数量以及任务文件不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 50。
  - 记录批量启用/禁用写入边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 50。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_disable_clear_collection_and_delete tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_enable_disable_skips_write_when_no_state_changes tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_clear_collection_skips_write_when_no_members`：通过，3 tests OK。
- `python -B -m compileall fls_manager/routes/api.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，165 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有触发真实任务进程，只验证批量任务 API 的 JSON 写入与调度器重载边界。
- 本阶段没有调整任务列表 AJAX 前端逻辑，现有响应字段保持兼容。

收束结论：

- 批量启用/禁用现在避免无实际状态变化时写 `tasks.json` 和重载调度器。
- 响应中的 `count` 继续表示请求选中数量，`updated_count` 表示实际变更数量。

## 阶段 51：单任务置顶 API 状态字段与边界

状态：已完成

目标：

- 继续低风险收束任务动作 API 边界。
- 让 `/api/task/action/pin/<id>` 成功响应直接返回置顶后的 `pinned` 状态。
- 固化缺失任务和置顶上限失败时不写回任务文件的边界。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 单任务置顶/取消置顶成功响应新增 `pinned` 布尔字段。
  - 保持原有 `ok/msg` 字段兼容。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖置顶上限失败返回 400、不调用 `save_tasks()`、任务文件不变。
  - 覆盖置顶成功返回 `pinned: true`，取消置顶返回 `pinned: false`。
  - 覆盖缺失任务返回 404 且不调用 `save_tasks()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 51。
  - 记录单任务置顶 API 状态字段和边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 51。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_action_pin_returns_state_and_checks_boundaries`：通过，1 test OK。
- `python -B -m compileall fls_manager/routes/api.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，166 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有调整任务列表 AJAX 前端逻辑；新增 `pinned` 字段面向 API 调用方，前端仍按既有局部刷新更新展示。
- 本阶段没有触发真实任务进程。

收束结论：

- 单任务置顶 API 现在可直接从响应判断操作后的置顶状态。
- 缺失任务和置顶上限失败路径继续保持无写回副作用。

## 阶段 52：单任务切换 API 状态字段与边界

状态：已完成

目标：

- 继续低风险收束任务动作 API 边界。
- 让 `/api/task/action/toggle/<id>` 成功响应直接返回切换后的 `enabled` 状态。
- 固化缺失任务时不写回任务文件、不重载调度器的边界。

已完成：

- 更新 `fls_manager/routes/api.py`：
  - 单任务启用/禁用切换成功响应新增 `enabled` 布尔字段。
  - 保持原有 `ok/msg` 字段兼容。
- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖启用任务切换为禁用时返回 `enabled: false`。
  - 覆盖再次切换为启用时返回 `enabled: true`，并重载调度器。
  - 覆盖缺失任务返回 404 且不调用 `save_tasks()` 或 `reload_scheduler()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 52。
  - 记录单任务切换 API 状态字段和边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 52。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_action_toggle_returns_state_and_checks_boundaries`：通过，1 test OK。
- `python -B -m compileall fls_manager/routes/api.py tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，167 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段没有调整任务列表 AJAX 前端逻辑；新增 `enabled` 字段面向 API 调用方，前端仍按既有局部刷新更新展示。
- 本阶段没有触发真实任务进程。

收束结论：

- 单任务切换 API 现在可直接从响应判断操作后的启用状态。
- 缺失任务路径继续保持无写回、无调度器重载副作用。

## 阶段 53：任务配置文件页边界回归测试

状态：已完成

目标：

- 继续低风险收束长期脏 diff 中尚未覆盖的 UI 边界。
- 固化 `/task/config/<id>` 的缺失任务、无配置路径和非法路径行为。
- 确认异常边界不会误写任务配置文件或 `scripts/` 外文件。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖缺失任务 POST `/task/config/<id>` 返回 404，且不创建配置文件。
  - 覆盖任务没有 `config_path` 时渲染提示页，清洗外部 back 为 `/tasks`，并提供编辑任务入口。
  - 覆盖非法 `config_path` 在 POST 保存时返回 400，保留安全 back，且不写出 `scripts/` 外文件。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 53。
  - 在当前行为边界和测试覆盖重点中记录任务配置文件页边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 53。

验证记录：

- `python -B -m unittest tests.test_ui_route_components.UiRouteComponentTests.test_task_config_missing_task_returns_404_without_write tests.test_ui_route_components.UiRouteComponentTests.test_task_config_without_config_path_renders_edit_prompt tests.test_ui_route_components.UiRouteComponentTests.test_task_config_illegal_path_renders_error_without_write`：通过，3 tests OK。
- `python -B -m compileall tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，170 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务配置文件页边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- 任务配置文件页的 404、无配置路径和非法路径边界现在有回归测试固定。
- 保存动作仍只会在任务存在、配置路径非空且安全路径校验通过后写入。

## 阶段 54：任务表单校验无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务表单 UI 边界。
- 固化 `/task/new` Cron 校验失败时不创建任务、不重载调度器。
- 固化 `/task/edit/<id>` 缺失任务时不写任务文件、不重载调度器。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖新建任务 Cron 不合法时返回 400，保留表单与安全 back，不调用 `save_tasks()` 或 `reload_scheduler()`。
  - 覆盖编辑缺失任务时返回 404，不调用 `save_tasks()` 或 `reload_scheduler()`，原任务文件保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 54。
  - 在当前行为边界和测试覆盖重点中记录任务表单校验边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 54。

验证记录：

- `python -B -m unittest tests.test_ui_route_components.UiRouteComponentTests.test_task_new_cron_validation_error_does_not_save_or_reload tests.test_ui_route_components.UiRouteComponentTests.test_task_edit_missing_task_aborts_without_side_effects`：通过，2 tests OK。
- `python -B -m compileall tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，172 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务表单边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- 新建任务 Cron 校验失败和编辑缺失任务的无副作用边界现在有回归测试固定。
- 表单错误路径继续只渲染错误和原表单，不写 `tasks.json` 或重载调度器。

## 阶段 55：任务表单合集校验无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务表单 UI 边界。
- 固化 `/task/new` 选择不存在合集时不创建任务、不重载调度器。
- 固化 `/task/edit/<id>` 改到不存在合集时不写任务文件、不重载调度器，并保留安全 back。

已完成：

- 扩展 `tests/test_ui_route_components.py`：
  - 覆盖新建任务提交缺失 `collection_id` 时返回 400，提示“合集不存在”，不调用 `save_tasks()` 或 `reload_scheduler()`。
  - 覆盖编辑任务提交缺失 `collection_id` 时返回 400，外部 back 清洗为原任务所属合集锚点，不调用 `save_tasks()` 或 `reload_scheduler()`，原任务字段保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 55。
  - 在当前行为边界中记录 Cron 不合法和合集不存在都属于任务表单校验失败。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 55。

验证记录：

- `python -B -m unittest tests.test_ui_route_components.UiRouteComponentTests.test_task_new_missing_collection_does_not_save_or_reload tests.test_ui_route_components.UiRouteComponentTests.test_task_edit_missing_collection_keeps_safe_back_and_does_not_save`：通过，2 tests OK。
- `python -B -m compileall tests/test_ui_route_components.py`：通过。
- `python -B -m unittest discover -s tests`：通过，174 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补任务表单合集校验边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- 新建和编辑任务的“合集不存在”错误路径现在有回归测试固定。
- 表单错误路径继续只渲染错误和原表单，不写 `tasks.json` 或重载调度器。

## 阶段 56：合集加入缺失合集无副作用回归测试

状态：已完成

目标：

- 继续低风险收束合集任务操作边界。
- 固化 `/collection/add-task/<id>` 在合集不存在时返回 404。
- 确认缺失合集时不读取任务列表、不写 `tasks.json`。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖 POST `/collection/add-task/missing` 返回 404。
  - 断言缺失合集路径不调用 `load_tasks()` 或 `save_tasks()`。
  - 断言原任务文件保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 56。
  - 在当前行为边界和测试覆盖重点中记录合集加入缺失合集边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 56。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_collection_add_task_missing_collection_aborts_without_task_write`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，175 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补合集加入缺失合集边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/collection/add-task/<id>` 的缺失合集路径现在有回归测试固定。
- 缺失合集会在读取或写入任务文件前中止，避免无意义副作用。

## 阶段 57：合集加入空选择无副作用回归测试

状态：已完成

目标：

- 继续低风险收束合集任务操作边界。
- 固化 `/collection/add-task/<id>` 在未选择任务时只重定向。
- 确认未选择任务时清洗外部 back，不读取任务列表、不写 `tasks.json`。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖 POST `/collection/add-task/c1` 空表单返回 302。
  - 覆盖外部 back 被清洗为 `/collections`。
  - 断言空选择路径不调用 `load_tasks()` 或 `save_tasks()`，原任务文件保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 57。
  - 在当前行为边界中记录合集加入空选择的无副作用重定向。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 57。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_collection_add_task_empty_selection_redirects_without_task_write`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，176 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补合集加入空选择边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/collection/add-task/<id>` 的空选择路径现在有回归测试固定。
- 空选择会在读取或写入任务文件前重定向，并继续使用安全 back 清洗。

## 阶段 58：批量任务未知操作无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务批量 API 边界。
- 固化 `/api/task/bulk-action` 在 action 未知时返回 400。
- 确认未知操作在读取或写入任务文件前中止。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖未知 `action` 返回 400 和 `msg=未知批量操作`。
  - 覆盖 `task_ids` 去重后的 `count` 仍按结构化响应返回。
  - 断言未知操作路径不调用 `load_tasks()` 或 `save_tasks()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 58。
  - 在任务 API 行为边界和测试覆盖重点中记录批量未知操作无副作用边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 58。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_rejects_unknown_action_without_loading_tasks`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，177 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补批量任务未知操作边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/bulk-action` 的未知操作路径现在有回归测试固定。
- 未知操作会在读取或写入任务文件前返回 400，避免无意义副作用。

## 阶段 59：批量任务空选择无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务批量 API 边界。
- 固化 `/api/task/bulk-action` 在空选择时返回 400。
- 确认空选择在读取或写入任务文件前中止。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 补强已有空选择测试，覆盖 `msg=请选择任务`。
  - 断言空选择路径不调用 `load_tasks()` 或 `save_tasks()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 59。
  - 在任务 API 行为边界中记录空选择与未知操作一样不读取或写入任务。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 59。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_rejects_empty_selection`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，177 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补强批量任务空选择边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/bulk-action` 的空选择路径现在有无副作用回归断言。
- 空选择会在读取或写入任务文件前返回 400，避免无意义副作用。

## 阶段 60：批量任务缺失项无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务批量 API 边界。
- 固化 `/api/task/bulk-action` 在选中任务混入缺失 ID 时返回 404。
- 确认混入缺失任务时不部分写入、不重载调度器。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖批量禁用混入缺失任务时返回 404。
  - 覆盖响应中的 `missing_count` / `missing_ids`。
  - 断言不调用 `save_tasks()` 或 `reload_scheduler()`，原任务启用状态保持不变。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 60。
  - 在任务 API 行为边界中记录批量操作混入缺失任务时不部分写入或重载调度器。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 60。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_missing_task_aborts_without_write_or_reload`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，178 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补批量任务缺失项边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/bulk-action` 的混入缺失任务路径现在有回归测试固定。
- 缺失项会在任何批量动作副作用前返回 404，避免部分写入。

## 阶段 61：单任务未知操作无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务动作 API 边界。
- 固化 `/api/task/action/<unknown>/<id>` 在 action 未知时返回 400。
- 确认未知单任务操作不触发运行、停止、读取、写入或调度器重载。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖未知单任务 action 返回 400 和 `msg=未知操作`。
  - 断言不调用 `run_task_now()`、`stop_task_now()`、`load_tasks()`、`save_tasks()` 或 `reload_scheduler()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 61。
  - 在任务 API 行为边界中记录未知单任务操作无副作用边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 61。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_action_unknown_action_returns_400_without_side_effects`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，179 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补单任务未知操作边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/action/<unknown>/<id>` 的未知操作路径现在有回归测试固定。
- 未知单任务操作会在任何任务副作用前返回 400。

## 阶段 62：单任务复制缺失项无副作用回归测试

状态：已完成

目标：

- 继续低风险收束任务动作 API 边界。
- 固化 `/api/task/action/copy/<id>` 在任务缺失时返回 404。
- 确认复制缺失任务时不写回任务文件、不重载调度器。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖复制缺失任务返回 404 和 `msg=任务不存在`。
  - 断言不调用 `save_tasks()` 或 `reload_scheduler()`。
  - 断言原任务文件只保留既有任务，不产生复制项。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 62。
  - 在任务 API 行为边界和测试覆盖重点中记录单任务复制缺失项边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 62。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_copy_missing_task_returns_404_without_write_or_reload`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，180 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补单任务复制缺失项边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/action/copy/<id>` 的缺失任务路径现在有无写回、无调度器重载回归断言。
- 缺失复制不会产生额外任务项或刷新调度器。

## 阶段 63：单任务运行业务失败兼容回归测试

状态：已完成

目标：

- 继续低风险收束任务动作 API 边界。
- 固化 `/api/task/action/run/<id>` 在普通业务失败时保留 `200 + ok:false`。
- 确认运行 API 只以 `source="manual"` 委托任务运行器，不写回任务文件、不重载调度器。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖 `run_task_now()` 返回 `False, "任务已在运行"` 时接口状态码仍为 200。
  - 断言响应保留 `ok=false` 和原始 `msg`。
  - 断言 `run_task_now()` 使用 `source="manual"` 调用，且不调用 `save_tasks()` 或 `reload_scheduler()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 63。
  - 在任务 API 行为边界和测试覆盖重点中记录单任务运行业务失败兼容边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 63。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_action_run_business_failure_keeps_200_without_write_or_reload`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，181 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补单任务运行业务失败边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/action/run/<id>` 的普通业务失败兼容语义现在有回归测试固定。
- 除“任务不存在”映射为 404 外，其它运行失败继续由 JSON `ok/msg` 表达，避免破坏现有前端兼容。

## 阶段 64：单任务停止业务失败兼容回归测试

状态：已完成

目标：

- 继续低风险收束任务动作 API 边界。
- 固化 `/api/task/action/stop/<id>` 在普通停止失败时保留 `200 + ok:false`。
- 确认停止 API 不因业务失败写回任务文件或重载调度器。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖 `stop_task_now()` 返回 `False, "停止失败"` 时接口状态码仍为 200。
  - 断言响应保留 `ok=false` 和原始 `msg`。
  - 断言不调用 `save_tasks()` 或 `reload_scheduler()`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 64。
  - 在任务 API 行为边界和测试覆盖重点中记录单任务停止业务失败兼容边界。
- 更新 `docs/SESSION_HANDOFF.md`：
  - 本文件同步到阶段 64。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_action_stop_business_failure_keeps_200_without_write_or_reload`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，182 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境仍无 Playwright/Chromium，真实浏览器截图检查继续留到有浏览器环境时执行。
- 本阶段只补单任务停止业务失败边界测试，没有改变路由运行时行为。
- 本阶段没有触发真实任务进程。

收束结论：

- `/api/task/action/stop/<id>` 的普通停止失败兼容语义现在有回归测试固定。
- 除任务缺失映射为 404 外，其它停止失败继续由 JSON `ok/msg` 表达，避免破坏现有前端兼容。

## 阶段 65：批量任务表单输入兼容回归

状态：已完成

目标：

- 固化 `/api/task/bulk-action` 对传统表单输入的任务 ID 归一化行为。
- 确认空白和重复 ID 不会导致重复操作或错误计数。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖表单 `task_ids` 含首尾空白、空项和重复项的批量禁用请求。
  - 断言响应的 `count=2`、`updated_count=2`，以及两个唯一任务都被禁用。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 65。
  - 记录 JSON 与传统表单输入共用任务 ID 归一化规则。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_form_input_dedupes_and_normalizes_task_ids`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，183 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境没有 Playwright/Chromium，未进行真实浏览器截图检查。
- 本阶段只补兼容边界回归测试，没有改变路由运行时行为，也没有触发真实任务进程。

收束结论：

- 批量接口的传统表单输入路径已有回归保护，计数和副作用只基于归一化后的唯一任务 ID。

## 阶段 66：批量运行失败摘要回归

状态：已完成

目标：

- 固定批量运行部分或全部失败时的结构化统计和摘要上限。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖五个任务均运行失败的批量请求。
  - 断言完整 `failures` 保留五项，消息仅展示前三项并带 `等 5 个` 提示。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 66。
  - 记录批量运行失败字段和消息摘要规则。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_run_limits_failure_summary_to_first_three_tasks`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，184 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境没有 Playwright/Chromium，未进行真实浏览器截图检查。
- 本阶段只补 API 兼容边界回归测试，没有改变路由运行时行为，也没有触发真实任务进程。

收束结论：

- 前端既可使用完整失败列表，也可安全展示有长度上限的失败摘要。

## 阶段 67：批量停止混合结果回归

状态：已完成

目标：

- 固定批量停止任务时成功、未运行和业务失败的独立统计及失败摘要。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - 覆盖一个成功、一个未运行、四个业务失败的批量停止请求。
  - 断言 `stopped_count=1`、`skipped_count=1`、`failed_count=4`，失败消息最多展开前三项。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 67。
  - 记录批量停止的跳过项、失败项和摘要规则。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_stop_separates_skipped_and_limits_failure_summary`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，185 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境没有 Playwright/Chromium，未进行真实浏览器截图检查。
- 本阶段只补 API 兼容边界回归测试，没有改变路由运行时行为，也没有触发真实任务进程。

收束结论：

- 批量停止的“未运行”状态不会混入失败计数，前端可准确展示三类结果。

## 阶段 68：运行状态接口 Schema 回归

状态：已完成

目标：

- 固定 `/api/status` 对运行中和未运行任务的稳定响应字段。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - mock 运行态、运行记录和进程名称推导。
  - 覆盖运行中与未运行任务的完整状态响应，验证未运行任务的 `pid=null`。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 68。
  - 记录状态接口的字段契约。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_api_status_returns_stable_running_and_idle_task_fields`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，186 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境没有 Playwright/Chromium，未进行真实浏览器截图检查。
- 本阶段只补状态接口 Schema 回归测试，没有改变路由运行时行为，也没有触发真实任务进程。

收束结论：

- 前端轮询可依赖稳定字段区分运行和空闲任务。

## 阶段 69：调度任务查询异常回归

状态：已完成

目标：

- 固定 `/api/scheduler/jobs` 在调度器查询异常时的 JSON 错误协议。

已完成：

- 扩展 `tests/test_bulk_workflows.py`：
  - mock `scheduler.get_jobs()` 抛出异常。
  - 断言响应为 `500`，包含 `ok=false`、原始错误消息和空 `jobs` 列表。
- 更新 `DEVELOPMENT.md`：
  - 基线推进到阶段 69。
  - 记录调度任务查询异常的响应约定。

验证记录：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_api_scheduler_jobs_returns_json_error_when_scheduler_fails`：通过，1 test OK。
- `python -B -m compileall tests/test_bulk_workflows.py`：通过。
- `python -B -m unittest discover -s tests`：通过，187 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

受限验证：

- 当前环境没有 Playwright/Chromium，未进行真实浏览器截图检查。
- 本阶段只补错误响应回归测试，没有改变路由运行时行为，也没有触发真实任务进程。

收束结论：

- 状态页和外部调用方在调度器异常时仍可获得可解析的 JSON 响应。

## 下一阶段候选

## 阶段 70：调度任务查询 Schema 回归

状态：已完成

- 增加正常调度任务查询的回归测试，覆盖 ID、下一次执行时间和触发器序列化。
- `next_run_time` 覆盖字符串和 `null` 两种值，不触发真实调度。
- 验证：目标测试通过；全量 `188 tests OK`；响应式烟测、编译检查和 diff 检查通过。

## 下一阶段候选

## 阶段 71：任务复制响应回归

状态：已完成

- 扩展既有任务复制测试，固定成功响应的 `msg=已复制为 Demo-copy`。
- 已有测试继续验证新任务的运行态字段重置、重试配置保留与调度器重载。
- 验证：目标测试通过；全量 `188 tests OK`；响应式烟测、编译检查和 diff 检查通过。

## 下一阶段候选

## 阶段 72：任务置顶写入异常回归

状态：已完成

- mock `save_tasks()` 抛出磁盘异常，覆盖置顶接口的 `500` JSON 错误响应。
- 断言持久化任务文件仍维持未置顶状态。
- 验证：目标测试通过；全量 `189 tests OK`；响应式烟测、编译检查和 diff 检查通过。

## 下一阶段候选

## 阶段 73：批量删除混合结果回归

状态：已完成

目标：

- 固定批量删除任务在混合停止结果下的响应统计、失败列表和持久化边界。

测试契约：

- mock 三类停止结果：停止成功、停止失败和“任务未运行”。
- 停止成功与“任务未运行”的任务计入 `deleted_count` 并从任务文件移除；其他停止失败项计入 `failed_count`、写入完整 `failures` 并保留在任务文件中。
- 删除消息摘要最多展开前三项失败，超过三项时保留失败总数提示，完整失败列表仍由 `failures` 返回。
- 混合结果存在可删除项时只写回可删除项的移除结果，并重载调度器一次。

目标测试命令：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_task_bulk_delete_reports_complete_failures_and_truncates_summary`

验证记录：

- 目标测试通过，1 test OK。
- 全量 `python -B -m unittest discover -s tests`：通过，191 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `node --check fls_manager/static/fls.js`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

## 阶段 74：运行状态进程名兜底回归

状态：已完成

目标：

- 固定 `/api/status` 在运行中任务记录缺少进程名称时的安全兜底表示。

测试契约：

- mock 任务处于运行中，但对应运行记录缺少或未提供 `process_name`。
- 响应继续包含完整状态字段，`pid` 保留运行记录值，`process_name` 使用任务名称或命令经 `safe_process_name()` 推导的兜底值，接口不抛异常。
- 兜底仅用于缺失进程名称的运行记录，不改变已有非空进程名称。

目标测试命令：

- `python -B -m unittest tests.test_bulk_workflows.BulkWorkflowTests.test_api_status_running_record_without_process_name_uses_safe_fallback`

验证记录：

- 目标测试通过，1 test OK。
- 两项目标测试合并运行通过，2 tests OK。
- 全量 `python -B -m unittest discover -s tests`：通过，191 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `node --check fls_manager/static/fls.js`：通过。
- `git -c safe.directory=/data/data/com.termux/files/home/fls diff --check`：通过。

## 阶段 75：运行状态异常 JSON 回归

状态：已完成

- `/api/status` 状态收集异常时返回 `500` JSON，包含 `ok=false`、错误 `msg` 和空 `tasks`。
- 正常成功响应继续保持裸任务数组，避免破坏现有调用方。
- 新增异常回归测试，不触发真实任务进程。
- 验证：目标测试通过；全量测试、响应式烟测、编译检查、JS 语法检查和 diff 检查通过。

## 阶段 76：批量任务异常上下文回归

状态：已完成

- 批量接口在通用 `500` 响应中保留规范化后的 `action/count`。
- 加载任务失败时不调用保存或调度器重载。
- 验证：目标测试通过；全量测试、响应式烟测、编译检查、JS 语法检查和 diff 检查通过。

## 阶段 77：批量删除全失败无写入回归

状态：已完成

- 覆盖所有选中任务均停止失败的批量删除请求。
- 固定 `deleted_count=0`、失败明细和消息协议。
- 断言任务文件不变，且不调用保存或调度器重载。
- 验证：目标测试通过；全量测试、响应式烟测、编译检查、JS 语法检查和 diff 检查通过。

## 阶段 78：运行状态进程名类型兜底回归

状态：已完成

- `/api/status` 仅直接采用非空字符串 `process_name`。
- 非字符串和纯空白运行记录改用任务名称或命令生成安全兜底值。
- 新增类型边界回归，确认 PID、运行状态和已有有效字符串行为不变。
- 验证：目标测试通过；全量测试、响应式烟测、编译检查、JS 语法检查和 diff 检查通过。

## 下一阶段候选

## 阶段 79：备份任务轮询 Schema 回归

状态：已完成

- 构造内存中的已完成备份任务，固定轮询接口的进度、文件、大小和时间字段。
- 固定缺失备份任务的 `404` JSON 响应。
- 测试不启动后台线程、不创建真实归档。
- 验证：目标测试通过；全量测试、响应式烟测、编译检查、JS 语法检查和 diff 检查通过。

## 下一阶段候选

## 阶段 80：备份列表 Schema 回归

状态：已完成

- 在隔离目录构造多个备份文件，固定列表字段和大小格式。
- 验证仅返回 `.tar.gz` 普通文件，并按修改时间倒序排列。
- 测试不启动备份线程，不执行压缩或恢复。
- 验证：目标测试通过；全量测试、响应式烟测、编译检查、JS 语法检查和 diff 检查通过。

## 下一阶段候选

- 有浏览器环境时补真实响应式截图验收，重点覆盖 `/tasks`、`/collections`、`/logs`、`/online-scripts` 和脚本拉取页面。
- 等任务/日志相关工作区改动收束后，再把 `pagination_card()` 接入任务和日志分页。
