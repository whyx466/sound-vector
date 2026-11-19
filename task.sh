#!/bin/sh
# cron 执行入口：cron 不继承容器环境变量，先加载 start.sh 固化的 env 再跑采集
# 关键修复(2026-11-20)：cron 默认 PATH=/usr/bin:/bin，官方 Python 镜像的 python3
# 在 /usr/local/bin，不补 PATH 会 "python3: not found" 静默失败——这是部署成功却
# 零采集/零入库的根因。显式补 PATH + 绝对路径双保险。
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
cd /app
[ -f /app/.cron_env ] && . /app/.cron_env
exec /usr/local/bin/python3 run_cycle.py
