# FLS

<p align="center">
  <b>FLS = Flask Lightweight Script Manager</b><br>
  一个轻量级、开箱即用的脚本任务管理面板
</p>

<p align="center">
  <a href="https://github.com/liyw0205/fls/stargazers"><img src="https://img.shields.io/github/stars/liyw0205/fls?style=flat-square" alt="stars"></a>
  <a href="https://github.com/liyw0205/fls/network/members"><img src="https://img.shields.io/github/forks/liyw0205/fls?style=flat-square" alt="forks"></a>
  <a href="https://github.com/liyw0205/fls/issues"><img src="https://img.shields.io/github/issues/liyw0205/fls?style=flat-square" alt="issues"></a>
  <a href="https://github.com/liyw0205/fls/blob/main/LICENSE"><img src="https://img.shields.io/github/license/liyw0205/fls?style=flat-square" alt="license"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20Termux-blue?style=flat-square" alt="platform">
  <img src="https://img.shields.io/badge/python-3.x-brightgreen?style=flat-square" alt="python">
</p>

---

## 项目简介

FLS 是一个基于 Flask 的轻量级脚本任务管理面板，支持通过 Web 页面管理脚本、定时任务、日志、代理、依赖、通知、备份恢复等功能。

项目地址：

- GitHub：<https://github.com/liyw0205/fls>

---

## 功能特点

- 支持 **Python / Shell / Node.js / TypeScript / PowerShell / Batch / PHP / Ruby / Perl / Lua / Java Jar**
- 支持 **Cron 定时任务**
- 支持 **任务超时控制**
- 支持 **随机延迟启动**
- 支持 **脚本拉取 / 导入 / 在线编辑 / 改名**
- 支持 **日志查看 / 日志管理 / 自动清理**
- 支持 **代理管理**
- 支持 **依赖管理**
- 支持 **通知推送**
- 支持 **备份恢复**
- 支持 **Linux / Windows / Termux**
- 支持 **KernelSU / Magisk 调用 Termux 环境启动**
- 支持 **Linux systemd 自启**
- 支持 **Termux:Boot 自启**
- 支持 **KernelSU / Magisk `/data/adb/service.d/` 自启**
- 支持 **Windows 计划任务自启**
- 自带 **启动 / 停止 / 重启 / 状态 / 日志 / 更新 / 强制 clone / 自启管理** 脚本

---

## 项目结构

```text
fls/
├─ fls-manager.py
├─ fls.sh
├─ fls.ps1
├─ fls.bat
├─ fls-a.sh
├─ fls_manager/
│  ├─ app.py
│  ├─ auth.py
│  ├─ command.py
│  ├─ config.py
│  ├─ logs.py
│  ├─ models.py
│  ├─ notify.py
│  ├─ paths.py
│  ├─ proxy.py
│  ├─ scheduler.py
│  ├─ state.py
│  ├─ task_runner.py
│  ├─ ui/
│  └─ routes/
├─ data/
├─ log/
├─ scripts/
└─ .venv/
```

### 目录说明

- `fls-manager.py`：主入口文件
- `fls_manager/`：模块化核心代码
- `fls.sh`：Linux / Termux 启停脚本
- `fls.ps1`：Windows PowerShell 启停脚本
- `fls.bat`：Windows CMD 启停入口
- `fls-a.sh`：KernelSU / Magisk / adb root 环境调用 Termux 的启动脚本
- `data/`：配置、任务、代理、通知等数据
- `log/`：运行日志
- `scripts/`：脚本目录
- `.venv/`：Python 虚拟环境

---

## 脚本说明

### `fls-manager.py`

主程序入口，负责启动模块化 FLS Web 面板。

---

### `fls.sh`

Linux / Termux 启停脚本。

支持：

```bash
sh fls.sh start
sh fls.sh stop
sh fls.sh restart
sh fls.sh status
sh fls.sh log
sh fls.sh update
sh fls.sh clone
sh fls.sh bstart
sh fls.sh rstart
sh fls.sh ensure-repo
```

临时参数：

```bash
sh fls.sh start -p 5701 -t 123456
sh fls.sh restart -p 5701 -t 123456
```

说明：

- 无参数时只显示帮助，不进入交互菜单。
- `ensure-repo` 会检查程序是否完整，不完整时才拉取仓库。
- `update` 会执行 `git pull` 更新程序。
- `clone` 会强制重新 clone 仓库并覆盖程序文件。
- `bstart` 会生成自启配置并重启 FLS。
- `rstart` 会移除自启配置并重启 FLS。

