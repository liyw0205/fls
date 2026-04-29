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
- 支持 **脚本拉取 / 导入 / 在线编辑 / 改名**
- 支持 **日志查看 / 日志管理 / 自动清理**
- 支持 **代理管理**
- 支持 **依赖管理**
- 支持 **通知推送**
- 支持 **备份恢复**
- 支持 **Linux / Windows / Termux**
- 自带 **启动 / 停止 / 重启 / 状态 / 日志** 管理脚本

---

## 项目结构

```text
fls/
├─ fls-manager.py
├─ fls.sh
├─ fls.ps1
├─ fls.bat
├─ data/
├─ log/
├─ scripts/
└─ .venv/
```

### 目录说明

- `fls-manager.py`：主程序，Web 面板入口
- `fls.sh`：Linux / Termux 启停脚本
- `fls.ps1`：Windows PowerShell 启停脚本
- `fls.bat`：Windows CMD 启停脚本
- `data/`：配置、任务、代理、通知等数据
- `log/`：运行日志
- `scripts/`：脚本目录
- `.venv/`：Python 虚拟环境

---

## 脚本说明

### `fls-manager.py`
主程序，负责：

- 启动 Web 面板
- 管理任务调度
- 执行脚本
- 管理日志
- 管理依赖
- 管理通知
- 备份恢复
- 配置管理

### `fls.sh`
Linux / Termux 启停脚本。

支持：

```bash
sh fls.sh start
sh fls.sh stop
sh fls.sh restart
sh fls.sh status
sh fls.sh log
sh fls.sh menu
sh fls.sh ensure-manager
```

临时参数：

```bash
sh fls.sh start -t 123456 -p 5701
```

### `fls.ps1`
Windows PowerShell 启停脚本。

支持：

```powershell
.\fls.ps1 start
.\fls.ps1 stop
.\fls.ps1 restart
.\fls.ps1 status
.\fls.ps1 log
.\fls.ps1 menu
.\fls.ps1 ensure-manager
```

### `fls.bat`
Windows CMD 启停脚本。

支持：

```bat
fls.bat start
fls.bat stop
fls.bat restart
fls.bat status
fls.bat log
fls.bat menu
fls.bat ensure-manager
```

---

## 安装教程

---

### 一、Linux 安装

适用于：

- Debian / Ubuntu
- CentOS / Rocky / AlmaLinux
- Alpine
- 其他 Linux 环境

#### 1. 安装 Python

**Debian / Ubuntu**

```bash
apt update
apt install -y python3 python3-pip python3-venv
```

**CentOS / Rocky / AlmaLinux**

```bash
yum install -y python3 python3-pip
```

**Alpine**

```bash
apk add --no-cache python3 py3-pip
```

#### 2. 下载项目

```bash
git clone https://github.com/liyw0205/fls.git
cd fls
chmod +x fls.sh
```

#### 3. 启动

```bash
sh fls.sh start
```

#### 4. 访问

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

### 二、Termux 安装

#### 1. 安装基础环境

```bash
pkg update -y
pkg install -y python git
```

建议额外安装：

```bash
pkg install -y clang make openssl libffi
```

#### 2. 下载项目

```bash
git clone https://github.com/liyw0205/fls.git
cd fls
chmod +x fls.sh
```

#### 3. 启动

```bash
sh fls.sh start
```

---

### 三、Windows 安装

#### 方式 1：PowerShell

1. 安装 Python：<https://www.python.org/downloads/windows/>
2. 下载项目文件
3. 执行：

```powershell
.\fls.ps1 start
```

如果遇到执行策略限制：

```powershell
powershell -ExecutionPolicy Bypass -File .\fls.ps1 start
```

#### 方式 2：CMD

执行：

```bat
fls.bat start
```

---

## 首次使用说明

### 默认端口

默认端口：

```text
5700
```

可临时指定：

```bash
sh fls.sh start -p 5701
```

或：

```powershell
.\fls.ps1 start -p 5701
```

### 登录 Token

#### 方式 1：启动时临时指定

Linux：

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
sh fls.sh menu
```

### Windows PowerShell

```powershell
.\fls.ps1 start
.\fls.ps1 stop
.\fls.ps1 restart
.\fls.ps1 status
.\fls.ps1 log
```

### Windows CMD

```bat
fls.bat start
fls.bat stop
fls.bat restart
fls.bat status
fls.bat log
```

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
- 企业微信应用
- 钉钉机器人
- 飞书机器人
- SMTP 邮件
- Ntfy
- WxPusher
- 自定义 Webhook
- go-cqhttp
- Gotify
- iGot
- PushDeer
- Synology Chat
- 微加机器人
- Qmsg 酱
- 智能微秘书
- PushMe
- Chronocat
- OpeniLink

说明：

- 可配置多个通知实例
- 同一渠道可配置多份
- 任务可独立指定通知方式

---

## 数据文件说明

- `data/tasks.json`：任务数据
- `data/global_env.json`：全局变量
- `data/proxies.json`：代理配置
- `data/config.json`：系统配置
- `data/fls-manager.pid`：主进程 PID

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

---

## 备份恢复

支持格式：

- `.tar.gz`
- `.tgz`
- `.gz`
- `.zip`
- `.rar`
- `.7z`
- `.tar`

说明：

- 备份时会自动导出依赖列表
- 恢复时可选择同时恢复 Python 依赖
- `rar / 7z` 需要系统安装解压工具

---

## 常见问题

### 1. 启动失败怎么办？

查看日志：

Linux：

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

### 3. 端口无法访问

请检查：

- 程序是否已启动
- 防火墙是否放行端口
- 云服务器安全组是否放行端口
- 是否使用了其他端口启动

### 4. 首次访问要求设置 Token

正常现象。  
如果未设置登录 Token，首次访问会自动进入 `/setup`。

### 5. 为什么任务运行后没有通知？

请检查：

- 是否在通知管理中创建并启用了通知
- 是否设置了全局默认通知
- 任务是否选择了“不通知”
- 通知渠道配置是否正确

---

## AI 说明

本项目在开发过程中使用了 AI 辅助生成、重构、润色和整理部分代码与文档，最终内容由项目维护者审阅、整合与维护。

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
