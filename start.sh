#!/bin/sh
# 声向采集服务启动：固化环境变量给 cron → 起 cron → 起 Web
set -e
cd /app

# 1. 把容器环境变量固化给 cron（cron 默认只有最小环境）
printenv | grep -E '^(WB_KEY|WB_REST_URL|COLLECT_TOKEN|GOOGLE_API_KEY|CYCLE_|https_proxy|http_proxy)=' > /app/.cron_env || true
chmod 600 /app/.cron_env

# 2. 安装并启动定时任务
crontab /app/task.txt
cron

# 3. 启动 Web 服务（探活/手动触发）
PORT="${PORT:-8080}"
exec gunicorn -w 1 -b "0.0.0.0:${PORT}" --timeout 600 app:app
