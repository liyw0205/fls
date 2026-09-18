# 阶段 96：备份创建与恢复隔离验证

为备份创建和导入恢复流程补充 mock/专用夹具回归，验证任务登记、目录恢复和调度器边界。

验收：

- POST `/api/backup/create` 解析选择项并调用 mock worker，不启动真实归档线程。
- POST `/backup/import` 仅在隔离 `FLS_BASE_DIR` 中处理内存 tar 归档，验证 `data`/`scripts` 覆盖、现有 `backups` 保留和依赖恢复调用。
- 恢复流程 mock `install_dependencies()` 与 `reload_scheduler()`，不覆盖真实数据、不执行真实 pip 或调度器重载。
- 目标测试、全量单元测试、响应式烟测、编译检查和差异检查通过。
