# FLS 开发文档

更新时间：2026-07-06
基线：`main` / 阶段 48

本文是 FLS 当前代码库的开发协作文档。历史阶段流水见
`docs/DEVELOPMENT_PROGRESS.md`，下一轮接续信息见
`docs/SESSION_HANDOFF.md`，数据结构细节见 `docs/DATA_SCHEMA.md`。

## 1. 项目定位

FLS = Flask Lightweight Script Manager，是一个基于 Flask 的轻量级脚本任务管理面板。

核心能力：

- 通过 Web 面板管理脚本、任务、合集、全局变量、代理、通知、日志、依赖和备份恢复。
- 支持手动任务和 Cron 定时任务。
- 支持 Python、Shell、Node.js、TypeScript、PowerShell、Batch、PHP、Ruby、Perl、Lua、Jar 等脚本类型。
- 支持 Linux、Windows、Termux，并提供不同平台的启停脚本。

架构原则：

- 保持 Flask + 原生 CSS/JS，不引入 npm 构建链。
- 页面主要由 Flask 路由拼接 HTML，公共布局在 `fls_manager/ui/layout.py`。
- 静态增强逻辑在 `fls_manager/static/fls.js` 和 `fls_manager/static/fls.css`。
- 优先小步改动、标准库测试、运行数据隔离和可回滚边界。

产品参考对象：

- 青龙面板：任务、脚本、环境变量、日志和通知的信息架构。
- 呆呆面板：轻量、现代、开箱即用的功能组织。
- 白虎面板：低资源占用、多运行时、节点互联等后续方向。

参考只用于产品行为和信息架构，不改变 FLS 当前轻量技术路线。

## 2. 启动入口

主入口：

- `fls-manager.py`：Python 主入口，负责依赖自检、创建 Flask app、加载调度器、清理日志并启动服务。
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

自动检查依赖：

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
- `FLS_PYTHON` / `FLS_NODE` / `FLS_BASH`：覆盖任务运行时解释器。

首次运行如果没有 Token，会跳转 `/setup` 初始化。

## 3. 目录与模块边界

根目录：

- `README.md`：用户说明。
- `AI-NOTICE.md`：AI 相关说明。
- `DEVELOPMENT.md`：当前开发协作文档。
- `docs/`：数据 schema、阶段进度、交接文档和参考资料。
- `fls_manager/`：核心应用代码。
- `tests/`：标准库 `unittest` 测试。
- `tools/responsive_smoke.py`：无浏览器环境下的响应式结构烟测。
- `data/` / `log/` / `scripts/`：运行数据、日志和用户脚本，通常不提交真实本地内容。

核心模块：

- `fls_manager/paths.py`：工作目录探测和 `data/log/scripts` 路径常量。
- `fls_manager/app.py`：创建 Flask app、配置 session、注册 Blueprint。
- `fls_manager/auth.py`：登录态、Token、API 鉴权入口。
- `fls_manager/csrf.py`：CSRF 校验和 token 注入约定。
- `fls_manager/security.py`：随机验证码和 TOTP 二次验证。
- `fls_manager/config.py`：默认配置、配置合并、端口、Token、虚拟时间。
- `fls_manager/storage.py`：JSON 读写和进程内锁。
- `fls_manager/models.py`：任务、历史、全局变量、代理、合集的数据访问与归一化。
- `fls_manager/command.py`：任务命令解析、脚本类型归一、混合命令展开、`fls_kill` 注入。
- `fls_manager/task_runner.py`：任务启动、停止、超时、失败重试、日志和通知。
- `fls_manager/scheduler.py`：Cron 解析、虚拟时间调度、APScheduler job 重载。
- `fls_manager/logs.py`：任务日志文件、tail、任务名解析和日志清理。
- `fls_manager/proxy.py`：HTTP/SOCKS/GitHub 代理配置、检测和任务环境注入。
- `fls_manager/notify.py`：通知渠道模型和发送实现。
- `fls_manager/state.py`：全局运行态，包括 scheduler、运行中任务和依赖安装任务。
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
- `fls_manager/routes/api.py`：任务状态、调度器状态、任务动作和批量任务 API。

Blueprint 约定：

