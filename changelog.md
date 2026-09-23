# Changelog

## 20260923.2

- Release 更新日志现在只显示对应版本的内容。
- Windows EXE 安装器使用简体中文界面，缺少 Python 时打开官方 Windows 下载页且不自动下载。

## 20260923.1

- 将面板重启和停止操作移动到仪表盘，并增加二次确认弹窗。
- 移除仪表盘最近运行区和重复提示，精简运行历史及面板信息表头。

## 20260922.1

- 模块、Windows 桌面包和 Windows EXE 安装包统一自动上传到对应版本 Release。
- 手动运行或主分支版本变更时使用 `version.json` 自动生成 Release 标签。
- Windows 桌面包和 EXE 安装包使用独立的 SHA256 校验文件名，避免 Release 资源冲突。

## 20260921.1

- 每次 `main` 提交都会触发模块工作流，但只有 `version.json` 发生变化时才打包。
- 修复移动端多操作卡片的按钮布局，并避免桌面端重复显示移动卡片。

## 20260920.4

- 明确拆分 `fls-t.sh` 的 Termux 容器与 `fls-a.sh` 的 `/data/fls` 独立容器部署。
- KernelSU/Magisk 模块说明会显示面板运行状态、端口和 PID。
- 重整 README 安装、更新、模块和运行时说明，并加入 FLS 面板图标。

## 20260920.3

- 任务运行时将主操作切换为“停止任务”，避免重复启动同一任务。
- 将卡片底部的停止操作改为停用/启用任务，并移除更多操作中的重复入口。
- 使用新的 FLS 页面图标，并补充 Apple touch icon。

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
