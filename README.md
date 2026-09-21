# FLS

<p align="center">
  <img src="https://raw.githubusercontent.com/liyw0205/fls/main/fls_manager/static/favicon-generated.png" alt="FLS 面板图标" width="112" height="112">
</p>

<p align="center">
  <b>FLS = Flask Lightweight Script Manager</b><br>
  轻量级脚本任务管理面板
</p>

<p align="center">
  <a href="https://github.com/liyw0205/fls/stargazers"><img src="https://img.shields.io/github/stars/liyw0205/fls?style=flat-square" alt="stars"></a>
  <a href="https://github.com/liyw0205/fls/releases"><img src="https://img.shields.io/github/v/release/liyw0205/fls?style=flat-square" alt="release"></a>
  <a href="https://github.com/liyw0205/fls/blob/main/LICENSE"><img src="https://img.shields.io/github/license/liyw0205/fls?style=flat-square" alt="license"></a>
</p>

FLS 通过 Web 面板管理脚本、定时任务、日志、依赖、代理、通知和备份。

## Android 部署区别

| 入口 | 运行位置 | 是否依赖 Termux | 说明 |
| --- | --- | --- | --- |
| [`fls-t.sh`](fls-t.sh) | `$HOME/.fls-runtime`、`$HOME/fls` | 是 | Termux 专用 proot 容器，可由 Termux 或 Android 外部 shell 调用 |
| [`fls-a.sh`](fls-a.sh) | `/data/fls/runtime`、`/data/fls/project` | 否 | KernelSU/Magisk 独立 proot 容器 |

两条链路的运行时、项目和数据目录互相独立，不能混用。模块构建使用
`packaging/magisk/fls-a.sh`，安装后为 `/data/adb/fls-a.sh`。

## 快速开始

### Linux

```bash
apt update && apt install -y python3 python3-pip python3-venv git
git clone https://github.com/liyw0205/fls.git
cd fls
sh fls.sh start
```

也可以使用 Linux 一键安装脚本：

```bash
sh install.sh
```

### Termux

```bash
pkg update -y && pkg install -y curl git
git clone https://github.com/liyw0205/fls.git "$HOME/fls"
sh "$HOME/fls/fls-t.sh" start
```

### KernelSU / Magisk

从 GitHub Release 安装 `fls-manager-module.zip`，安装后使用：

```bash
su -c 'sh /data/adb/fls-a.sh status'
su -c 'sh /data/adb/fls-a.sh start'
```

### Windows

下载 Windows Release 后，在压缩包根目录执行一次安装脚本：

```powershell
.\install.ps1
```

安装脚本会在 `%LOCALAPPDATA%\FLS` 创建独立虚拟环境，后续升级会保留
`data/`、`log/` 和 `scripts/`。也可以指定安装目录或只安装不启动：

```powershell
.\install.ps1 -InstallDir "D:\Apps\FLS"
.\install.ps1 -NoStart
```

源码目录也可以直接运行：

```powershell
git clone https://github.com/liyw0205/fls.git
cd fls
.\fls.ps1 start
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [安装与部署](docs/INSTALL.md) | Linux、Windows 和部署选择 |
| [Termux 容器](docs/TERMUX.md) | `fls-t.sh` 安装、外部调用、运行时和自启 |
| [KernelSU / Magisk 模块](docs/ANDROID_MODULE.md) | `fls-a.sh`、`/data/fls`、模块状态和数据保留 |
| [命令与配置](docs/COMMANDS.md) | 启停命令、端口、Token、环境变量和维护 |
| [Actions 与发布](docs/ACTIONS.md) | 模块 ZIP、固定 proot 运行时和版本发布 |
| [数据结构](docs/DATA_SCHEMA.md) | `data/*.json` 字段和读取迁移规则 |
| [面板参考](docs/PANEL_REFERENCE.md) | 同类面板的信息架构参考 |

## 项目结构

```text
fls/
├─ fls-manager.py       # 面板入口
├─ fls.sh               # Linux/Termux 原生控制脚本
├─ fls-t.sh             # Termux proot 入口
├─ fls-a.sh             # /data/fls 容器入口
├─ fls.ps1 / fls.bat    # Windows 入口
├─ install.ps1          # Windows 发布包安装器
├─ install.sh           # Linux 一键安装器
├─ fls_manager/         # Flask 面板代码
├─ data/ log/ scripts/  # 运行数据、日志和用户脚本
└─ packaging/           # KernelSU/Magisk 和 proot 构建文件
```

更新程序时保留 `data/`、`log/` 和 `scripts/`。默认面板地址为
`http://设备IP:5700`；未配置 Token 时访问 `/setup` 完成初始化。

## License

详见 [`LICENSE`](LICENSE)。
