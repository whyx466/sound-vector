#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 云托管采集服务 HTTP 入口
==============================================
Flask 应用，内置定时调度线程（每 5 分钟一轮），不依赖容器 cron。

为何内置调度而不是 cron（2026-11-20 实测教训）：
  容器 cron 在云托管里部署成功但任务静默不执行（PATH/daemon 驻留/环境继承多重坑），
  连续 3 个班次零落盘。Web 进程经探活证明稳定存活，改由它自己的线程驱动采集最可靠。

路由：
  GET  /        健康检查（云托管探活用）
  GET  /status  上一轮采集结果（out/last_cycle.json）+ 调度器/密钥诊断
  POST /collect 手动触发一轮（异步后台跑，立即返回；带令牌鉴权）

鉴权：环境变量 COLLECT_TOKEN。
  - 请求头 X-Collect-Token: <token>  或  ?token=<token>
  - 未配置 COLLECT_TOKEN 时只允许容器内 127.0.0.1 调用（cron 走本机安全）。
"""
import json
import os
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "collector", "out")
TOKEN = os.environ.get("COLLECT_TOKEN", "").strip()
INTERVAL = int(os.environ.get("CYCLE_INTERVAL", "300"))  # 默认 5 分钟

_lock = threading.Lock()
_state = {"busy": False, "last_started": None, "last_finished": None,
          "trigger": None, "cycles_started": 0}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _authorized():
    if not TOKEN:
        return request.remote_addr in ("127.0.0.1", "::1")
    got = request.headers.get("X-Collect-Token", "") or request.args.get("token", "")
    return got == TOKEN


def _run_cycle():
    env = dict(os.environ)
    try:
        p = subprocess.run([sys.executable or "python3", "run_cycle.py"],
                           cwd=HERE, timeout=3600, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env)
        print((p.stdout or "")[-2000:], flush=True)
        if p.returncode != 0:
            print((p.stderr or "")[-1000:], flush=True)
    except Exception as e:
        print("采集线程异常：%s" % str(e)[:300], flush=True)
    finally:
        with _lock:
            _state["busy"] = False
            _state["last_finished"] = _now()


def _trigger_cycle(source="manual"):
    """统一入口：手动 /collect 与内置调度器都走这里，带互斥防重入。"""
    with _lock:
        if _state["busy"]:
            return False
        _state["busy"] = True
        _state["last_started"] = _now()
        _state["trigger"] = source
        _state["cycles_started"] += 1
    threading.Thread(target=_run_cycle, daemon=True).start()
    return True


def _scheduler_loop():
    """进程内定时：启动 15 秒后跑首轮，之后每 INTERVAL 秒一轮。"""
    time.sleep(15)
    while True:
        _trigger_cycle("scheduler")
        time.sleep(INTERVAL)


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "soundvector-collector",
                    "busy": _state["busy"], "interval_sec": INTERVAL})


@app.get("/status")
def status():
    data = None
    try:
        with open(os.path.join(OUT_DIR, "last_cycle.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    progress = None
    try:
        with open(os.path.join(OUT_DIR, "progress.json"), "r", encoding="utf-8") as f:
            progress = json.load(f)
    except Exception:
        progress = None
    gh_kick = None
    try:
        sys.path.insert(0, os.path.join(HERE, "collector"))
        from gh_kick import status as gh_status
        gh_kick = gh_status()
    except Exception:
        pass
    return jsonify({
        # 版本标记：用于验证 push 是否触发云托管自动重建（线上出现此值=新代码已生效）
        "build_tag": "v20261120-1250-garble-scrub",
        "busy": _state["busy"],
        "last_started": _state["last_started"],
        "last_finished": _state["last_finished"],
        "trigger": _state["trigger"],
        "cycles_started": _state["cycles_started"],
        "interval_sec": INTERVAL,
        "progress": progress,
        "gh_kick": gh_kick,
        "env": {
            "WB_KEY": bool(os.environ.get("WB_KEY", "").strip()),
            "GOOGLE_API_KEY": bool(os.environ.get("GOOGLE_API_KEY", "").strip()),
            "COLLECT_TOKEN": bool(TOKEN),
            "GH_TOKEN": bool(os.environ.get("GH_TOKEN", "").strip()),
        },
        "last_cycle": data,
    })


@app.route("/collect", methods=["POST", "GET"])
def collect():
    if not _authorized():
        return jsonify({"ok": False, "error": "未授权（缺 COLLECT_TOKEN）"}), 401
    if not _trigger_cycle("manual"):
        return jsonify({"ok": False, "error": "上一轮还在跑，稍后再试"}), 409
    return jsonify({"ok": True, "msg": "采集已在后台启动，GET /status 查进度"})


# 启动进程内调度线程（gunicorn -w 1 单 worker，只会有一个调度器）
threading.Thread(target=_scheduler_loop, daemon=True, name="cycle-scheduler").start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
