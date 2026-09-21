# 安装与部署

FLS 有三种常用部署方式。Android 上的两种 proot 容器是独立的，详见
[Termux 容器](TERMUX.md) 和 [KernelSU / Magisk 模块](ANDROID_MODULE.md)。

## Linux

Debian/Ubuntu：

```bash
apt update
apt install -y python3 python3-pip python3-venv git
git clone https://github.com/liyw0205/fls.git
cd fls
sh fls.sh start
```

其他发行版只需提供 Python 3、pip、venv 和 Git。FLS 默认使用当前项目目录保存
`data/`、`log/`、`scripts/`，访问 `http://服务器IP:5700`。

## Windows

发布包安装（推荐）：

```powershell
.\install.ps1
```

它会检测 Python 3.10+，在 `%LOCALAPPDATA%\FLS` 创建虚拟环境并安装依赖；升级时保留
`data/`、`log/` 和 `scripts/`。如果需要自定义目录：

```powershell
.\install.ps1 -InstallDir "D:\Apps\FLS" -NoStart
```

源码目录开发运行需要安装 [Python](https://www.python.org/downloads/windows/)；Git 仅用于
源码目录的更新和重新拉取：

```powershell
git clone https://github.com/liyw0205/fls.git
cd fls
.\fls.ps1 start
```

也可以执行 `fls.bat start`。双击 `fls.bat` 或执行 `.\fls.ps1 menu` 可以打开前台菜单。

## Linux 一键安装

在源码、发布包目录执行：

```bash
sh install.sh
```

脚本默认安装到 `~/.local/share/fls`，保留已有 `data/`、`log/` 和 `scripts/`。设置
`FLS_NO_START=1` 可只安装不启动，设置 `FLS_INSTALL_DIR` 可修改安装目录。

## Android 选择

| 需求 | 入口 | 目录 | 依赖 |
| --- | --- | --- | --- |
| 在 Termux 中运行，或从 Android shell 调用 Termux | `fls-t.sh` | `$HOME/fls`、`$HOME/.fls-runtime` | Termux |
| 不安装 Termux，使用 KernelSU/Magisk 模块 | `fls-a.sh` | `/data/fls/project`、`/data/fls/runtime` | Root、KernelSU 或 Magisk |

`fls-t.sh` 和 `fls-a.sh` 不能互换：前者使用 Termux HOME/PREFIX，后者使用独立的
`/data/fls` 容器。

## 首次访问

默认端口为 `5700`。可以在启动时使用 `-p` 指定端口、使用 `-t` 指定 Token；没有预设
Token 时访问 `/setup` 初始化。详细参数见 [命令与配置](COMMANDS.md)。
