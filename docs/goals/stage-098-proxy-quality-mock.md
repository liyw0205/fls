# 阶段 98：代理质量检测隔离验证

为代理质量检测的已保存代理和表单入口补充请求级 mock 回归，固定 URL 解析、响应字段和代理参数边界。

验收：

- GET `/api/proxy/quality/<proxy_id>` 对有效代理调用 mock detector 并返回稳定结果，不访问外部地址。
- POST `/api/proxy/quality-form` 解析表单检测地址后调用 mock detector，保留代理连接参数和结果字段。
- 目标测试、全量单元测试、响应式烟测、编译检查和差异检查通过。
