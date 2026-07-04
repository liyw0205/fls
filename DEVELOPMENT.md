# FLS 开发文档

更新时间：2026-07-05
基线：`main` / `b8762aa`

本文用于后续开发协作。每次完成开发后，都要同步更新本文的“开发日志”和“后续方向”，必要时同步调整架构、数据模型、接口和验证清单。

## 1. 项目定位

FLS = Flask Lightweight Script Manager，是一个基于 Flask 的轻量级脚本任务管理面板。

核心能力：

- 通过 Web 面板管理脚本、任务、合集、全局变量、代理、通知、日志、依赖和备份恢复。
- 支持手动任务和 Cron 定时任务。
- 支持 Python、Shell、Node.js、TypeScript、PowerShell、Batch、PHP、Ruby、Perl、Lua、Jar 等脚本类型。
- 支持 Linux、Windows、Termux，并提供不同平台的启停脚本。

当前项目不是前后端分离架构。页面主要由 Flask 路由直接拼接 HTML，公共布局在 `fls_manager/ui/layout.py`，静态增强逻辑在 `fls_manager/static/fls.js` 和 `fls_manager/static/fls.css`。

## 1.1 产品参考对象

后续产品和交互设计可以参考同类任务面板，但不能照搬实现或引入不适合 FLS 定位的复杂依赖。

参考对象：

- 青龙面板：参考成熟任务管理面板的信息架构、脚本/环境变量/配置/日志/通知/移动端操作等功能组织。
- 呆呆面板：参考轻量、现代、开箱即用的产品方向，以及订阅、依赖、Open API、通知渠道等功能组织。
- 白虎面板：参考低资源占用、多运行时管理、节点互联、跨节点环境变量同步等后续演进方向。

借鉴边界：

- FLS 继续保持 Flask + 原生 CSS/JS 的轻量架构，不因为参考对象而强制切换 Go、Vue、Node/npm 构建链或数据库。
- 优先学习信息架构、操作流程、响应式体验和功能取舍。
- 涉及安全、鉴权、远程命令执行、跨节点互联、开放 API 等能力时，必须先做威胁建模和最小权限设计。
- 如复用开源代码、图标、样式或协议，需要先确认许可证和维护成本；默认只做产品行为参考。

## 2. 快速启动与运行入口

主入口：

- `fls-manager.py`：Python 主入口，负责依赖自检安装、创建 Flask app、加载调度器、清理日志并启动服务。
- `fls.sh`：Linux / Termux 启停脚本。
- `fls.ps1`：Windows PowerShell 启停脚本。
- `fls.bat`：Windows CMD 入口，转调 `fls.ps1`。
- `fls-a.sh`：KernelSU / Magisk / adb root 环境调用 Termux 启动。

常用命令：

```sh
python fls-manager.py
sh fls.sh start
sh fls.sh stop
sh fls.sh restart
sh fls.sh status
sh fls.sh log
```

`fls-manager.py` 会自动检查并尝试安装这些依赖：

- `flask`
- `apscheduler`
- `requests`
- `PySocks`

重要环境变量：

- `FLS_BASE_DIR`：覆盖默认工作目录。
- `FLS_HOST`：覆盖监听地址，默认 `0.0.0.0`。
- `FLS_PORT`：覆盖监听端口，默认 `5700`。
- `FLS_TOKEN`：覆盖管理 Token。
- `FLS_SECRET_KEY`：覆盖 Flask session secret。
- `FLS_PYTHON`：任务运行时 Python 解释器。
- `FLS_NODE`：任务运行时 Node.js 解释器。
- `FLS_BASH`：任务运行时 Bash 解释器。

首次运行如果没有 Token，会跳转到 `/setup` 进行初始化。

## 3. 目录与模块边界

根目录：

- `README.md`：用户使用说明。
- `AI-NOTICE.md`：AI 相关说明。
- `fls-manager.py`：服务入口。
- `fls.sh` / `fls.ps1` / `fls.bat` / `fls-a.sh`：平台启动脚本。
- `fls_manager/`：核心应用代码。
- `data/`：运行数据，通常不应提交真实本地内容。
- `log/`：运行和任务日志，通常不应提交。
- `scripts/`：用户脚本目录。

核心模块：

