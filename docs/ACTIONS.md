# Actions 与发布

仓库有两个互相独立的工作流：模块打包和固定 proot 运行时构建。

## 模块打包

文件：`.github/workflows/package-module.yml`

- 普通 `main` 推送只有 `version.json` 变化时才运行。
- `workflow_dispatch` 可以手动运行。
- 推送 `v*` 标签时打包并创建模块 Release。
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
# 先更新 version.json 和 changelog.md
git add version.json changelog.md
git commit -m "chore: release 20260920.4"
git push origin main
git tag v20260920.4
git push origin v20260920.4
```

## 手动触发

GitHub 仓库的 **Actions** 页面中：

1. `Package KernelSU Magisk module`：生成模块 ZIP Artifact。
2. `Build fixed FLS proot runtimes`：构建并更新固定 `proot-runtime` Release。
