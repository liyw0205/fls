# 开发与测试

## 本地检查

```bash
PYTHONPATH=. pytest -q
python3 -m compileall -q fls_manager
git diff --check
```

Shell 控制脚本修改后检查语法：

```bash
for script in fls.sh fls-a.sh fls-t.sh packaging/magisk/*.sh; do
  sh -n "$script" || exit 1
done
```

## 浏览器回归

前端修改需要在当前 Termux 浏览器环境测试手机和桌面视口。至少覆盖：

- 登录、刷新、后台恢复和首次访问通知
- 侧边栏切换、更新提示和更新日志浮窗
- 任务启动、运行中停止、停用/启用、复制、删除和日志查看
- 表单保存、错误提示、弹窗关闭和返回导航
- 模块状态、端口和 PID 文案（模块环境）

检查页面是否存在错位、文字溢出、按钮遮挡、操作入口重复、功能互相干扰、错误请求
方法、重复触发和异常状态。手机视口建议覆盖 `390x844`、`430x932`，桌面视口建议覆盖
`1024x768`、`1280x800` 和 `1440x900`。

## 数据隔离

测试时设置独立的 `FLS_BASE_DIR`，不要直接使用真实运行数据：

```bash
FLS_BASE_DIR="$PWD/.test-fls" PYTHONPATH=. pytest -q
```

真实 Termux、服务器、KernelSU/Magisk 数据不应进入测试提交。浏览器测试完成后检查：

```bash
git status --short
```
