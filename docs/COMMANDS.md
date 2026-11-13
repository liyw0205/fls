# 命令与配置

## 通用命令

`fls.sh`、`fls-t.sh` 和模块里的 `fls-a.sh` 都支持以下核心动作：

| 命令 | 作用 |
| --- | --- |
| `start` | 启动面板 |
| `stop` | 停止面板 |
| `restart` | 重启面板 |
| `status` | 查看状态、目录、端口和 PID |
| `log` | 查看实时面板日志 |
| `update` | 拉取仓库更新 |
| `clone` | 强制重新拉取仓库并覆盖程序文件 |
| `ensure-repo` | 程序不完整时才拉取仓库 |
| `bstart` | 创建并启用对应平台的自启 |
| `rstart` | 移除对应平台的自启并重启 |

命令只替换程序文件，`data/`、`log/` 和 `scripts/` 应保留。

## 端口和 Token

启动参数：

```bash
sh fls.sh start -p 5701 -t 123456
sh fls.sh restart --port 5701 --token 123456
```

环境变量：

```bash
FLS_PORT=5701 FLS_TOKEN=123456 sh fls.sh restart
```

默认端口为 `5700`。如果没有 Token，首次访问 `/setup` 设置管理 Token。

## 目录变量

原生 Linux/Termux 控制脚本：

| 变量 | 作用 |
| --- | --- |
| `FLS_BASE_DIR` | 覆盖项目目录 |
| `FLS_PYTHON` | 指定 Python 解释器 |
| `FLS_REPO_URL` | 覆盖仓库地址 |
| `FLS_REPO_BRANCH` | 覆盖仓库分支 |
| `FLS_PORT` | 面板端口 |
| `FLS_TOKEN` | 临时管理 Token |
| `FLS_TRUSTED_PROXIES` | 可提供 `X-Forwarded-For` 的代理 IP/CIDR，逗号分隔；默认不信任任何代理 |
| `FLS_ALLOW_QUERY_TOKEN` | 设为 `1` 临时启用 URL 查询 Token 兼容；默认禁用，优先使用登录页或 `X-Token` |

Termux 专用变量见 [TERMUX.md](TERMUX.md)，KernelSU/Magisk 容器变量见
[ANDROID_MODULE.md](ANDROID_MODULE.md)。

## 依赖与运行器安装

在面板“依赖管理”（`/deps`）安装 Python 包，或在“面板状态”（`/panel/status`）
安装运行器后，会进入同一个安装日志页。Windows 的运行器安装入口会跳转到官方下载页。

日志页会显示安装中、安装成功、安装失败或启动失败，以及进程退出码。退出码 `0`
表示安装命令成功结束；其他退出码表示失败，具体原因见日志。启动失败没有退出码，
页面会保留生成命令或启动进程时的错误。安装后可用“刷新依赖检测结果”检测 Python
核心依赖；运行器请返回“面板状态”确认可用性。

安装记录保存在面板进程内存中。面板重启或记录被淘汰后，原链接显示“无法追踪”；
旧记录缺少退出码时显示“结果未知”。这两种状态均不能证明安装成功，请到日志管理
查找 `deps-install-*.log` 或 `system-install-*.log`，并重新检测依赖或运行器。

日志 API `GET /api/deps/install-log/<install_id>` 保留 `running` 和 `log`，并提供：

| 字段 | 含义 |
| --- | --- |
| `status` | `running`、`succeeded`、`failed`、`start_failed`、`unknown` 或 `missing` |
| `status_label` | 对应的中文状态 |
| `returncode` | 进程退出码；尚未退出或无法取得时为 `null` |
| `error` | 失败或无法确认结果的原因；正常运行和成功时为空字符串 |

## 备份恢复限制

以下面板环境变量在启动时读取，设置后需重启 FLS：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `FLS_BACKUP_MAX_UPLOAD_MB` | 备份上传大小上限，正整数 MiB | `512` |
| `FLS_BACKUP_MAX_EXTRACTED_MB` | 归档解压后总大小上限，正整数 MiB | `2048` |
| `FLS_BACKUP_MAX_ARCHIVE_ENTRIES` | 归档成员数上限，正整数 | `10000` |

## `ensure-repo` 和 `clone`

- `ensure-repo`：检查 `fls-manager.py`、`fls_manager/` 和 `fls.sh`，缺失时才拉取。
- `clone`：不管当前是否完整，都从仓库重新拉取；适合本地文件损坏或更新失败。
- `update`：优先在现有 Git 仓库执行 fast-forward 更新；非 Git 目录会重新拉取。

## 自启位置

| 平台 | `bstart` 生成的位置 |
| --- | --- |
| Linux | `/etc/systemd/system/fls.service` |
| Termux | `~/.termux/boot/fls-start.sh` |
| KernelSU/Magisk | `/data/adb/service.d/fls-a.sh` |
| Windows | 计划任务 `FLS Manager` |
