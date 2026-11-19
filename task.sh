#!/bin/sh
# cron 执行入口：cron 不继承容器环境变量，先加载 start.sh 固化的 env 再跑采集
cd /app
[ -f /app/.cron_env ] && . /app/.cron_env
exec python3 run_cycle.py