- 单文件功能可直接在 `routes/<name>.py` 中创建 `bp = Blueprint(...)`。
- 多文件功能使用 `routes/<domain>/bp.py` 暴露 `bp`，在 `routes/<domain>/__init__.py` 导入子模块让 `@bp.route` 生效。
- 新增功能后必须在 `fls_manager/app.py` 注册 Blueprint。
- 新增导航入口时同步更新 `fls_manager/ui/layout.py`。

## 4. 数据模型

所有运行数据默认位于 `BASE_DIR/data`。`BASE_DIR` 来自 `FLS_BASE_DIR`，否则按平台推断：

- Windows：`C:/fls`
- Termux：`$HOME/fls`
- Linux root 优先：`/root/fls`
- 兜底：`$HOME/fls`

主要数据文件：

- `data/config.json`：系统配置、通知配置、线上脚本源等。
- `data/tasks.json`：任务列表。
- `data/task_history.json`：任务运行历史。
- `data/global_env.json`：全局环境变量。
- `data/proxies.json`：代理配置。
- `data/collections.json`：任务合集。
- `data/fls-manager.pid`：面板进程 PID。
- `data/secret_key.txt`：持久化 Flask secret_key。
- `data/fls_code.json`：随机二次验证码临时文件。

读写规则：

- JSON 读写统一走 `fls_manager/storage.py`。
- 业务数据访问优先走 `fls_manager/models.py`。
- `tasks.json`、`task_history.json`、`global_env.json`、`proxies.json`、`collections.json` 读取时通过 `models.py` 做归一化。
- `config.json` 读取时通过 `config.normalize_config_data()` 合并默认值、转换类型并钳制范围。
- 写文件使用临时文件替换，减少半写入文件。
- 当前锁是进程内 `threading.RLock`，不保证多进程事务一致性。
- 不要在路由中直接读写 `data/*.json`，除非是在补充专门的数据访问函数。

任务核心字段：

- `id`：任务 ID，`uuid.uuid4().hex`。
- `name` / `remark` / `command` / `cron` / `config_path` / `collection_id`。
- `enabled` / `pinned` / `run_count`。
- `env`：任务级环境变量。
- `proxy_id`：代理 ID。
- `notify`：`{"mode": "default|none|custom", "ids": []}`。
- `random_delay`：`{"mode": "none|default|custom", "seconds": 0}`。
- `retry`：`{"attempts": 0, "interval_seconds": 60}`，`attempts` 范围 `0-5`，`interval_seconds` 范围 `5-3600`。
- `created_at` / `updated_at` / `last_run_at`。

兼容迁移：

- 旧 `notify_ids` 会读取迁移为 `notify`，写回时移除旧字段。
- 旧 `retry_count` 会读取迁移为 `retry`，写回时移除旧字段。
- 坏类型、缺失字段和空 ID 在读取时归一化，详细规则见 `docs/DATA_SCHEMA.md`。

## 5. 请求、鉴权与安全

`create_app()` 在注册路由前设置：

- 持久化 `secret_key`。
- `PERMANENT_SESSION_LIFETIME`。
- `SESSION_COOKIE_HTTPONLY=True`。
- `SESSION_COOKIE_SAMESITE="Lax"`。
- `app.before_request(csrf_before_request)`。
- `app.before_request(auth_before_request)`。

CSRF 约定：

- `layout()` 会为 POST 表单注入隐藏字段 `csrf_token`，并在页面 `<meta name="csrf-token">` 输出同一个 token。
- `fls_manager/static/fls.js` 会为同源非 GET `fetch()` 自动补 `X-CSRF-Token`。
- 普通 session POST 必须通过 CSRF 校验。
- `X-Token` 命中管理 Token 的机器请求跳过 CSRF，便于 API 和脚本调用。
- 破坏性页面路由必须使用 POST，不允许 GET 删除、置顶、取出、停止、切换或运行任务。

鉴权优先级：

1. 静态资源和认证路由放行。
2. 未配置 Token 时 API 返回 JSON 403，页面跳转 `/setup`。
3. `X-Token` 命中 Token 时直接放行。
4. URL `?token=` 命中 Token 时写入 session 并重定向到清理后的 URL。
5. session Token 有效且二次验证通过时放行。
6. API 返回 JSON 错误，页面跳转登录页。

二次验证：

- 配置项：`security_verify_enabled`、`security_verify_type`、`totp_secret`。
- 支持随机验证码和 TOTP。
- 随机验证码默认 300 秒过期。

## 6. 当前行为边界

任务 API：