---

### `fls.ps1`

Windows PowerShell 启停脚本。

支持：

```powershell
.\fls.ps1 start
.\fls.ps1 stop
.\fls.ps1 restart
.\fls.ps1 status
.\fls.ps1 log
.\fls.ps1 update
.\fls.ps1 clone
.\fls.ps1 bstart
.\fls.ps1 rstart
.\fls.ps1 ensure-repo
.\fls.ps1 menu
```

说明：

- Windows 版本保留前台菜单。
- 双击或无参数默认进入菜单。
- 可以关闭窗口退出菜单，不影响已后台启动的 FLS 面板进程。
- `bstart` 会创建 Windows 计划任务实现开机自启。
- `rstart` 会移除 Windows 计划任务并重启 FLS。

---

### `fls.bat`

Windows CMD 启停入口。

支持：

```bat
fls.bat start
fls.bat stop
fls.bat restart
fls.bat status
fls.bat log
fls.bat update
fls.bat clone
fls.bat bstart
fls.bat rstart
fls.bat ensure-repo
fls.bat menu
```

说明：

- `fls.bat` 会调用同目录下的 `fls.ps1`。
- 建议将 `fls.bat` 和 `fls.ps1` 放在同一目录。

---

### `fls-a.sh`

KernelSU / Magisk / adb root 环境调用 Termux 运行 FLS 的脚本。

支持：

```bash
sh fls-a.sh start
sh fls-a.sh stop
sh fls-a.sh restart
sh fls-a.sh status
sh fls-a.sh log
sh fls-a.sh update
sh fls-a.sh clone
sh fls-a.sh bstart
sh fls-a.sh rstart
sh fls-a.sh ensure-repo
```

说明：

- 手动无参数执行时，只显示帮助，不进入菜单。
- 放入 `/data/adb/service.d/` 后无参数默认执行 `start`。
- 如果 Termux 中没有 FLS 文件，会自动在 Termux 环境中安装 git 并 `git clone` 完整仓库。
- `bstart` 会生成 `/data/adb/service.d/fls-a.sh` 自启脚本并重启 FLS。
- `rstart` 会移除 `/data/adb/service.d/fls-a.sh` 自启脚本并重启 FLS。
- 不提供交互菜单，避免 Android 文件管理器或后台环境断开输入后造成异常。

---

## 命令说明

### 基础命令

| 命令 | 说明 |
|---|---|
| `start` | 启动 FLS |
| `stop` | 停止 FLS |
| `restart` | 重启 FLS |
| `status` | 查看运行状态 |
| `log` / `logs` | 查看实时日志 |
| `update` | 使用 `git pull` 更新程序 |
| `clone` | 强制重新 clone 仓库并覆盖程序文件 |
| `bstart` | 生成自启配置并重启 |
| `rstart` | 移除自启配置并重启 |
| `ensure-repo` | 检查程序完整性，不完整才 clone |
| `menu` | Windows 前台菜单 |

---

### `ensure-repo` 和 `clone` 的区别

| 命令 | 行为 | 是否强制重新拉取 |
|---|---|---|
| `ensure-repo` | 检查本地程序是否完整，不完整才 clone | 否 |
| `clone` | 不管本地是否完整，都重新 clone 并覆盖程序文件 | 是 |

建议：

- 首次安装或文件缺失：使用 `ensure-repo`
- 本地仓库异常、文件混乱、`git pull` 失败：使用 `clone`
- 正常更新：使用 `update`

---

## 安装教程

---

## 一、Linux 安装

适用于：

- Debian / Ubuntu
- CentOS / Rocky / AlmaLinux
- Alpine
- 其他 Linux 环境

### 1. 安装基础环境

Debian / Ubuntu：

```bash
apt update
apt install -y python3 python3-pip python3-venv git
```

CentOS / Rocky / AlmaLinux：

```bash
yum install -y python3 python3-pip git
```

Alpine：

```bash
apk add --no-cache python3 py3-pip py3-virtualenv git
```

### 2. 下载项目

```bash
git clone https://github.com/liyw0205/fls.git
cd fls
chmod +x fls.sh
```

### 3. 启动

```bash
sh fls.sh start
```

### 4. 访问

默认地址：

```text
http://服务器IP:5700
```

如果指定端口：

```bash
sh fls.sh start -p 5701
```

则访问：

```text
http://服务器IP:5701
```

---

## 二、Termux 安装

### 1. 安装基础环境

```bash
pkg update -y
pkg install -y python git
```