- `fls_manager/paths.py`：工作目录探测和 `data/log/scripts` 路径常量。
- `fls_manager/app.py`：创建 Flask app、配置 session、注册 Blueprint。
- `fls_manager/auth.py`：登录态、Token、API 鉴权入口。
- `fls_manager/security.py`：随机验证码和 TOTP 二次验证。
- `fls_manager/config.py`：默认配置、配置合并、端口、Token、虚拟时间。
- `fls_manager/storage.py`：JSON 文件读写和进程内锁。
- `fls_manager/models.py`：任务、全局变量、代理、合集的数据访问层。
- `fls_manager/command.py`：任务命令解析、脚本类型归一、混合命令展开、`fls_kill` 注入。
- `fls_manager/task_runner.py`：任务启动、停止、超时、重试、日志、通知。
- `fls_manager/scheduler.py`：Cron 解析、虚拟时间调度、APScheduler job 重载。
- `fls_manager/logs.py`：任务日志文件、tail、日志清理。
- `fls_manager/proxy.py`：HTTP/SOCKS/GitHub 代理配置、测试和任务环境注入。
- `fls_manager/notify.py`：通知渠道模型和发送实现。
- `fls_manager/state.py`：全局运行态，包括 scheduler、运行中任务、依赖安装任务。
- `fls_manager/utils.py`：HTML escape、时间、名称清洗、环境变量解析等通用工具。

路由模块：

- `fls_manager/routes/auth_routes.py`：登录、退出、初始化、二次验证。
- `fls_manager/routes/dashboard.py`：仪表盘和运行时指标。
- `fls_manager/routes/tasks/`：任务列表、新建编辑、动作、日志、配置文件、合集。
- `fls_manager/routes/env/`：全局变量管理。
- `fls_manager/routes/scripts/`：脚本浏览、新建、编辑、改名、下载、删除、调试、拉取导入。
- `fls_manager/routes/online_scripts/`：在线脚本源、刷新、安装、导入任务、文档和日志。
- `fls_manager/routes/logs/`：日志列表、查看、删除、分组删除。
- `fls_manager/routes/config/`：系统配置。
- `fls_manager/routes/proxy/`：代理管理和测试。
- `fls_manager/routes/notify/`：通知配置和测试。
- `fls_manager/routes/backup/`：备份导出、导入、任务状态。
- `fls_manager/routes/deps.py`：Python 依赖安装、卸载和日志。
- `fls_manager/routes/runtime.py`：Node 等运行时安装。
- `fls_manager/routes/status.py`：环境状态页。
- `fls_manager/routes/about/`：版本、更新、面板控制、时间同步。
- `fls_manager/routes/api.py`：任务状态、调度器状态、任务动作 API。

Blueprint 约定：

- 单文件功能可直接在 `routes/<name>.py` 中创建 `bp = Blueprint(...)`。
- 多文件功能使用 `routes/<domain>/bp.py` 暴露 `bp`，在 `routes/<domain>/__init__.py` 导入子模块让 `@bp.route` 生效。
- 新增功能后在 `fls_manager/app.py` 注册 Blueprint。
- 新增导航入口时同步更新 `fls_manager/ui/layout.py`。

## 4. 数据文件与模型

所有运行数据默认位于 `BASE_DIR/data`。`BASE_DIR` 来自 `FLS_BASE_DIR`，否则按平台推断：

- Windows：`C:/fls`
- Termux：`$HOME/fls`
- Linux root 优先：`/root/fls`
- 兜底：`$HOME/fls`

核心 JSON schema 和读取迁移规则见 `docs/DATA_SCHEMA.md`。

主要数据文件：

- `data/config.json`：系统配置、通知配置、线上脚本源等。
- `data/tasks.json`：任务列表。
- `data/global_env.json`：全局环境变量。
- `data/proxies.json`：代理配置。
- `data/collections.json`：任务合集。
- `data/fls-manager.pid`：面板进程 PID。
- `data/secret_key.txt`：持久化 Flask secret_key。
- `data/fls_code.json`：随机二次验证码临时文件。

读写规则：

- JSON 读写统一走 `fls_manager/storage.py`。
- 业务数据访问优先走 `fls_manager/models.py`。
- `tasks.json`、`global_env.json`、`proxies.json`、`collections.json` 读取时会通过 `models.py` 做归一化。
- `config.json` 读取时会通过 `config.normalize_config_data()` 合并默认值、转换类型并钳制数值范围。
- 写文件使用临时文件替换，减少半写入文件。
- 当前锁是进程内 `threading.RLock`，不能保证多进程事务一致性。
- 不要在路由中直接读写 `data/*.json`，除非是在补充专门的数据访问函数。

任务字段：

- `id`：任务 ID，`uuid.uuid4().hex`。
- `name`：任务名。
- `remark`：备注。
- `command`：命令，支持单行 `task xxx.py` 或多行 Shell 混合命令。
- `cron`：Cron 表达式，支持 5 位或 6 位。
- `config_path`：相对 `scripts/` 的配置文件路径。
- `collection_id`：所属合集 ID。
- `enabled`：是否启用调度。
- `env`：任务级环境变量。
- `proxy_id`：代理 ID。
- `notify`：任务结束通知配置，形如 `{"mode": "default|none|custom", "ids": []}`。
- `random_delay`：随机延迟配置，形如 `{"mode": "none|default|custom", "seconds": 0}`。
- `retry_count`：失败重试次数，范围 `0-20`。
- `run_count`：运行次数。
- `pinned`：列表置顶。
- `created_at` / `updated_at` / `last_run_at`：面板时间字符串。