- `/api/task/action/run/<id>` 缺失任务返回 404。
- `/api/task/action/stop/<id>` 缺失任务返回 404 且不调用停止逻辑；已存在但未运行仍返回 200 + `ok:false`。
- `/api/task/action/delete/<id>` 缺失任务返回 404；存在任务删除前必须先停止任务，停止成功或任务未运行才删除，停止失败返回 409 且不写回。
- `/api/task/bulk-action` 保持 `ok/msg` 兼容字段，并返回 `action/count` 及结构化计数。
- 批量删除前逐个停止任务；停止失败的任务不能被删除，响应包含 `failed_count` / `failures`。

兼容页面动作：

- `/task/delete/<id>` 只接受 POST；停止失败时返回 409，渲染错误卡片，保留任务并避免写回。
- `/task/toggle/<id>` 只接受 POST；缺失任务返回 404 且不写回任务文件或重载调度器。
- `/run/<id>` 只接受 POST；缺失任务返回 404，普通运行失败返回 400，失败时渲染错误卡片；日志页、合集页和配置页不能渲染 GET 运行链接。
- `/stop/<id>` 只接受 POST；缺失任务返回 404 且不调用停止逻辑；任务未运行仍重定向，非未运行停止失败返回 409 并渲染错误卡片。
- `/task/pin/<id>` 只接受 POST；缺失任务返回 404 且不写回；最多 5 个置顶任务，超过上限时渲染错误卡片且不写回。
- `/task/collection/clear/<id>` 只接受 POST；缺失任务返回 404；任务已经不在合集时不写 `tasks.json`。
- `/collection/delete/<id>` 只接受 POST；缺失合集返回 404 且不写入；删除空合集不写 `tasks.json`；删除含任务合集时才清理任务归属。
- `/collection/add-task/<id>` 只接受 POST；兼容 `task_ids` 多选和旧 `task_id` 单选；混入缺失任务时返回 404 且不部分写入。

日志与备份：

- 单文件日志删除使用 POST。
- 日志分组批量删除使用 `/api/logs/groups/delete`。
- 备份解压必须拒绝路径穿越、绝对路径、链接和 tar 特殊成员。

返回路径：

- 返回跳转 URL 优先用 `utils.get_back_url()` 或同等校验。
- 不允许外部 URL、协议相对 URL 或非站内路径作为回跳目标。

## 7. 任务执行链路

创建或编辑任务：

1. 路由解析表单并校验必填字段。
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
3. 创建任务日志文件和任务历史记录。
4. 写入 `RUNNING` 状态。
5. 后台线程启动任务。
6. worker 合并系统环境、全局变量、任务变量、代理变量和 `FLS_TASK_*`。
7. watcher 等待进程结束，处理超时、失败重试、历史收尾、通知和运行态清理。

停止：

1. `stop_task_now(task_id)` 从 `RUNNING` 查找进程。
2. 未运行返回 `False, "任务未运行"`。
3. 存在进程时调用 `force_kill_process()`。
4. 写停止日志、更新历史、清理运行态。

## 8. 前端约定

- 继续使用 Flask 字符串拼接 HTML + 原生 CSS/JS。
- 页面输出用户可控字段必须显式 HTML escape，优先使用 `utils.h()`。
- 页面应走统一 `layout()`，不要绕过鉴权、CSRF 和静态资源注入。
- 新增 POST 表单用 `method="post"`，让 `layout()` 自动注入 CSRF。
- JS 同源非 GET `fetch()` 依赖 `fls.js` 自动补 `X-CSRF-Token`。
- 不引入 npm、Tailwind、Bootstrap 或其它构建链，除非先评估部署成本。
- 响应式至少考虑 390px、768px、1024px、1440px。
- 任务列表和合集页的破坏性操作应保留 POST/API 方案，不能退回 GET 链接。

现有 UI 组件：

- `fls_manager/ui/components.py`
  - `page_header_card()`
  - `table_card()`
  - `pagination_card()`
  - `message_card()`
  - `summary_item()`
- `fls_manager/ui/tables.py`
  - 任务表格、移动端任务卡片、批量工具栏和任务操作按钮。
- `fls_manager/ui/log_controls.py`
  - 日志页控制按钮。

## 9. 测试策略

测试原则：