建议额外安装：

```bash
pkg install -y clang make openssl libffi
```

### 2. 下载项目

```bash
git clone https://github.com/liyw0205/fls.git
cd fls
chmod +x fls.sh
```

### 3. 启动

```bash
sh fls.sh start
```

---

## 三、KernelSU / Magisk 启动 Termux 中的 FLS

适用于：

- KernelSU
- Magisk
- adb shell root
- 需要开机自启 FLS 的 Android 环境

### 1. 前提

请先安装并初始化 Termux。

如果希望 `fls-a.sh` 自动 clone 项目，需要 Termux 可正常使用 `pkg`。

### 2. 放置脚本

将 `fls-a.sh` 放到任意可执行位置，例如：

```text
/data/adb/fls-a.sh
```

赋予权限：

```bash
chmod 755 /data/adb/fls-a.sh
```

### 3. 手动测试

```bash
su -c 'sh /data/adb/fls-a.sh start'
```

### 4. 生成开机自启

```bash
su -c 'sh /data/adb/fls-a.sh bstart'
```

执行后会生成：

```text
/data/adb/service.d/fls-a.sh
```

### 5. 移除开机自启

```bash
su -c 'sh /data/adb/service.d/fls-a.sh rstart'
```

### 6. 查看日志

查看 `fls-a.sh` 日志：

```bash
su -c 'cat /data/adb/fls-a.log'
```

查看 FLS 主进程日志：

```bash
su -c 'cat /data/data/com.termux/files/home/fls/log/fls-manager-daemon.log'
```

---

## 四、Windows 安装

### 1. 安装基础环境

需要安装：

- Python：<https://www.python.org/downloads/windows/>
- Git：<https://git-scm.com/download/win>

也可以让脚本尝试通过 `winget` 自动安装 Git。

### 2. 下载项目

```powershell
git clone https://github.com/liyw0205/fls.git
cd fls
```

### 3. PowerShell 启动

```powershell
.\fls.ps1 start
```

如果遇到执行策略限制：

```powershell
powershell -ExecutionPolicy Bypass -File .\fls.ps1 start
```

### 4. CMD 启动

```bat
fls.bat start
```

### 5. Windows 菜单

双击：

```text
fls.bat
```

或执行：

```powershell
.\fls.ps1 menu
```

可以进入前台菜单。

---

## 首次使用说明

### 默认端口

默认端口：

```text
5700
```

临时指定端口：

Linux / Termux：

```bash
sh fls.sh start -p 5701
```

Windows PowerShell：

```powershell
.\fls.ps1 start -p 5701
```

Windows CMD：

```bat
fls.bat start -p 5701
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/service.d/fls-a.sh start -p 5701'
```

---

### 登录 Token

#### 方式 1：启动时临时指定

Linux / Termux：

```bash
sh fls.sh start -t 123456
```

Windows PowerShell：

```powershell
.\fls.ps1 start -t 123456
```

Windows CMD：

```bat
fls.bat start -t 123456
```

KernelSU / Magisk：

```bash
su -c 'FLS_TOKEN=123456 sh /data/adb/service.d/fls-a.sh start'
```

#### 方式 2：首次访问网页设置

如果未预设 Token，首次访问会进入：

```text
/setup
```

根据页面提示设置登录 Token 即可。

---

## 常用命令

### Linux / Termux

```bash
sh fls.sh start
sh fls.sh stop
sh fls.sh restart
sh fls.sh status
sh fls.sh log
sh fls.sh update
sh fls.sh clone
sh fls.sh bstart
sh fls.sh rstart
sh fls.sh ensure-repo
```

### Windows PowerShell

```powershell
.\fls.ps1 start
.\fls.ps1 stop
.\fls.ps1 restart
.\fls.ps1 status
.\fls.ps1 log
.\fls.ps1 update
.\fls.ps1 clone
.\fls.ps1 bstart
.\fls.ps1 rstart
.\fls.ps1 ensure-repo
```

### Windows CMD

```bat
fls.bat start
fls.bat stop
fls.bat restart
fls.bat status
fls.bat log
fls.bat update
fls.bat clone
fls.bat bstart
fls.bat rstart
fls.bat ensure-repo
```

### KernelSU / Magisk

