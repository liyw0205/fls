# Changelog

## 20260920.2

- 新增独立的 `fls-t.sh`，支持 Termux 内和 Android 外部调用。
- Termux proot 运行时改为下载到 `$HOME/.fls-runtime`，项目更新时保留用户数据。
- 模块 Web 入口支持保存 IP/端口、重置默认地址并在新窗口打开面板。
- KernelSU/Magisk 模块同步安装 `fls-t.sh`，并补充打包校验。

## 20260920.1

- 修正 KernelSU / Magisk 模块 ZIP 的根目录结构，避免嵌套模块目录导致无法安装。
- 将 `version.json`、`changelog.md`、`action.sh` 和 `webroot/index.html` 纳入模块包。
- 模块安装时检查并下载固定标签的 proot 运行时，同时保留 `/data/fls` 中的配置、日志和用户脚本。
- 将四个 proot 运行时拆分到独立工作流，避免每次模块打包重复构建。

## 20260920

- 新增 KernelSU / Magisk 通用模块 GitHub Actions 打包流程。
- 新增 arm64、armv7 的 proot Python 运行时资源。
- 新增 `python` 和 `all` 两种运行时配置。
- 模块首次启动时将 proot、Python 环境安装到 `/data/fls`，不再依赖 Termux。
- 模块升级时同步 FLS 项目代码，同时保留配置、任务、日志和用户脚本。
- 修复 Android Docker 构建阶段清理 `/etc/hostname` 导致运行时构建失败的问题。