兼容字段：

- 旧版任务可能包含 `notify_ids`，`routes/tasks/forms.py` 会归一化为新的 `notify` 结构。

## 5. 请求、鉴权与安全流程

`create_app()` 在注册路由前设置：

- 持久化 `secret_key`。
- `PERMANENT_SESSION_LIFETIME`。
- `SESSION_COOKIE_HTTPONLY=True`。
- `SESSION_COOKIE_SAMESITE="Lax"`。
- `app.before_request(auth_before_request)`。

鉴权优先级：

1. 静态资源和认证路由放行。
2. 如果未配置 Token，API 返回 403，页面跳转 `/setup`。
3. `X-Token` 请求头命中 Token 时直接放行。
4. URL `?token=` 命中 Token 时写入 session 并重定向到清理后的 URL。
5. session Token 有效且二次验证通过时放行。
6. API 返回 JSON 错误，页面跳转登录页。

二次验证：

- 配置项：`security_verify_enabled`、`security_verify_type`、`totp_secret`。
- 支持随机验证码和 TOTP。
- 随机验证码默认 300 秒过期。

开发注意：

- 新增 API 应让错误响应保持 JSON。
- 新增页面应走统一 `layout()`，避免绕过鉴权流程。
- 返回跳转 URL 时优先使用 `utils.get_back_url()` 或等价校验，避免开放重定向。

## 6. 任务执行链路

任务创建或编辑：

1. 路由解析表单并验证必填字段。
2. Cron 不为空时用 `cron_to_trigger()` 校验。
3. 写入 `data/tasks.json`。
4. 调用 `reload_scheduler()` 重载定时任务。

调度：

1. `scheduler.py` 使用面板虚拟时间计算下一次 Cron。
2. 再把虚拟时间转换为真实系统时间。
3. APScheduler 使用 `DateTrigger` 触发一次。
4. 触发后执行任务并重新计算下一次时间。

运行：

1. `run_task_now(task_id, source)` 读取任务。
2. `command.build_command()` 解析命令。
3. 创建任务日志文件。
4. 写入 `RUNNING` 状态。
5. 后台线程启动任务。
6. 合并环境变量：系统环境、全局变量、任务变量、代理变量。
7. 支持随机延迟、失败重试、超时结束。
8. 任务结束后写日志、清理状态、发送通知。

停止：

- `stop_task_now()` 标记手动停止，并结束子进程或进程组。
- 手动停止不会发送任务完成通知。

命令模式：

- 单行纯 `task xxx.py arg`：直接构造命令数组，`shell=False`，工作目录为脚本所在目录。
- 多行或混合命令：展开其中的 `task xxx.py` 行，整体以 Shell 运行，工作目录为 `scripts/`。
- 使用 `fls_kill` 时会在 Shell 命令前注入内置函数。

## 7. 前端与页面约定

页面渲染：

- 公共布局由 `fls_manager/ui/layout.py` 生成。
- 页面内容通常由路由函数拼 HTML 字符串。
- 用户输入输出到 HTML 时必须使用 `utils.h()` 转义。
- 低风险通用 HTML 外壳优先放到 `fls_manager/ui/components.py`，当前已有 `page_header_card()`、`table_card()`、`message_card()`、`summary_item()` 和 `pagination_card()`。

静态资源：

- `fls_manager/static/fls.css`：全局样式、响应式布局、表格、表单、按钮。
- `fls_manager/static/fls.js`：移动端菜单、表格字段名补全、长表单悬浮提交按钮等增强逻辑。

响应式要求：

- 前端必须同时支持手机、平板和电脑显示。
- JS 会按设备和宽度给 `body` 添加 `fls-phone`、`fls-tablet`、`fls-desktop`，并保留历史兼容类 `fls-mobile`。
- 手机和小平板使用抽屉侧边栏、紧凑卡片、卡片化表格和原生 textarea。
- 横屏平板和中等宽度桌面使用压缩侧边栏、三列仪表盘网格、两列卡片/表单和可横向滚动表格。
- 桌面端保持完整侧栏、宽松间距和 CodeMirror 编辑器。
- `fls-mobile` 是布局语义，不是单纯设备 UA 语义；宽度大于 900px 的横屏平板不应强制进入抽屉移动布局。
- 新增页面时必须检查 390px、768px、1024px、1440px 四类宽度，不允许按钮、表格、日志、代码块或长任务名横向撑破页面。

开发约定：