```bash
su -c 'sh /data/adb/service.d/fls-a.sh start'
su -c 'sh /data/adb/service.d/fls-a.sh stop'
su -c 'sh /data/adb/service.d/fls-a.sh restart'
su -c 'sh /data/adb/service.d/fls-a.sh status'
su -c 'sh /data/adb/service.d/fls-a.sh update'
su -c 'sh /data/adb/service.d/fls-a.sh clone'
su -c 'sh /data/adb/service.d/fls-a.sh bstart'
su -c 'sh /data/adb/service.d/fls-a.sh rstart'
```

---

## 更新与维护

### update：正常更新

执行 `git pull` 更新当前仓库：

Linux / Termux：

```bash
sh fls.sh update
```

Windows：

```powershell
.\fls.ps1 update
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/service.d/fls-a.sh update'
```

更新后如面板正在运行，建议重启：

```bash
sh fls.sh restart
```

---

### clone：强制重新 clone

当本地仓库异常、文件缺失、`git pull` 失败时，可以强制重新 clone 覆盖程序文件。

Linux / Termux：

```bash
sh fls.sh clone
```

Windows：

```powershell
.\fls.ps1 clone
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/service.d/fls-a.sh clone'
```

说明：

- 会重新拉取仓库代码。
- 会覆盖程序文件。
- 通常会保留 `data/`、`log/`、`scripts/`、`.venv/` 等本地数据目录。
- 如果面板正在运行，建议执行后重启。

---

### ensure-repo：检查/拉取程序

用于检查程序文件是否完整。

Linux / Termux：

```bash
sh fls.sh ensure-repo
```

Windows：

```powershell
.\fls.ps1 ensure-repo
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/service.d/fls-a.sh ensure-repo'
```

说明：

- 如果本地程序完整，则不做处理。
- 如果缺少 `fls-manager.py` 或 `fls_manager/`，才会自动 clone 仓库。

---

## 自启管理

### bstart：生成自启并重启

Linux systemd：

```bash
sudo sh fls.sh bstart
```

Termux：

```bash
sh fls.sh bstart
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/fls-a.sh bstart'
```

Windows：

```powershell
.\fls.ps1 bstart
```

说明：

- Linux 会生成并启用：

```text
/etc/systemd/system/fls.service
```

- Termux 会生成 Termux:Boot 脚本：

```text
~/.termux/boot/fls-start.sh
```

- KernelSU / Magisk 会生成：

```text
/data/adb/service.d/fls-a.sh
```

- Windows 会生成计划任务：

```text
FLS Manager
```

生成自启后会立即重启 FLS。

---

### rstart：移除自启并重启

Linux：

```bash
sudo sh fls.sh rstart
```

Termux：

```bash
sh fls.sh rstart
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/service.d/fls-a.sh rstart'
```

Windows：

```powershell
.\fls.ps1 rstart
```

说明：

- 会移除对应平台的自启配置。
- 移除后会立即重启 FLS。
- 注意：`rstart` 不是停止 FLS，而是移除自启后重启当前服务。

---

## 任务命令说明

### `task` 模式

表示运行脚本目录中的脚本，或运行绝对路径脚本。

示例：

```text
task 1.py
task folder/main.py
task demo.sh arg1 arg2
task demo.js
task demo.ts
task run.ps1
task run.bat
task demo.php
task demo.rb
task demo.pl
task demo.lua
task app.jar
task /root/test.py
```

### 系统命令模式

不以 `task` 开头时，作为系统命令直接执行。

示例：

```text
python3 /root/other.py
bash /root/test.sh
node /root/demo.js
```

---

## 支持的脚本类型

当前支持：

- `.py`
- `.sh`
- `.js`
- `.ts`
- `.ps1`
- `.bat`
- `.php`
- `.rb`
- `.pl`
- `.lua`
- `.jar`

可在面板 **配置页** 中启用或禁用。

---

## FLS 内置命令：fls_kill

FLS 支持在任务命令中直接使用内置命令 `fls_kill`，用于清理残留进程、端口或后台服务。

支持参数：

```bash
fls_kill -p 3000                 # 按端口清理
fls_kill -n apiService           # 按进程名 / 命令关键字清理
fls_kill -f kgcheckin/api/app.js # 按文件或路径清理
fls_kill -d 12345                # 按 PID 清理
```

常见用法：

```bash
cd kgcheckin || exit 1

cleanup() {
  fls_kill -p 3000
}

trap cleanup EXIT INT TERM HUP

fls_kill -p 3000

npm run main
```

说明：

- `fls_kill` 是 FLS 内置命令，只能在 FLS 任务命令中使用。
- 推荐优先使用 `-p` 按端口清理，最安全。
- `-n` 是按命令关键字匹配，请避免写过于宽泛的名称，例如 `fls_kill -n node`，防止误杀其他进程。

