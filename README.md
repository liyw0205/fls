# FLS

<p align="center">
  <img src="https://raw.githubusercontent.com/liyw0205/fls/main/fls_manager/static/favicon-generated.png" alt="FLS 面板图标" width="128" height="128">
</p>

<p align="center">
  <b>FLS = Flask Lightweight Script Manager</b><br>
  一个轻量级的脚本任务管理面板
</p>

<p align="center">
  <a href="https://github.com/liyw0205/fls/stargazers"><img src="https://img.shields.io/github/stars/liyw0205/fls?style=flat-square" alt="stars"></a>
  <a href="https://github.com/liyw0205/fls/releases"><img src="https://img.shields.io/github/v/release/liyw0205/fls?style=flat-square" alt="release"></a>
  <a href="https://github.com/liyw0205/fls/blob/main/LICENSE"><img src="https://img.shields.io/github/license/liyw0205/fls?style=flat-square" alt="license"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20Android%20%7C%20Termux-blue?style=flat-square" alt="platform">
  <img src="https://img.shields.io/badge/python-3.x-brightgreen?style=flat-square" alt="python">
</p>

FLS 提供 Web 面板来管理脚本、定时任务、日志、依赖、代理、通知和备份。项目地址：
<https://github.com/liyw0205/fls>

## 先看部署区别

仓库中有两条 Android 容器链路，它们的运行位置和用途完全不同：

| 入口 | 适用环境 | 容器/项目位置 | 是否依赖 Termux | 用途 |
| --- | --- | --- | --- | --- |
| `fls-t.sh` | Termux，或 Android 外部 shell 调用 Termux | 运行时 `$HOME/.fls-runtime`，项目 `$HOME/fls` | 是 | Termux 专用 proot 部署 |
| `fls-a.sh` | KernelSU / Magisk 模块环境 | 运行时 `/data/fls/runtime`，项目 `/data/fls/project` | 否 | 独立的 `/data/fls` proot 部署 |

`fls-t.sh` 可以从 Android 外部调用，但它仍然连接到 Termux 的 HOME/PREFIX；这不等于
KernelSU/Magisk 容器。`fls-a.sh` 使用模块安装的独立运行时，不读取 Termux 的 Python、项目或
数据目录。

模块构建使用 `packaging/magisk/fls-a.sh`，安装后复制为 `/data/adb/fls-a.sh`；仓库根目录的
`fls-a.sh` 与模块入口保持相同实现，便于查看和手动测试。两者都不是 Termux 入口。

## 功能

- Python、Shell、Node.js、TypeScript、PowerShell、Batch、PHP、Ruby、Perl、Lua、Jar 任务
- Cron 定时、超时控制、随机延迟、任务合集、任务启停和运行日志
- 在线脚本导入、编辑、依赖管理、代理管理、通知推送、备份恢复
- Linux、Windows、Termux 原生运行
- Termux proot 运行时和 KernelSU/Magisk 独立 proot 运行时
- Linux systemd、Termux:Boot、KernelSU/Magisk `service.d`、Windows 计划任务自启
- GitHub Actions 打包 KernelSU/Magisk 模块和固定 proot 运行时

## 项目结构

```text
fls/
├─ fls-manager.py                 # 面板主入口
├─ fls.sh                         # Linux/Termux 原生控制脚本
├─ fls.ps1                        # Windows PowerShell 控制脚本
├─ fls.bat                        # Windows CMD 入口
├─ fls-t.sh                       # Termux proot 入口
├─ fls-a.sh                       # /data/fls 容器入口
├─ fls_manager/                   # Flask 面板代码
├─ data/                          # 配置、任务、通知等运行数据
├─ log/                           # 面板和任务日志
├─ scripts/                       # 用户脚本
├─ packaging/magisk/              # KernelSU/Magisk 模块模板
├─ packaging/proot/               # proot 运行时构建脚本
└─ .github/workflows/             # 模块和运行时工作流
```

运行中的数据目录会被保留。更新程序时不要删除 `data/`、`log/` 和 `scripts/`。

## Linux 安装

以 Debian/Ubuntu 为例：