- 新页面优先复用现有 `.card`、`.table-wrap`、`.btn`、`.badge`、`.form-grid` 等样式。
- 表单页面如果较长，不需要手写悬浮按钮，`fls.js` 会自动处理。
- 新增危险按钮文案应包含清晰动作名，避免被悬浮提交逻辑误识别为普通提交。
- UI 文案保持中文，命令、代码、日志和配置键保持原文。

## 8. 日志、备份、线上脚本

日志：

- 任务日志写入 `log/<safe-task-name>-YYYY-mm-dd-HH-MM-SS.log`。
- `cleanup_logs()` 会删除超过大小限制的日志，并按任务分组保留最近 N 个。
- 日志分组优先从日志头里的任务名解析。

备份：

- 备份功能位于 `routes/backup/`。
- 解压导入有路径安全检查，继续开发时不能绕过 `safe_extract_tar()` / `safe_extract_zip()`。
- 备份依赖文件生成逻辑在 `routes/backup/_common.py`。

线上脚本：

- 源地址默认在 `config.py` 的 `online_script_source`。
- 源读取、规范化、缓存、刷新在 `fls_manager/online_scripts/source.py`。
- 安装流程在 `fls_manager/online_scripts/install.py`。
- 导入任务逻辑在 `fls_manager/online_scripts/tasks.py`。
- 文档渲染在 `fls_manager/online_scripts/docs.py`。

## 9. 开发流程约定

每次开发建议流程：

1. 先读相关路由、模型、执行链路，不直接改入口。
2. 修改数据结构时，先补归一化或兼容逻辑，再改页面和 API。
3. 修改任务、Cron、时间、运行状态相关逻辑时，必须验证手动运行和定时调度。
4. 修改脚本管理、备份、线上脚本下载时，必须验证路径穿越和归档解压边界。
5. 修改鉴权、Token、二次验证时，必须验证页面请求和 API 请求的不同响应。
6. 修改配置项时，同步更新 `DEFAULT_CONFIG`、配置页、文档。
7. 开发结束后更新本文“开发日志”和“后续方向”。

阶段会话规则：

- 项目开发按阶段推进，避免单个会话上下文过大。
- 每个阶段默认采用主代理加子代理协作：主代理负责范围控制、实现整合、验证和提交；子代理负责独立审查、方案调研或不重叠文件范围的实现。
- 子代理必须有明确任务边界，不能还原或覆盖其他参与者的修改。
- 阶段结束前必须更新：
  - `DEVELOPMENT.md`
  - `docs/DEVELOPMENT_PROGRESS.md`
  - `docs/SESSION_HANDOFF.md`
- `docs/SESSION_HANDOFF.md` 必须写明本阶段完成进度、受限验证、遗留风险和下一阶段目标。
- 阶段结束以 git commit 收束；提交时只纳入本阶段相关文件，不能误提交用户或其他阶段的无关改动。
- 当前工具不能真正自动创建新的聊天线程；下一会话应以最新 `docs/SESSION_HANDOFF.md` 为入口继续。
- 如果 GitHub、npm 或 Python 生态已有成熟开源方案，优先评估复用；只有在项目开箱即用、离线能力、体积或维护成本更优时才继续本地轻量实现。

代码风格：

- Python 保持标准库优先，沿用当前模块化风格。
- 新数据读写优先增加 `models.py` 或功能域内 `_common.py` 辅助函数。
- 页面输出必须显式 HTML escape。
- 避免在路由函数里写过长业务流程，复杂逻辑下沉到功能模块。
- 不提交 `__pycache__/`、本地 `data/` 内容、真实日志和本地脚本私密内容。

## 10. 验证清单

轻量检查：

```sh
python -B -m unittest discover -s tests
python -B tools/responsive_smoke.py
python -B -m compileall fls-manager.py fls_manager tests tools
```

手动验证：

- 启动面板后访问 `/`。
- 分别用手机宽度、平板宽度、桌面宽度检查核心页面布局。
- 未设置 Token 时能进入 `/setup`。
- 设置 Token 后登录、退出、过期返回逻辑正常。
- 新建手动任务，执行 `task xxx.py`。
- 新建 Cron 任务，确认 `/api/scheduler/jobs` 有对应 job。
- 修改任务启用状态后调度器刷新。
- 任务超时、失败重试、手动停止日志符合预期。
- 代理配置测试、任务代理环境注入符合预期。
- 通知测试和任务结束通知符合预期。
- 日志查看、实时刷新、删除符合预期。
- 备份导出和导入不会越权写出目标目录。

当前已有 `tests/` 轻量自动化测试目录。新增测试优先使用标准库 `unittest`，并通过临时 `FLS_BASE_DIR` 隔离真实运行数据。

已覆盖：

