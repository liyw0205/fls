# KernelSU / Magisk 模块

模块使用独立的 proot 和 Python 环境，不安装、不启动、也不读取 Termux。模块入口来源于
`packaging/magisk/fls-a.sh`，安装后复制为 `/data/adb/fls-a.sh`。

## 目录

| 内容 | 路径 |
| --- | --- |
| 模块控制脚本 | `/data/adb/fls-a.sh` |
| Termux 控制脚本 | `/data/adb/fls-t.sh` |
| 模块目录 | `/data/adb/modules/fls-manager` |
| proot 运行时 | `/data/fls/runtime` |
| FLS 项目 | `/data/fls/project` |
| 配置和任务 | `/data/fls/project/data` |
| 项目日志 | `/data/fls/project/log` |
| Android 入口日志 | `/data/fls/fls-a.log` |

`fls-t.sh` 虽然也会随模块安装，但它仍然是 Termux 链路；它的项目和运行时在 Termux
HOME 下，不会切换到 `/data/fls`。

## 安装后控制

```bash
su -c 'sh /data/adb/fls-a.sh status'
su -c 'sh /data/adb/fls-a.sh start'
su -c 'sh /data/adb/fls-a.sh stop'
su -c 'sh /data/adb/fls-a.sh restart -p 5701 -t 123456'
su -c 'sh /data/adb/fls-a.sh log'
su -c 'sh /data/adb/fls-a.sh update'
su -c 'sh /data/adb/fls-a.sh clone'
```

模块服务启动时会同步程序文件到 `/data/fls/project`，但保留 `data/`、`log/` 和
`scripts/`。卸载模块也不会删除 `/data/fls`，重新安装可以继续使用已有数据。

## 运行时选择

默认安装轻量 `python` 运行时。需要额外 Linux 工具时选择 `all`：

```bash
su -c 'mkdir -p /data/fls && printf all > /data/fls/profile'
su -c 'sh /data/adb/fls-a.sh restart'
```

可用资源由固定的 `proot-runtime` Release 提供：

```text
fls-proot-python-arm64.tar.gz
fls-proot-all-arm64.tar.gz
fls-proot-python-armv7.tar.gz
fls-proot-all-armv7.tar.gz
```

## 模块状态说明

`/data/adb/modules/fls-manager/module.prop` 的 `description` 会在面板启动、停止、重启和
状态查询时更新，KernelSU/Magisk 页面会显示类似内容：

```text
🟢 运行中 | 端口: 5700 | PID: 12345
🔴 已停止 | 端口: 5700 | PID: -
```

这表示 `/data/fls` 容器中的面板状态，不表示 Termux 容器状态。

## 自启

模块自身通过 `service.sh` 在开机时启动。手动生成或移除 `service.d` 自启时使用：

```bash
su -c 'sh /data/adb/fls-a.sh bstart'
su -c 'sh /data/adb/fls-a.sh rstart'
```