- 使用标准库 `unittest`，不引入 pytest。
- 测试导入 `fls_manager.*` 前设置临时 `FLS_BASE_DIR`，避免污染真实数据。
- Flask test client 通过 `X-Token` 进行 API/页面请求。
- 测试结束后关闭 scheduler，并清理 `sys.modules` 中的 `fls_manager.*`。
- 网络、SMTP、子进程、线程、通知、代理检测等外部出口必须 mock。

当前覆盖重点：

- 鉴权、CSRF、Query Token 清理跳转、破坏性路由 GET 拒绝。
- 备份 zip/tar 安全解压。
- 命令解析、Cron、虚拟时间。
- JSON schema 读取迁移和配置归一化。
- 任务运行、停止、超时、失败重试、历史、通知和代理环境注入。
- 存储、通知出口、代理质量检测、日志 tail 和清理。
- 任务批量 API、任务动作 API、合集批量/单项边界、兼容运行/停止/删除/置顶入口错误提示。
- UI 组件和关键路由渲染、HTML 转义、安全 back。
- 响应式结构 smoke。

完整验证：

```sh
python -B -m unittest discover -s tests
python -B tools/responsive_smoke.py
python -B -m compileall fls-manager.py fls_manager tests tools
git -c safe.directory=/data/data/com.termux/files/home/fls diff --check
```

受限验证：

- 当前环境没有 Playwright/Chromium，真实浏览器截图检查需要在具备浏览器环境后补充。
- 推荐截图宽度：390px、768px、1024px、1440px。
- 推荐页面：`/tasks`、`/collections`、`/logs`、`/online-scripts`、`/pull`、`/config`、`/deps`、`/panel/status`。

## 10. 开发流程

每轮开发：

1. 读取 `docs/SESSION_HANDOFF.md`。
2. 执行 `git -c safe.directory=/data/data/com.termux/files/home/fls status --short --branch`。
3. 确认本地提交身份为 `liyw0205 <2650115317@qq.com>`。
4. 只选择一个可验证的窄边界开发。
5. 不整包恢复 `stash@{0}`。
6. 更新 `DEVELOPMENT.md`、`docs/DEVELOPMENT_PROGRESS.md`、`docs/SESSION_HANDOFF.md`。
7. 跑完整验证。
8. 提交并推送 `origin/main`。

Git 约束：

- 命令使用 `git -c safe.directory=/data/data/com.termux/files/home/fls ...`。
- 不使用破坏性 reset/checkout 覆盖用户改动。
- 不 amend 旧提交，除非用户明确要求。
- 当前长期 stash：`stash@{0}: pre-main-merge dirty task-log runtime changes`。
- 该 stash 是旧基线工作树快照，不应整包 `pop/apply`；只能从中摘取可证明的窄边界。

不要迁入的旧方向：

- 旧 `retry_count` 表单。
- GET 删除、置顶、取出、停止、切换、运行。
- 移除 CSRF。
- 移除任务运行历史。
- 移除 back 清洗和合集锚点。

## 11. 已知约束

- JSON 存储适合轻量单实例部署，不适合多进程高并发写入。
- APScheduler 是进程内调度，面板进程退出后不会继续调度。
- 当前没有数据库迁移层，数据结构演进必须在读取时兼容归一。
- 页面由字符串拼接 HTML，新增复杂页面时要特别注意转义和可维护性。
- CodeMirror 资源来自 CDN，离线环境下编辑器增强可能不可用。
- `totp_qr_url()` 使用在线二维码服务，离线环境只能展示密钥或 otpauth 链接。
- 依赖安装、运行时安装、在线脚本源刷新依赖外部网络或代理配置。

## 12. 后续方向

优先级高：

- 继续维护 `docs/DATA_SCHEMA.md`，新增或调整数据字段时同步读取迁移函数。
- 把过长路由中的业务流程逐步下沉到 service/helper。
- 在具备浏览器环境时补真实截图响应式验收。

优先级中：

- 给配置、任务、代理、通知等 JSON 写入增加备份或回滚机制。
- 继续抽取可复用 UI 组件，减少路由中的 HTML 重复。
- 增加运行中任务状态 API 字段，例如耗时、attempt、source。
- 改善离线环境下 CodeMirror 和 TOTP 二维码资源可用性。
- 梳理 Open API、订阅管理、运行时管理和后续跨节点能力。

优先级低：

- 评估是否引入轻量模板层组织复杂页面。
- 评估是否引入 SQLite 作为可选存储后端。
- 增加更细的操作审计日志。