- `command.py` 的命令解析。
- `scheduler.py` 的 Cron 和虚拟时间换算。
- `config.py` / `models.py` 的核心 JSON 读取归一化和兼容迁移。
- `task_runner.py` 的任务提交、运行状态、随机延迟、重试次数、停止、watcher 通知/重试分支和环境合并。
- `proxy.py` 的代理 URL、请求代理字典和任务环境变量注入。
- `notify.py` 的任务通知 ID 选择、默认通知去重和 `send_by_ids()` mock 发送。
- `task_runner.py` 的 `_start_task_attempt()` Popen 参数、watcher 线程提交和超时 watcher 分支。
- `logs.py` 的日志大小清理和按任务分组保留。
- `notify.py` 的 webhook、Bark、SMTP 三类 `send_one()` 出口 mock。
- `proxy.py` 的 GitHub 代理可用性缓存。
- `storage.py` 的缺失文件、损坏 JSON、父目录创建、临时文件替换和替换失败边界。
- `notify.py` 的 Server 酱、PushPlus、Telegram、企业微信、钉钉、飞书、Ntfy、Gotify、PushDeer `send_one()` 出口 mock。
- `proxy.py` 的 GitHub URL 改写、Git 临时配置参数、质量检测 URL 解析和普通代理质量检测并发聚合。
- `logs.py` 的 `tail_file()` 缺失/尾读/坏 UTF-8 和 `parse_task_name_from_log()` 边界。
- `notify.py` 的通知配置清理、默认通知过滤保存、内容分片、WxPusher 出口 mock 和多分片多通知发送顺序。
- `proxy.py` 的 GitHub 质量检测拼接请求、无 git、Git insteadOf 成功/失败/超时，以及 Git 命令代理拼接 helper。
- `logs.py` 的 `latest_log_for_task()`、`cleanup_logs()` keep=0、非法配置、无启动头分组和 unlink 异常吞掉边界。
- `auth.py` 的 API 与页面鉴权分支。
- `backup/_common.py` 的备份文件名归一、zip/tar 路径穿越拒绝、安全 tar 解压 filter 兼容、tar 特殊成员拒绝和 DeprecationWarning 回归测试。
- `ui.components.table_card()` 的可选说明区、操作区、表格 ID、标题和表头 HTML 转义。
- `ui.components.pagination_card()` 的链接分页、按钮分页、禁用态、省略号和 HTML 转义。
- `ui.components.message_card()` 的空/空白消息、成功/错误/普通提示、未知类型回退、加粗样式、可选标题和 HTML 转义。
- `ui.components.summary_item()` 的统计项结构、数字 value 和 HTML 转义。
- 路由层 UI 组件接入：`/pull/new` 脚本新建头部卡和普通提示卡、`/pull/fetch` 和 `/pull/import` 表单头部卡、普通/结果提示卡，以及 `/online-scripts/source` 脚本源 JSON 头部卡、`/online-scripts/doc/<id>` 文档加载失败卡、无文档链接提示卡的渲染与转义。
- 路由层 UI 组件接入：`/task/config/<id>` 保存成功/失败提示卡的渲染与转义。
- 路由层 UI 组件接入：`/env/import` 任务变量导入页头部卡和表格卡、`/env/view` 全局变量全文编辑页头部卡、`/env/new` 和 `/env/edit/<key>` 全局变量表单头部卡、`/proxy/new` 和 `/proxy/edit/<id>` 代理表单头部卡、`/scripts/view` 和 `/scripts/rename` 脚本文件表单头部卡、`/config` 脚本类型表格卡、`/deps` 依赖列表、`/deps/refresh` 依赖刷新完成页头部卡、`/deps/install-log/<id>` 依赖安装日志头部卡、`/deps/uninstall` 依赖卸载结果页头部卡、`/panel/status`、`/` 仪表盘环境状态、`/about` 面板信息、`/notify/test/<id>` 通知测试结果、`/about/job-log/<id>` 后台任务日志头部卡、`/about/restart-panel`、`/about/stop-panel` 面板控制结果头部卡、`/about/refresh-log`、`/about/update-version` 版本失败头部卡、`/online-scripts/log/<id>` 在线脚本安装日志头部卡、`/online-scripts/install/<id>` 安装确认头部卡、`/online-scripts/install-select/<id>` 安装选择页头部卡、`/backup/import` 备份导入完成页头部卡和 `/scripts/debug-log/<id>` 脚本调试日志头部卡渲染、响应式表格 ID 保留与 HTML 转义。

后续优先补充：

- 响应式真实浏览器截图验收，重点检查手机、平板、桌面下任务、日志、配置和在线脚本页面。
- 继续低风险 UI 组件抽取，优先其它未脏页面纯文本提示卡；分页组件后续可逐步接入任务/日志页。

## 11. 已知约束

