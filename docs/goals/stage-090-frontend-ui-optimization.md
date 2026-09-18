# 阶段 90：前端壳层、 中宽表格与长页验收

状态：已完成

## 目标

- 修复长页面顶部栏吸附失效及移动菜单层级遮挡。
- 提升 901–1180px 中宽屏表格的关键字段和操作可达性。
- 为配置、依赖、在线脚本和关于页提供长页分段定位。
- 补齐 favicon、资源版本和可重复的真实浏览器回归矩阵。

## 实现

- 根滚动使用 `overflow-x:clip`，`.topbar` 在四类视口滚动时保持顶部。
- 菜单通过 `fls-menu-open` 管理层级和浮动控件隐藏，支持 Escape 关闭，并在 AJAX/设备切换后清理状态。
- 中宽表格固定首列和末列操作列；任务表隐藏低优先级列，配置类型表取消额外最小宽度。
- 配置、依赖、在线脚本和关于页增加区块导航及锚点；配置和关于说明子块使用 `fls-subsection`。
- 新增 `fls_manager/static/favicon.svg`，CSS/JS 资源版本统一为 `20260918-1`。
- 新增 `tools/browser_regression.py`，`tools/responsive_smoke.py` 覆盖正式 14 页面。

## 验收

- `python -B tools/browser_regression.py --base-url http://127.0.0.1:5710 --token ui-audit-token`：84/84（14 页面 × 6 视口）通过；无 HTTP、页面类、文档溢出、顶栏吸附、菜单遮罩、控制台错误或请求失败。
- `python -B -m unittest discover -s tests`：通过，216 tests OK。
- `python -B tools/responsive_smoke.py`：通过。
- `node --check fls_manager/static/fls.js`：通过。
- `python -B -m compileall fls-manager.py fls_manager tests tools`：通过。
- `git diff --check`：通过。

## 未覆盖副作用

真实依赖安装、在线脚本安装、通知发送、备份恢复和面板重启未在浏览器矩阵中触发，后续仍需专用夹具或 mock。