---

## Web 面板功能

- **仪表盘**
- **任务管理**
- **全局变量**
- **代理管理**
- **脚本管理**
- **依赖管理**
- **日志管理**
- **通知管理**
- **备份恢复**
- **配置管理**
- **环境状态查看**

---

## 通知支持

支持以下通知渠道：

- Bark
- Server 酱
- PushPlus
- Telegram Bot
- 企业微信机器人
- 钉钉机器人
- 飞书机器人
- SMTP 邮件
- Ntfy
- WxPusher
- Gotify
- PushDeer
- 自定义 Webhook

说明：

- 可配置多个通知实例
- 同一渠道可配置多份
- 任务可选择不通知、使用全局默认通知或指定通知渠道

---

## 数据文件说明

- `data/tasks.json`：任务数据
- `data/global_env.json`：全局变量
- `data/proxies.json`：代理配置
- `data/config.json`：系统配置
- `data/fls-manager.pid`：主进程 PID
- `data/secret_key.txt`：Flask Session 密钥

---

## 日志说明

日志目录：

```text
log/
```

包括：

- 任务日志
- 主进程日志
- 依赖安装日志
- 系统环境安装日志
- 备份恢复日志

查看主进程日志：

Linux / Termux：

```bash
sh fls.sh log
```

Windows：

```powershell
.\fls.ps1 log
```

KernelSU / Magisk：

```bash
su -c 'sh /data/adb/service.d/fls-a.sh log'
```

---

## 备份恢复

支持格式：

- `.tar.gz`
- `.tgz`
- `.tar`
- `.zip`

说明：

- 备份时会自动导出依赖列表
- 恢复时可选择同时恢复 Python 依赖

---

## 常见问题

### 1. 启动失败怎么办？

查看日志：

Linux / Termux：

```bash
sh fls.sh log
```

Windows PowerShell：

```powershell
.\fls.ps1 log
```

Windows CMD：

```bat
fls.bat log
```

KernelSU / Magisk：

```bash
su -c 'cat /data/adb/fls-a.log'
su -c 'cat /data/data/com.termux/files/home/fls/log/fls-manager-daemon.log'
```

---

### 2. 提示没有 git

安装 Git：

Linux：

```bash
apt install -y git
```

Termux：

```bash
pkg install -y git
```

Windows：

<https://git-scm.com/download/win>

---

### 3. 端口无法访问

请检查：

- 程序是否已启动
- 防火墙是否放行端口
- 云服务器安全组是否放行端口
- 是否使用了其他端口启动
- 是否监听在正确的主机地址

---

### 4. 首次访问要求设置 Token

正常现象。

如果未设置登录 Token，首次访问会自动进入：

```text
/setup
```

---

### 5. 为什么任务运行后没有通知？

请检查：

- 是否在通知管理中创建并启用了通知
- 是否设置了全局默认通知
- 任务是否选择了“不通知”
- 通知渠道配置是否正确

---

### 6. Android 上为什么不提供 menu？

`fls-a.sh` 主要面向 KernelSU / Magisk / adb root 等非交互环境。

为了避免文件管理器后台被杀、标准输入断开后造成脚本残留或异常占用，不提供交互菜单。

请直接使用：

```bash
start
stop
restart
status
log
update
clone
bstart
rstart
```

等一次性命令。

---

### 7. `update` 和 `clone` 应该用哪个？

建议：

- 正常更新：使用 `update`
- 本地仓库损坏或文件混乱：使用 `clone`
- 只是检查程序是否存在：使用 `ensure-repo`

---

### 8. `bstart` 和 `rstart` 是什么意思？

- `bstart`：boot start，生成自启配置并重启 FLS。
- `rstart`：remove start，移除自启配置并重启 FLS。

注意：

```text
rstart 不是停止程序，而是移除自启后重启当前 FLS。
```

如果只想停止 FLS，请使用：

```bash
sh fls.sh stop
```

或：

```powershell
.\fls.ps1 stop
```

---

## AI 说明

本项目在开发过程中使用了 AI 辅助生成、重构、润色和整理部分代码与文档，最终内容由项目维护者审阅、整合与维护。

详见：[AI-NOTICE.md](./AI-NOTICE.md)

---

## 作者信息

- 作者：**余生只有凄渺**
- QQ群：**923184177**

---

## License

本项目采用 [MIT License](./LICENSE) 开源。

---

## Star 支持

如果这个项目对你有帮助，欢迎点一个 **Star** ⭐
