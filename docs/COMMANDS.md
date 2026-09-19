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

Termux 专用变量见 [TERMUX.md](TERMUX.md)，KernelSU/Magisk 容器变量见
[ANDROID_MODULE.md](ANDROID_MODULE.md)。

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
