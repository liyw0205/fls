# FLS 数据 Schema 与读取迁移

更新时间：2026-07-04

本文记录 `data/*.json` 的当前结构和读取时归一化规则。FLS 仍采用轻量 JSON 文件存储；结构演进优先通过读取时兼容迁移完成，不引入数据库迁移层。

## 通用规则

- JSON 读写统一走 `fls_manager/storage.py`。
- 业务读取优先走 `fls_manager/models.py` 或 `fls_manager/config.py`。
- 新增字段必须提供默认值；删除旧字段时先在读取时迁移，不能直接让旧数据报错。
- 环境隔离测试必须在导入 `fls_manager.*` 前设置临时 `FLS_BASE_DIR`。
- 当前读取时会规范化 `tasks.json`、`task_history.json`、`global_env.json`、`proxies.json`、`collections.json`；配置读取由 `normalize_config_data()` 规范化返回值。

## data/backup_jobs.json

类型：以作业 ID 为键的备份作业快照对象。

- `id`、`items`、`type_text`：作业标识和备份范围。
- `running`、`status`：当前状态；终态包括 `已完成`、`失败`、`超时`、`已取消`，面板重启后无法确认的运行中作业会归一为 `结果未知`。
- `filename`、`size`、`size_text`：已完成归档信息；失败、超时和取消会清空这些字段。
- `error`：失败、超时或结果未知时的原始诊断信息。
- `created_at`、`updated_at`：作业时间戳。

快照不包含线程、取消事件或进程句柄。最多保留最近 100 个作业；读取到 `running=true` 的旧快照时，
系统会幂等改写为 `结果未知`，不会声称外部备份进程已经完成或退出。

## data/online_install_jobs.json

类型：以安装 ID 为键的在线脚本安装作业快照对象。

- `id`、`script_id`、`script_name`：安装和脚本标识。
- `log_file`：安装日志路径；日志文件本身仍按日志读取规则处理。
- `running`、`status`：准备中、运行中、停止中、停止失败或超时（停止失败）时为活跃状态；终态为已完成、失败、启动失败、超时、已停止或结果未知。
- `returncode`、`error`：真实进程退出码和原始诊断；无进程退出码时为 `null`。重启后失去进程句柄的作业归一为 `结果未知`，保留原诊断并追加重启说明。
- `start_time`、`updated_at`：作业时间戳。

快照不包含线程、停止集合或进程句柄，最多保留最近 100 个作业。安装日志 API 对真实不存在的 ID 返回
`404`；已持久化但重启后无法确认的运行中作业返回 `200` 和明确的 `结果未知`。

## data/online_script_refresh.json

类型：单个在线脚本源刷新快照对象。

- `id`：本次刷新 ID；同一面板进程内用于并发占位和状态关联。
- `running`、`status`：运行中或终态；终态包括 `已完成`、`失败`、`启动失败` 和 `结果未知`。
- `message`、`error`：成功消息或保留原始失败原因。网络超时仍归入失败状态，错误文本不会被改成模糊提示。
- `updated_at`、`log_file`：最后状态更新时间和刷新日志路径。

刷新请求先在锁内登记并原子持久化运行中快照，再启动后台线程，因此重复请求可确定返回冲突而不会覆盖任务。
线程启动异常会写入 `启动失败`；面板重启读取到 `running=true` 时幂等转换为 `结果未知`，不会推断远程源
是否已经更新。刷新得到的新脚本列表先写入临时文件，再用原子替换发布，抓取或发布失败时旧缓存保持不变。

## data/tasks.json

类型：任务对象数组。

规范字段：

- `id`：字符串，任务唯一 ID。缺失时读取迁移会生成新 ID。
- `name`：字符串，任务名。缺失时用 `command`，再兜底为 `未命名任务`。
- `remark`：字符串，备注。
- `command`：字符串，任务命令。
- `cron`：字符串，空字符串表示手动任务；支持 5 位或 6 位 Cron。
- `config_path`：字符串，相对 `scripts/` 的配置文件路径。
- `collection_id`：字符串，所属合集 ID，空字符串表示不属于合集。
- `enabled`：布尔值，是否启用调度。
- `env`：对象，任务级环境变量，键和值都会归一为字符串，空键会被丢弃。
- `proxy_id`：字符串，代理 ID。
- `notify`：对象，结构为 `{"mode": "none|default|custom", "ids": []}`。
- `random_delay`：对象，结构为 `{"mode": "none|default|custom", "seconds": 0}`，`seconds` 范围 `0-120`。
- `retry`：对象，结构为 `{"attempts": 0, "interval_seconds": 60}`；`attempts` 范围 `0-5`，`interval_seconds` 范围 `5-3600`。
- `run_count`：非负整数。
- `pinned`：布尔值。
- `created_at`：字符串，创建时间。
- `updated_at`：字符串，更新时间。
- `last_run_at`：可选字符串，最后运行时间。

读取迁移：

- 非数组数据返回空数组并在保存时写回规范数组。
- 非对象任务会被丢弃。
- 旧字段 `notify_ids` 会迁移为 `notify`：
  - 包含 `__none__` => `{"mode": "none", "ids": []}`
  - 包含 `__default__` => `{"mode": "default", "ids": []}`
  - 其他 ID => `{"mode": "custom", "ids": [...]}`
- 缺失 `notify` 且没有 `notify_ids` 时按旧运行时行为归一为 `none`，避免给旧任务意外开启通知。
- 旧字段 `retry_count` 会迁移为 `retry.attempts`，写回时移除旧字段；新任务默认 `retry.interval_seconds=60`。
- `random_delay.seconds`、`retry.attempts`、`retry.interval_seconds`、`run_count` 会转整数并钳制到有效范围。
- `enabled`、`pinned` 支持布尔、数字和常见字符串值。
- 待执行的失败重试仅保存在运行时内存中；每个任务最多保留一个待重试计时器。手动或定时启动会使旧重试失效，停止、禁用或删除任务会取消待重试。

