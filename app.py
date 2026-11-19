#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 云托管采集服务 HTTP 入口
==============================================
Flask 应用，配合容器内 cron（见 task.txt / start.sh）每小时自动采集。

路由：
  GET  /        健康检查（云托管探活用）
  GET  /status  上一轮采集结果（out/last_cycle.json）
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

from flask import Flask, jsonify, request

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "collector", "out")
TOKEN = os.environ.get("COLLECT_TOKEN", "").strip()

_lock = threading.Lock()
_state = {"busy": False, "last_started": None, "last_finished": None}


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
            _state["last_finished"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "soundvector-collector",
                    "busy": _state["busy"]})


@app.get("/status")
def status():
    data = None
    try:
        with open(os.path.join(OUT_DIR, "last_cycle.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    return jsonify({"busy": _state["busy"],
                    "last_started": _state["last_started"],
                    "last_finished": _state["last_finished"],
                    "last_cycle": data})


@app.route("/collect", methods=["POST", "GET"])
def collect():
    if not _authorized():
        return jsonify({"ok": False, "error": "未授权（缺 COLLECT_TOKEN）"}), 401
    with _lock:
        if _state["busy"]:
            return jsonify({"ok": False, "error": "上一轮还在跑，稍后再试"}), 409
        _state["busy"] = True
        _state["last_started"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    threading.Thread(target=_run_cycle, daemon=True).start()
    return jsonify({"ok": True, "msg": "采集已在后台启动，GET /status 查进度"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
