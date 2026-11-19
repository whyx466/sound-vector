#!/bin/sh
# 声向采集服务启动：Web 进程内置调度线程驱动采集（不依赖容器 cron）
set -e
cd /app

# 1. 固化环境变量供子进程采集脚本继承（cron 已弃用，此步仍保留给诊断/兼容）
printenv | grep -E '^(WB_KEY|WB_REST_URL|COLLECT_TOKEN|GOOGLE_API_KEY|CYCLE_|https_proxy|http_proxy)=' > /app/.cron_env || true
chmod 600 /app/.cron_env

# 2. 定时调度已内置于 app.py 的 scheduler 线程（Web 进程存活即调度存活）。
#    容器 cron 在云托管实测静默不执行（PATH/daemon 驻留坑），故停用：
#    crontab /app/task.txt ; cron

# 3. 启动 Web 服务（探活/手动触发/内置调度都在这个进程里）
PORT="${PORT:-8080}"
exec gunicorn -w 1 -b "0.0.0.0:${PORT}" --timeout 600 app:app
