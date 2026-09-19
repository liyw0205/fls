# Termux 容器

`fls-t.sh` 是 Termux 专用的 proot 控制脚本。它可以在 Termux 内运行，也可以从 Android
外部 shell 调用，但始终使用 Termux 的 HOME/PREFIX，不使用 `/data/fls`。

## 目录与运行时

- 项目目录：`$HOME/fls`
- proot 运行时：`$HOME/.fls-runtime`
- 运行时 Release：固定标签 `proot-runtime`
- 默认运行时：`python`
- 可选运行时：`all`

首次执行会按设备 ABI 下载运行时。项目更新或重新拉取时会保留项目中的 `data/`、`log/`
和 `scripts/`。

## Termux 内安装

```bash
pkg update -y
pkg install -y curl git
git clone https://github.com/liyw0205/fls.git "$HOME/fls"
sh "$HOME/fls/fls-t.sh" start
```

不安装 Git 时，脚本在找不到 Git 的情况下会尝试下载仓库压缩包；下载运行时仍需要
`curl` 或 `wget`。

切换到包含更多 Linux 工具的运行时：

```bash
printf '%s\n' all > "$HOME/.fls-profile"
sh "$HOME/fls/fls-t.sh" restart
```

## Android 外部调用

模块安装后会提供 `/data/adb/fls-t.sh`。也可以把仓库中的脚本复制到该路径：

```bash
su -c 'sh /data/adb/fls-t.sh start'
su -c 'sh /data/adb/fls-t.sh status'
```

默认定位 `com.termux`。包名或多用户路径不同的时候覆盖：

```bash
su -c 'TERMUX_PACKAGE=com.termux \
  TERMUX_HOME=/data/data/com.termux/files/home \
  TERMUX_PREFIX=/data/data/com.termux/files/usr \
  sh /data/adb/fls-t.sh restart'
```

## 命令

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

`bstart` 和 `rstart` 管理 Termux:Boot，不创建 KernelSU/Magisk 的 `service.d` 服务。

## 环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `TERMUX_PACKAGE` | `com.termux` | Termux 包名 |
| `TERMUX_HOME` | 根据包名推导 | Termux HOME |
| `TERMUX_PREFIX` | 根据包名推导 | Termux PREFIX |
| `FLS_BASE_DIR` | `$TERMUX_HOME/fls` | 项目目录 |
| `FLS_RUNTIME_DIR` | `$TERMUX_HOME/.fls-runtime` | 运行时目录 |
| `FLS_PROOT_PROFILE` | 读取 `~/.fls-profile`，默认 `python` | `python` 或 `all` |
| `FLS_REPO_URL` | FLS GitHub 仓库 | 项目仓库地址 |
| `FLS_REPO_BRANCH` | `main` | 项目分支 |