```bash
apt update
apt install -y python3 python3-pip python3-venv git
git clone https://github.com/liyw0205/fls.git
cd fls
chmod +x fls.sh
sh fls.sh start
```

默认访问 `http://服务器IP:5700`。临时指定端口和 Token：

```bash
sh fls.sh start -p 5701 -t 123456
```

`fls.sh` 支持 `start`、`stop`、`restart`、`status`、`log`、`update`、`clone`、
`ensure-repo`、`bstart` 和 `rstart`。`FLS_BASE_DIR` 可以指定项目目录，`FLS_PYTHON` 可以
指定 Python 解释器。

## Termux：`fls-t.sh`

这是 Termux 专用的 proot 部署。运行时会从固定的 `proot-runtime` Release 下载到
`$HOME/.fls-runtime`，项目默认位于 `$HOME/fls`，不会写入 `/data/fls`。

### Termux 内安装

```bash
pkg update -y
pkg install -y curl git
git clone https://github.com/liyw0205/fls.git "$HOME/fls"
sh "$HOME/fls/fls-t.sh" start
```

首次执行会按 ABI 下载 `python` 运行时。需要更多 Linux 工具时选择 `all`：

```bash
printf '%s\n' all > "$HOME/.fls-profile"
sh "$HOME/fls/fls-t.sh" restart
```

### Android 外部调用 Termux

模块安装后可直接使用 `/data/adb/fls-t.sh`；也可以把仓库中的脚本放到该路径：

```bash
su -c 'sh /data/adb/fls-t.sh start'
su -c 'sh /data/adb/fls-t.sh status'
```

脚本默认定位 `com.termux` 的 HOME/PREFIX。其他包名或多用户路径使用环境变量覆盖：

```bash
su -c 'TERMUX_PACKAGE=com.termux TERMUX_HOME=/data/data/com.termux/files/home \
  TERMUX_PREFIX=/data/data/com.termux/files/usr sh /data/adb/fls-t.sh restart'
```

常用变量：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `TERMUX_PACKAGE` | `com.termux` | Termux 包名 |
| `TERMUX_HOME` | 根据包名推导 | Termux HOME |
| `TERMUX_PREFIX` | 根据包名推导 | Termux PREFIX |
| `FLS_BASE_DIR` | `$TERMUX_HOME/fls` | Termux 容器中的项目目录 |
| `FLS_RUNTIME_DIR` | `$TERMUX_HOME/.fls-runtime` | Termux 容器运行时 |
| `FLS_PROOT_PROFILE` | `python` | `python` 或 `all` |
| `FLS_REPO_URL` | FLS GitHub 仓库 | 项目仓库地址 |

支持命令：

```bash
sh fls-t.sh start [-p 5700] [-t 123456]
sh fls-t.sh stop
sh fls-t.sh restart [-p 5700] [-t 123456]
sh fls-t.sh status
sh fls-t.sh log
sh fls-t.sh update
sh fls-t.sh clone
sh fls-t.sh ensure-repo
sh fls-t.sh bstart
sh fls-t.sh rstart
```

`bstart`/`rstart` 管理的是 Termux:Boot；它们不创建 KernelSU/Magisk 的
`service.d` 服务。

## KernelSU / Magisk：`fls-a.sh`

该入口使用模块自己的 proot 和 Python，不需要安装或启动 Termux。模块安装完成后：

- 控制脚本：`/data/adb/fls-a.sh`
- 模块目录：通常为 `/data/adb/modules/fls-manager`
- 运行时：`/data/fls/runtime`
- 项目：`/data/fls/project`
- 数据：`/data/fls/project/data`
- 日志：`/data/fls/project/log` 和 `/data/fls/fls-a.log`

安装模块后手动控制：

```bash
su -c 'sh /data/adb/fls-a.sh status'
su -c 'sh /data/adb/fls-a.sh start'
su -c 'sh /data/adb/fls-a.sh stop'
su -c 'sh /data/adb/fls-a.sh restart -p 5701 -t 123456'
su -c 'sh /data/adb/fls-a.sh log'
su -c 'sh /data/adb/fls-a.sh update'
```

