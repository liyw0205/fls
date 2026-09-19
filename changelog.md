# Changelog

## 20260920

- 新增 KernelSU / Magisk 通用模块 GitHub Actions 打包流程。
- 新增 arm64、armv7 的 proot Python 运行时资源。
- 新增 `python` 和 `all` 两种运行时配置。
- 模块首次启动时将 proot、Python 环境安装到 `/data/fls`，不再依赖 Termux。
- 模块升级时同步 FLS 项目代码，同时保留配置、任务、日志和用户脚本。
- 修复 Android Docker 构建阶段清理 `/etc/hostname` 导致运行时构建失败的问题。
