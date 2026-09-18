# FLS 签到脚本

`wisart_checkin.py` 和 `lupi_checkin.py` 是两个独立的 Python 签到脚本，分别对应智画创和噜皮生图。

## FLS 任务变量

在 FLS 任务的环境变量中配置：

```text
WISART_COOKIE=wisart_session=...
LUPI_COOKIE=chatgpt2api_session=...
```

任务命令：

```text
task wisart_checkin.py
task lupi_checkin.py
```

也可以直接传参：

```text
task wisart_checkin.py --cookie "wisart_session=..."
task lupi_checkin.py --cookie "chatgpt2api_session=..."
```

也可以使用显式凭据文件：`--cookie-file /path/to/cookies.txt`，或设置通用环境变量 `SIGNIN_COOKIE_FILE`。文件需包含对应站点域名及其 Cookie；脚本不会自动扫描或读取固定路径。

常用选项：

```text
--dry-run   只查询状态，不执行签到
--json      输出单行 JSON，便于日志或通知处理
--timeout 30
```