模块服务启动时会把项目同步到 `/data/fls/project`，保留该目录中的 `data/`、`log/` 和
`scripts/`。模块卸载也不会删除 `/data/fls`，重新安装时可继续复用数据。

### 运行时选择

默认使用轻量 `python` 运行时。安装前或切换运行时前写入 `all`，然后重启设备或手动重启
服务：

```bash
su -c 'mkdir -p /data/fls && printf all > /data/fls/profile'
su -c 'sh /data/adb/fls-a.sh restart'
```

运行时资源来自固定标签 `proot-runtime`：

```text
fls-proot-python-arm64.tar.gz
fls-proot-all-arm64.tar.gz
fls-proot-python-armv7.tar.gz
fls-proot-all-armv7.tar.gz
```

### 模块说明状态

模块的 `/data/adb/modules/fls-manager/module.prop` 中的 `description` 会在面板启动、停止、
重启和状态查询时更新，显示：

```text
🟢 运行中 | 端口: 5700 | PID: 12345
🔴 已停止 | 端口: 5700 | PID: -
```

这段说明是 KernelSU/Magisk 模块管理页面显示的当前面板状态，不代表 Termux 容器状态。

## Windows

安装 Python 和 Git 后：

```powershell
git clone https://github.com/liyw0205/fls.git
cd fls
.\fls.ps1 start
```

也可以执行 `fls.bat start`，双击 `fls.bat` 或执行 `.\fls.ps1 menu` 打开交互菜单。支持
`start`、`stop`、`restart`、`status`、`log`、`update`、`clone`、`ensure-repo`、
`bstart` 和 `rstart`。

## 配置与登录

- 默认端口：`5700`
- 启动参数：`-p/--port` 和 `-t/--token`
- 也可以使用环境变量 `FLS_PORT`、`FLS_TOKEN`、`FLS_BASE_DIR`、`FLS_PYTHON`
- 未配置 Token 时，首次访问会进入 `/setup` 设置

示例：

```bash
FLS_PORT=5701 FLS_TOKEN=123456 sh fls.sh restart
```

面板启动后访问：

```text
http://设备IP:端口
```

## GitHub Actions 与发布

### 模块工作流

`.github/workflows/package-module.yml` 只生成 `fls-manager-module.zip`。ZIP 根目录直接包含：

```text
module.prop
customize.sh
service.sh
action.sh
uninstall.sh
runtime.sh
runtime.env
fls-a.sh
fls-t.sh
version.json
changelog.md
webroot/index.html
fls/
```

不会再包一层 ZIP，也不会生成 `payload/` 嵌套目录。普通推送只有 `version.json` 发生变化时
才自动打包；`workflow_dispatch` 可以手动打包；推送 `v*` 标签时才创建模块 Release。

### 固定 proot 工作流

`.github/workflows/build-proot.yml` 只支持手动触发，四个运行时统一发布到固定标签
`proot-runtime`，不会因为普通提交重复构建。模块安装时按架构和 `/data/fls/profile` 下载
已有资源。

### 版本文件

`version.json` 的 `versionCode` 和 `version` 用于 KernelSU/Magisk 更新判断；
`changelog.md` 是更新日志。发布模块时应保持版本标签与 `version.json.version` 一致：

```bash
git tag v20260920.3
git push origin v20260920.3
```

## 数据和更新

源码更新只替换程序文件，不应覆盖：

```text
data/
log/
scripts/
```

Termux 容器执行 `sh fls-t.sh update` 或 `clone`；KernelSU/Magisk 容器执行
`sh /data/adb/fls-a.sh update` 或 `clone`。两条链路的项目和数据目录互相独立，更新时不要把
Termux 的 `$HOME/fls` 与 `/data/fls/project` 混用。

## 开发与测试

本地测试：

```bash
PYTHONPATH=. pytest -q
python3 -m compileall -q fls_manager
git diff --check
```

Web 界面变更应在当前 Termux 浏览器环境同时验证手机端和桌面端：登录、侧边栏切换、任务
启动/停止、日志弹窗、模块状态显示和更新提示，并检查布局错位、按钮重复触发、功能互相
干扰及错误状态。测试完成后再提交代码和工作流变更。

## License

详见 [`LICENSE`](LICENSE)。
