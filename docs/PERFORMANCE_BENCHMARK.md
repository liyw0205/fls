# 高频入口性能基准

## 范围与方法

本阶段只测真实前端/API 调用链：`GET /history`（浅分页、深分页、关键词搜索）、
`GET /log/<task_id>`（详情历史）、`GET /api/status`（状态列表）和
`POST /api/task/action/toggle/<task_id>`（状态变更）。基准在临时 `FLS_BASE_DIR` 中
生成固定 10,000 条历史和 100 个任务，Flask `test_client` 从请求开始计时到响应体
序列化完成；不连接生产服务，不生成百万用户/记录。

每个场景预热 2 次、采样 12 次，报告 p50/p95/p99、吞吐、错误数/错误率、CPU 时间、
峰值 RSS、平均/最大响应字节。基线通过 `FLS_DISABLE_HISTORY_INDEX=1` 模拟原始的
JSON 全量读取；优化组使用派生 SQLite 索引和 storage 锁。结果写到 `/tmp`，不是仓库
资产。

所有 API 或 Playwright 重测都必须先执行：

```bash
flock -w 600 /tmp/nonebot-project-validation.lock \
  python3 tools/benchmark_performance.py --mode optimized --iterations 12 --playwright
```

推荐使用 `tools/run_performance_benchmark.sh`，锁被占用时它在创建临时 fixture、测试
服务或浏览器前退出。Playwright 缺少 Python 包或 Chromium 时只报告 `skipped`，不伪造
通过。

## 2026-11-15 结果

本机 Linux、10,000 行、100 任务、12 次采样；RSS 为进程峰值，响应大小为每次响应体
字节数。两组 HTTP 错误率均为 0%，响应大小和字段契约保持一致。

| 场景 | 基线 p50/p95/p99 ms | 优化 p50/p95/p99 ms | 优化吞吐 req/s | RSS MB | 平均响应 B |
| --- | ---: | ---: | ---: | ---: | ---: |
| 列表浅页 | 30.695 / 37.642 / 38.346 | 1.817 / 2.140 / 2.220 | 542.14 | 57.648 | 16,905 |
| 深分页 | 30.144 / 32.356 / 33.032 | 3.220 / 4.071 / 4.500 | 296.40 | 57.648 | 17,201 |
| 关键词搜索 | 39.504 / 46.781 / 47.296 | 7.376 / 7.873 / 7.982 | 133.64 | 57.648 | 16,899 |
| 详情 | 31.491 / 34.771 / 37.211 | 2.367 / 2.494 / 2.498 | 420.38 | 57.648 | 9,572 |
| 状态列表 | 2.239 / 2.520 / 2.523 | 2.058 / 2.335 / 2.399 | 470.48 | 57.648 | 19,302 |
| 状态变更 toggle | 6.532 / 12.433 / 17.524 | 6.362 / 8.601 / 9.197 | 148.21 | 57.648 | 54.5 |

CPU 占用（CPU 时间/墙钟时间）约为 92%-100%，属于单进程同步基准；优化组没有错误
增长。`EXPLAIN QUERY PLAN` 对状态筛选使用 `history_rows_status_seq`，详情使用
`history_rows_task_seq`；保持任意子串语义的搜索使用有界 `SCAN history_rows`，但只解析
匹配页的 JSON 行。Playwright 结果为 `skipped`（当前环境缺少 `playwright` Python 包）。

## 回归门槛

在相同 10k fixture、12 次采样和共享锁下，优化组应保持 0 错误；p95 上限为浅页 5ms、
深分页 8ms、搜索 15ms、详情 5ms、状态 5ms、toggle 15ms；峰值 RSS 不超过 128MB。
机器差异较大时，以 CI 同一 runner 的连续两次结果为比较基线，不能放宽数据规模或跳过
锁。失败时保留 stdout JSON 和 stderr 日志到 `/tmp` 供定位，不把临时数据库或结果提交。