- JSON 存储适合轻量单实例部署，不适合多进程高并发写入。
- APScheduler 使用进程内调度，面板进程退出后不会继续调度。
- 当前没有数据库迁移层，数据结构演进必须在读取时做兼容归一。
- 页面由字符串拼接 HTML，新增复杂页面时要特别注意转义和可维护性。
- CodeMirror 资源来自 CDN，离线环境下编辑器增强可能不可用。
- `totp_qr_url()` 使用在线二维码服务，离线环境只能展示密钥或 otpauth 链接。
- 依赖安装、运行时安装、在线脚本源刷新都依赖外部网络或代理配置。

## 12. 后续方向

优先级高：

- 继续维护 `docs/DATA_SCHEMA.md`，新增或调整 `data/*.json` 字段时同步更新读取迁移函数。
- 把过长路由里的业务流程逐步下沉到 service/helper 模块。
- 在具备浏览器环境时补真实截图响应式验收。

优先级中：

- 给配置、任务、代理、通知等 JSON 写入增加备份或回滚机制。
- 将常用页面组件抽到 `fls_manager/ui/`，减少路由里的 HTML 重复。
- 增加运行中任务状态 API 的更多字段，例如耗时、attempt、source。
- 改善离线环境下 CodeMirror 和 TOTP 二维码资源可用性。
- 继续抽查各功能页在手机、平板、桌面下的细节间距和复杂表格表现。
- 对标青龙面板、呆呆面板、白虎面板，整理 FLS 可借鉴的导航结构、任务详情、订阅管理、运行时管理和 Open API 设计。

优先级低：

- 评估是否引入轻量模板引擎组织复杂页面。
- 评估是否引入 SQLite 作为可选存储后端。
- 增加更细的操作审计日志。

## 13. 开发日志

### 2026-07-05

- 阶段 38 将在线脚本源 JSON 页面顶部说明接入 `page_header_card()`，保留缓存 JSON textarea、保存脚本源 JSON 表单、返回入口和成功/失败消息卡，并补充 `/online-scripts/source` 路由渲染与缓存内容转义测试。
- 阶段 37 将脚本拉取和导入表单页接入 `page_header_card()`，保留拉取类型、代理选择、文件上传、返回入口和结果消息卡，并补充 `/pull/fetch`、`/pull/import` GET 路由渲染与目录转义测试。
- 阶段 36 将配置页“task 可执行脚本类型”表格接入 `table_card()`，保留表单内 checkbox、保存配置按钮和安全验证 JS，并补充 `/config` 路由渲染与启用状态测试。
- 阶段 35 将脚本新建、查看/编辑和改名页面接入 `page_header_card()`，保留 CodeMirror textarea、保存/调试/改名/返回按钮和提示消息卡，并补充 `/pull/new`、`/scripts/view`、`/scripts/rename` 路由渲染与转义测试。
- 阶段 34 将代理新增和编辑表单页接入 `page_header_card()`，保留代理字段卡、自定义质量检测地址、实时测试/质量检测 JS 和保存逻辑，并补充 `/proxy/new`、`/proxy/edit/<id>` 路由渲染与转义测试。
- 阶段 33 将全局变量查看全部、新增和编辑页面接入 `page_header_card()`，表单字段继续留在普通卡片内，POST 空变量名纯文本 400 响应保持不变，并补充 `/env/view`、`/env/new`、`/env/edit/<key>` 路由渲染与转义测试。
- 阶段 32 将从任务变量导入到全局变量页接入 `page_header_card()` 和 `table_card()`，保留允许覆盖复选框、导入状态 badge 和底部提交区，并通过临时任务/全局变量数据补充 `/env/import` 路由渲染与转义测试。
- 阶段 31 将依赖卸载结果页接入 `page_header_card()`，保留卸载输出日志块和返回依赖管理入口，并通过 mock `pip_cmd()` 补充 `/deps/uninstall` 路由渲染与输出转义测试，避免执行真实 pip 卸载。
- 阶段 30 将依赖刷新完成页头部接入 `page_header_card()`，保留核心依赖检测表格和返回依赖管理入口，并通过 mock `refresh_dependency_cache()` 补充 `/deps/refresh` 路由渲染与转义测试，避免依赖真实运行环境状态。
- 阶段 29 将依赖安装日志页接入 `page_header_card()`，保留安装状态、日志文件、返回/刷新入口和实时日志脚本，并通过假 `DEPS_RUNNING` 记录补充 `/deps/install-log/<id>` 缺失/运行中记录路由渲染与转义测试，避免触发真实 pip 安装。
- 阶段 28 将脚本调试日志页接入 `page_header_card()`，保留调试记录缺失提示、运行状态、停止调试按钮、返回入口、日志浮动控制和实时日志脚本，并补充 `/scripts/debug-log/<id>` 缺失/存在记录路由渲染与转义测试。
- 阶段 27 将备份导入完成页接入 `page_header_card()`，保留已恢复内容、依赖恢复、日志信息和返回/日志入口，并通过临时 `FLS_BASE_DIR` 内的小型 tar.gz 备份补充 `/backup/import` 成功渲染测试，避免触碰真实数据。
- 阶段 26 将关于页刷新更新日志和更新版本的失败结果页接入 `page_header_card()`，保留返回关于页入口，并通过 mock Git 可用性/仓库状态补充失败分支路由测试。
- 阶段 25 将在线脚本安装选择页顶部说明接入 `page_header_card()`，保留任务选择表单、隐藏字段、分页和任务选择 JS，并补充 `/online-scripts/install-select/<id>` 路由渲染、字段转义和任务选择 shell 保留测试。
- 阶段 24 将在线脚本文档无 `doc_link` 提示接入 `page_header_card()`，保留返回在线脚本入口，并补充 `/online-scripts/doc/<id>` 无文档链接路由渲染测试。
- 阶段 23 将在线脚本安装目标路径非法和目标已存在确认页接入 `page_header_card()`，保留继续安装表单和隐藏字段，并补充 `/online-scripts/install/<id>` 路由渲染、路径转义和不触发真实安装的确认页测试。
- 阶段 22 将在线脚本安装日志页头部接入 `page_header_card()`，保留安装记录缺失提示、停止安装按钮和实时日志主体，并补充 `/online-scripts/log/<id>` 路由渲染与动态字段转义测试。
- 阶段 21 将面板重启/停止结果页接入 `page_header_card()`，保留控制脚本缺失提示、成功后的跳转脚本和面板日志入口，并通过 mock 控制脚本与线程补充路由渲染测试，避免触发真实启停。
- 阶段 20 将后台任务日志页头部接入 `page_header_card()`，保留不存在记录提示、返回/日志入口和实时日志主体，并补充 `page_header_card()` 与 `/about/job-log/<id>` 路由渲染测试。
- 阶段 19 将通知测试结果页接入 `table_card()`，用状态 badge 展示成功/失败，保留返回通知管理操作区，并补充通知测试路由渲染与返回消息转义测试。
- 阶段 18 将关于页“面板信息”只读表格接入 `table_card()`，保留项目仓库链接和路径字段转义，同时补充 `/about` 路由渲染测试并把 `/about` 纳入响应式 smoke。
- 阶段 17 将仪表盘“环境状态”接入 `table_card()`，保留峰值 CPU 说明和现有环境行渲染，同时补充 `/` 路由渲染测试覆盖表格标题、表头和关键行。

