# 阶段 91：在线安装日志参数 JSON 回归

固定在线脚本安装日志接口在已有安装记录时收到非法 `lines` 参数的错误协议，避免轮询调用方收到 Flask HTML 500。

验收：构造内存安装记录并提交非整数行数，验证 `400 application/json`、稳定轮询字段；不读取日志、不修改 `ONLINE_INSTALL_RUNNING`，全量验证通过。
