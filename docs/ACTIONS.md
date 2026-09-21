# Actions 与发布

仓库有四个互相独立的工作流：KernelSU/Magisk 模块打包、固定 proot 运行时构建、
Windows 桌面包打包和 Windows EXE 安装包打包。

## Windows EXE 安装包

文件：`.github/workflows/package-windows-installer.yml`

- 采用 Inno Setup 生成 `FLS-Manager-Setup-Windows-x64.exe`，不是 ZIP 改名。
- 每次提交检查 `version.json`，只有版本变化时编译；`workflow_dispatch` 可强制运行。
- `workflow_dispatch`、主分支版本变更和推送 `v*` 标签时，将 EXE 和校验文件添加到对应 GitHub Release。
- 安装程序默认目录为 `%LOCALAPPDATA%\FLS`，安装阶段调用 `install.ps1` 创建虚拟环境并
  安装 Python 依赖。
- Windows Runner 会静默安装 EXE、启动面板、访问 `127.0.0.1:5700` 后停止服务。

## Windows 桌面包

文件：`.github/workflows/package-desktop-windows.yml`

- 每次提交都会检查 `version.json`；只有版本文件变化时才生成 Windows x64 ZIP。
- `workflow_dispatch`、主分支版本变更和推送 `v*` 标签时会打包并创建/更新对应 Release。
- 产物为 `fls-manager-desktop-windows-x64.zip`，包含 `install.ps1`、`fls.ps1` 和
  `fls.bat`，不包含运行数据、日志、虚拟环境或 Git 元数据。
- `install.ps1` 默认安装到 `%LOCALAPPDATA%\FLS`，升级时保留 `data/`、`log/` 和
  `scripts/`。

## 模块打包

文件：`.github/workflows/package-module.yml`

- 每次提交（任意分支）和标签推送都会触发工作流。
- 工作流会比较本次提交前后的 `version.json`，只有文件发生变化时才执行模块打包；未变化的提交会保留为跳过状态。
- `workflow_dispatch` 可以手动运行。
- `workflow_dispatch`、主分支版本变更和推送 `v*` 标签时打包并创建/更新模块 Release。
- 输出只有 `fls-manager-module.zip`，不会再套一层 ZIP 或 `payload/` 目录。

模块 ZIP 根目录包含：

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

## 固定 proot 运行时

文件：`.github/workflows/build-proot.yml`

该工作流只支持 `workflow_dispatch`，不会因普通提交重复构建。四个运行时统一发布到
固定标签 `proot-runtime`：

```text
fls-proot-python-arm64.tar.gz
fls-proot-all-arm64.tar.gz
fls-proot-python-armv7.tar.gz
fls-proot-all-armv7.tar.gz
```

模块和 Termux 脚本按设备 ABI、运行时类型分别下载资源。

## 版本文件

`version.json`：

- `versionCode`：KernelSU/Magisk 更新比较值
- `version`：可读版本号，也必须与 `v*` 标签去掉 `v` 后一致
- `zipUrl`：模块下载地址
- `changelog`：更新日志地址

`changelog.md` 保存发布说明。发布流程示例：

```bash
# 先更新 version.json 和 changelog.md；推送主分支即可自动创建/更新 Release
git add version.json changelog.md
git commit -m "chore: release 20260922.1"
git push origin main

# 也可以使用匹配 version.json.version 的标签触发
git tag v20260922.1
git push origin v20260922.1
```

## 手动触发

GitHub 仓库的 **Actions** 页面中：

1. `Package KernelSU Magisk module`：生成模块 ZIP Artifact。
2. `Build fixed FLS proot runtimes`：构建并更新固定 `proot-runtime` Release。
