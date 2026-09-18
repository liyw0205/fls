# 阶段 95：通知发送隔离验证

为通知测试和“保存并测试”流程补充 mock sender 回归，验证页面结果和配置持久化而不访问外部服务。

验收：

- GET `/notify/test/<item_id>` 使用 mock sender 并渲染成功结果，外部网络发送函数不被执行。
- POST `/notify/new` 的 `action=test` 持久化通知配置并调用 mock sender，发送参数保持稳定。
- 目标测试、全量单元测试、响应式烟测、编译检查和差异检查通过。