## data/task_history.json

类型：任务运行历史对象数组。

规范字段：

- `id`：字符串，历史记录 ID。
- `task_id`：字符串，任务 ID。
- `task_name`：字符串，任务名快照。
- `command`：字符串，任务命令快照。
- `status`：字符串，常见值包括 `success`、`failed`、`timeout`、`stopped`、`unknown`、`running`、`starting`、`delaying`、`start_failed`。`running`、`starting` 和 `delaying` 只表示面板当前进程仍持有运行状态；面板启动时会把没有结束时间的这些旧记录改为 `unknown`，因为重启后不能据此确认外部进程仍存活。
- `source`：字符串，运行来源，例如 `manual`、`cron`、`retry`。
- `retry_attempt`：整数，当前重试次数。
- `max_retries`：整数，最大重试次数。
- `start_at` / `end_at`：字符串，开始和结束时间。
- `duration_seconds`：数字或整数，耗时秒数。
- `return_code`：整数或 `null`。
- `log_file`：字符串，日志文件路径。
- `message`：字符串，状态说明。

读取和写入规则：

- 非数组数据读取为空数组。
- 非对象历史记录读取时会被过滤。
- `save_task_history()` 最多保留最近 `500` 条记录。
- `add_task_history()` 会把新记录插入到数组开头并按上限裁剪。
- `update_task_history()` 按 `id` 更新第一条匹配记录；找不到或传空 ID 时返回 `False`。
- 面板启动时会幂等恢复未结束的 `running`、`starting`、`delaying` 记录为 `unknown`，写入结束时间并保留原说明、日志路径和退出码；这不表示外部进程已被终止。
- `task_history_for_task(task_id, limit)` 按 `task_id` 过滤并按传入上限返回，最少返回上限为 `1`。

## data/config.json

类型：配置对象。

核心字段：

- `admin_token`：字符串，管理 Token。
- `security_verify_enabled`：布尔值，是否开启二次验证。
- `security_verify_type`：字符串，`code` 或 `totp`。
- `totp_secret`：字符串，TOTP 密钥。
- `port`：整数，范围 `1-65535`。
- `log_cleanup_minutes`：整数，范围 `1-1440`。
- `log_max_size_mb`：整数，最小 `1`。
- `log_keep_per_task`：整数，最小 `1`。
- `task_timeout_seconds`：整数，最小 `0`。
- `random_delay_seconds`：整数，范围 `0-120`。
- `timezone_offset_hours`：整数，范围 `-23` 到 `23`。
- `panel_time_offset_seconds`：整数，面板虚拟时间偏移秒数。
- `online_script_source`：字符串，在线脚本源地址。
- `task_types`：对象，当前支持 `py`、`sh`、`js`、`ts`、`ps1`、`bat`、`php`、`rb`、`pl`、`lua`、`jar`。
- `notify_items`：通知渠道数组，由 `fls_manager/notify.py` 继续归一化。
- `notify_default_ids`：默认通知渠道 ID 数组，由 `fls_manager/notify.py` 过滤可用渠道。

读取迁移：

- 非对象配置按默认配置处理。
- 读取时合并 `DEFAULT_CONFIG`。
- 核心数值字段会转整数并钳制范围。
- `security_verify_type` 非法时回退 `code`。
- `online_script_source` 为空时回退默认源。
- `task_types` 只保留当前支持的类型键，值归一为布尔。
- 未知顶层字段暂时保留，避免破坏后续扩展或用户自定义字段。

## data/global_env.json

类型：对象，键值均为字符串。

示例：

```json
{
  "TOKEN": "abc",
  "DEBUG": "1"
}
```

读取迁移：

- 非对象数据归一为空对象。
- 键会转字符串并去除首尾空白。
- 空键会被丢弃。
- 值会转字符串；`null` 会转为空字符串。

## data/proxies.json

类型：代理对象数组。

规范字段：

- `id`：字符串，代理唯一 ID。缺失时读取迁移会生成新 ID。
- `name`：字符串，代理名称，缺失时为 `未命名代理`。
- `type`：字符串，支持 `http`、`https`、`socks4`、`socks5`、`socks5h`、`github`。
- `host`：字符串，普通代理主机。
- `port`：字符串，普通代理端口。
- `username`：字符串，用户名。
- `password`：字符串，密码。
- `url`：字符串，GitHub 代理地址。
- `enabled`：布尔值。
- `created_at`：字符串，创建时间。
- `updated_at`：字符串，更新时间。

读取迁移：

- 非数组数据返回空数组。
- 非对象代理会被丢弃。
- 非法 `type` 回退为 `http`。
- `enabled` 支持布尔、数字和常见字符串值。
- 文本字段统一转字符串并去除首尾空白。

## data/collections.json

类型：合集对象数组。

规范字段：

- `id`：字符串，合集唯一 ID。缺失时该行会被丢弃。
- `name`：字符串，合集名称，缺失时为 `未命名合集`。
- `remark`：字符串，备注。
- `created_at`：字符串，创建时间。
- `updated_at`：字符串，更新时间。

读取迁移：

- 非数组数据返回空数组。
- 非对象合集会被丢弃。
- 缺失 `id` 的合集会被丢弃，避免任务引用无法稳定匹配。
- 文本字段统一转字符串并去除首尾空白。

## 测试入口

当前 schema 迁移测试在 `tests/test_schema_migration.py`。

常用命令：

```sh
python -B -m unittest tests.test_schema_migration
python -B -m unittest discover -s tests
```