### 2026-07-04

- 阶段 16 扩展 `table_card()`，支持可选说明区、操作区和 `table_id`，同时保持标题和表头转义以及旧调用兼容。
- 阶段 16 将依赖管理页、依赖刷新结果页和运行环境页接入 `table_card()`；运行环境页保留 `runtimeTable` ID，避免破坏移动端响应式表格 CSS。
- 阶段 16 扩展组件与路由测试，覆盖 `table_card()` 可选结构、`/deps` 依赖列表转义，以及 `/panel/status` 运行环境表格 ID 和运行时字段转义。
- 阶段 15 继续低风险消息卡接入：`fls_manager/routes/tasks/config_file.py` 的任务配置保存结果统一使用 `message_card()`，保留成功/失败加粗色彩和空消息不渲染行为。
- 阶段 15 扩展 `tests/test_ui_route_components.py`，覆盖 `/task/config/<id>` 保存成功提示、写入失败提示和错误消息 HTML 转义。
- 阶段 15 避开已有长期脏改动的任务列表、日志、认证/API 和 `ui/tables.py`，只触碰未脏的任务配置文件路由。

### 2026-07-03

- 新增本开发文档，基于当前项目结构、核心模块、路由、任务执行链路和数据文件约定整理。
- 记录后续开发必须维护本文的规则：每次开发后更新“开发日志”和“后续方向”，涉及架构、配置、数据结构或接口时同步更新对应章节。
- 补充前端响应式开发要求：手机、小平板、横屏平板和桌面必须分别适配，并要求后续新增页面检查 390px、768px、1024px、1440px 四类宽度。
- 开始前端响应式开发：增加 `fls-phone`、`fls-tablet`、`fls-desktop` 设备类，补充平板布局规则和手机优先兜底规则。
- 补充阶段会话规则：每阶段使用主代理加子代理协作，结束前更新开发进度和会话交接文档，并以 git commit 收束。
- 新增 `.gitignore`，忽略 Python 缓存、虚拟环境和本地运行数据。
- 根据子代理审查修正横屏平板分类：`fls-mobile` 按布局宽度判断，避免 1024px iPad/Android 平板被强制切到抽屉移动布局。
- 补充产品参考对象：青龙面板、呆呆面板、白虎面板仅作为信息架构、交互流程和功能演进参考，不改变 FLS 轻量开箱即用定位。
- 阶段 2 新增轻量响应式烟测工具 `tools/responsive_smoke.py`，用于在没有真实浏览器环境时检查核心页面结构、静态资源版本和响应式关键 token。
- 阶段 2 新增 `docs/PANEL_REFERENCE.md`，整理青龙面板、呆呆面板、白虎面板参考边界和 FLS 候选需求池。
- 阶段 2 开始低风险 UI 组件抽取：新增 `fls_manager/ui/components.py`，并在全局变量、通知、代理、脚本管理页面接入 `page_header_card()` 和 `table_card()`。
- 阶段 3 新增 `tests/test_auth_backup.py`，覆盖 Token 初始化、页面/API 鉴权分支、Query Token 清理跳转，以及备份文件名归一和 zip/tar 路径穿越拒绝。
- 阶段 3 新增 `tests/test_command_scheduler.py`，覆盖脚本类型归一、命令参数引用、`task` 命令构建、混合命令展开、5/6 位 Cron 和虚拟时间互逆换算。
- 阶段 3 将 `python -B -m unittest discover -s tests` 纳入常规验证清单，并继续保留 `tools/responsive_smoke.py` 作为无浏览器环境下的响应式结构烟测。
- 阶段 4 新增 `docs/DATA_SCHEMA.md`，文档化 `tasks.json`、`config.json`、`global_env.json`、`proxies.json`、`collections.json` 的规范字段和读取迁移规则。
- 阶段 4 在 `models.py` 增加任务、全局变量、代理、合集的读取归一化和保存归一化，迁移旧 `notify_ids` 并清洗坏类型。
- 阶段 4 在 `config.py` 增加 `normalize_config_data()`，集中处理默认值合并、布尔转换、数值钳制和脚本类型过滤。
- 阶段 4 根据子代理审查将面板时区偏移范围收紧为 `-23..23`，同步修复时间同步页选项和 helper，避免 `datetime.timezone()` 在 `±24` 边界报错。
- 阶段 4 新增 `tests/test_schema_migration.py`，覆盖核心 JSON 读取迁移和配置归一化。
- 阶段 5 新增 `tests/test_task_runtime.py`，覆盖任务提交状态、运行次数更新、随机延迟、重试次数、停止流程、worker 环境合并、watcher 通知/不通知/重试分支、代理环境注入和通知发送 mock。
- 阶段 6 扩展 `tests/test_task_runtime.py`，覆盖 `_start_task_attempt()` 的 Popen 参数和 watcher 线程提交、watcher 超时强杀、日志清理、GitHub 代理缓存、webhook/Bark/SMTP 通知出口 mock。
- 阶段 7 新增 `tests/test_storage_notify_proxy.py`，覆盖 storage 异常读写、更多通知渠道出口 mock、GitHub URL/Git 配置参数、普通代理质量检测并发聚合，以及日志 tail/任务名解析边界。
- 阶段 8 扩展 `tests/test_storage_notify_proxy.py`，覆盖通知配置清理、默认通知保存过滤、内容分片、WxPusher、多分片发送顺序、GitHub 代理质量检测细分分支、Git 命令代理 helper，以及最新日志和日志清理更多边界。
- 阶段 9 加固 `safe_extract_tar()`，显式使用 tarfile `filter="data"` 并兼容旧 Python；解压前拒绝 tar 特殊成员、链接和跨平台绝对/穿越路径，同时补充 zip 绝对/反斜杠路径测试。
- 阶段 10 新增通用分页组件 `pagination_card()`，保留 `.card`、`.help`、`.action-row`、`.btn` 响应式结构；先替换在线脚本页和安装选择页两处分页函数，并新增组件单元测试。
- 阶段 11 新增通用消息卡组件 `message_card()`，集中处理空/空白消息、成功/错误/普通提示颜色、未知类型回退、加粗样式和 HTML 转义；先替换在线脚本列表页和脚本源 JSON 页的成功/失败提示卡，并扩展组件单元测试。
- 阶段 12 继续 `message_card()` 第二批接入：替换在线脚本文档页的文档加载失败卡，以及脚本新建、编辑、改名页的普通提示卡；新增 `tests/test_ui_route_components.py` 覆盖路由渲染与转义，并把 `/pull/new` 纳入响应式 smoke。
- 阶段 13 扩展 `message_card()` 支持可选纯文本标题，保留脚本拉取/导入结果卡的“结果”标题；`fls_manager/routes/scripts/pull.py` 使用显式 `msg_kind` / `msg_strong` 接入成功、错误和空状态，并补充 `/pull/fetch`、`/pull/import` 路由测试及响应式 smoke。
- 阶段 14 新增 `summary_item(label, value)`，只替换在线脚本页 3 个统计项并保留外层 `.fls-summary-grid`；组件内部统一转义 label/value，并补充结构、数字 value 和 HTML 转义单元测试。
