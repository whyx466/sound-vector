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
  GET  /brain-check 大脑自检（容器内调一次真实大模型，验证链路；带令牌鉴权）

鉴权：环境变量 COLLECT_TOKEN。
  - 请求头 X-Collect-Token: <token>  或  ?token=<token>
  - 未配置 COLLECT_TOKEN 时只允许容器内 127.0.0.1 调用（cron 走本机安全）。
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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
        "build_tag": "v20261121-0210-auth-openid",
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


@app.route("/brain-check", methods=["GET"])
def brain_check():
    """大脑自检：让容器里的 llm_brain 对一条固定测试稿跑一次真实大模型调用。
    不写库、不采集，仅验证「容器 → 大模型端点」链路是否可用。带令牌鉴权。"""
    if not _authorized():
        return jsonify({"ok": False, "error": "未授权（缺 COLLECT_TOKEN）"}), 401
    result = {"ok": False, "endpoint_reachable": False, "model": "",
              "reply": "", "error": ""}
    try:
        sys.path.insert(0, os.path.join(HERE, "collector"))
        import llm_brain as brain
        result["model"] = brain.MODEL_CHAIN[0]
        messages = [
            {"role": "system", "content": "你是音乐新闻编辑，只输出简短中文。"},
            {"role": "user", "content": "用一句话说明格莱美奖是什么。"},
        ]
        last_err = ""
        for model in brain.MODEL_CHAIN:
            try:
                text = brain._chat_stream(model, messages, timeout=25)
                if text and text.strip():
                    result.update(ok=True, endpoint_reachable=True,
                                  model=model, reply=text.strip()[:300])
                    return jsonify(result)
                last_err = "%s 返回空" % model
            except Exception as e:
                last_err = "%s %s" % (model, str(e)[:120])
        result["error"] = last_err or "所有模型都不可用"
    except Exception as e:
        result["error"] = "大脑模块加载失败：%s" % str(e)[:150]
    return jsonify(result), 503


@app.route("/live-search", methods=["GET", "POST"])
def live_search():
    """提问当场联网搜索：抓境内可达的头条/Bing → 大模型汇总成中文答案。
    小程序通过微信云托管内网调用（免 request 合法域名）。带令牌鉴权。
    GET ?q=xxx&token=xxx 或 POST JSON {"q":"xxx","token":"xxx"}。"""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or "").strip()
        tok = data.get("token") or request.args.get("token") or ""
    else:
        q = (request.args.get("q") or "").strip()
        tok = request.args.get("token") or ""
    if tok != os.environ.get("COLLECT_TOKEN", "sv-2026-collect"):
        return jsonify({"ok": False, "error": "未授权（缺 token）"}), 401
    if not q:
        return jsonify({"ok": False, "error": "缺少问题参数 q"}), 400
    try:
        sys.path.insert(0, os.path.join(HERE, "collector"))
        import live_search as ls
        payload = ls.answer(q, log=app.logger.info)
        return jsonify(payload)
    except Exception as e:
        app.logger.exception("[live-search] 失败")
        return jsonify({"ok": False, "query": q, "answer": "",
                        "error": "联网搜索异常：%s" % str(e)[:150],
                        "sources": [], "searched": False}), 200


# ---------------------------------------------------------------------------
# 声向自有账号：微信 openid 直通登录（2026-11-21）
# ---------------------------------------------------------------------------
# 小程序通过 wx.cloud.callContainer 调云托管时，微信网关在请求头注入
# X-WX-OPENID / X-WX-UNIONID（微信保证、前端伪造不了）。用它做身份，
# 无需 WorkBuddy Auth 授权、无需绑定小程序、无需 AppSecret。
# 用户表用我们自己的 sv_users（不碰平台内置 users 表）。
WB_REST = os.environ.get("WB_REST_URL", "https://mp-api.app.workbuddy.host/.cloud/database/rest").rstrip("/")


def _wb_key():
    k = os.environ.get("WB_KEY", "").strip()
    if k:
        return k
    p = os.path.join(HERE, "collector", ".wb_key")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _wb_req(method, path, body=None, prefer=None):
    key = _wb_key()
    if not key:
        return 0, {"error": "缺少 WB_KEY"}
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hd = {"x-wb-webapp-access-key": key, "Content-Type": "application/json",
          "Accept": "application/json"}
    if prefer:
        hd["Prefer"] = prefer
    req = urllib.request.Request(WB_REST + path, data=data, headers=hd, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(raw) if raw.strip() else [])
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        return e.code, raw[:300]
    except Exception as e:
        return 0, str(e)[:200]


def _wechat_identity():
    """从微信网关注入的请求头取身份。openid 是权威身份，unionid 跨应用打通用。"""
    headers = request.headers
    openid = (headers.get("X-WX-OPENID") or headers.get("x-wx-openid") or "").strip()
    unionid = (headers.get("X-WX-UNIONID") or headers.get("x-wx-unionid") or "").strip()
    return openid, unionid


@app.route("/auth/login", methods=["POST", "GET"])
def auth_login():
    """小程序 callContainer 调用：读网关注入的 openid，upsert sv_users，返回稳定身份。

    返回 {ok, user:{openid, sid, nickname, avatar, loginCount, firstLogin, lastLogin}}。
    sid = openid 的短哈希，只用于展示/本地缓存，真正身份以 openid 为准。
    """
    openid, unionid = _wechat_identity()
    if not openid:
        # 不是从微信小程序网关注入的调用——可能是公网直连或本地调试，拒绝并说明。
        return jsonify({"ok": False,
                        "error": "NO_WECHAT_IDENTITY",
                        "hint": "请从体验版/正式版小程序内调用（wx.cloud.callContainer）"}), 200
    data = request.get_json(silent=True) or {} if request.method == "POST" else {}
    nickname = str(data.get("nickname") or "").strip()[:60]
    avatar = str(data.get("avatar") or "").strip()[:500]
    # 先读现有行，拿到累计登录次数与已存昵称头像。
    st, rows = _wb_req("GET", "/sv_users?openid=eq.%s&limit=1" % urllib.parse.quote(openid))
    old = (rows or [{}])[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    count = int(old.get("login_count") or 0) + 1
    payload = {
        "openid": openid,
        "unionid": unionid or old.get("unionid") or "",
        "nickname": nickname or old.get("nickname") or "",
        "avatar": avatar or old.get("avatar") or "",
        "login_count": count,
        "last_login": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }
    # PostgREST upsert：主键冲突则更新，配合 Prefer: resolution=merge-duplicates。
    st2, res2 = _wb_req("POST", "/sv_users", payload,
                        prefer="resolution=merge-duplicates,return=representation")
    if st2 >= 400 or (isinstance(res2, dict) and res2.get("error")):
        return jsonify({"ok": False, "error": "DB_UPSERT_FAILED",
                        "detail": str(res2)[:200]}), 200
    sid = hashlib.md5(openid.encode("utf-8")).hexdigest()[:12]
    return jsonify({"ok": True, "user": {
        "openid": openid, "sid": sid,
        "nickname": payload["nickname"], "avatar": payload["avatar"],
        "loginCount": count,
        "firstLogin": old.get("first_login") or payload["last_login"],
        "lastLogin": payload["last_login"],
    }})


@app.route("/auth/me", methods=["GET"])
def auth_me():
    """按 openid 查用户（仅读），供小程序刷新登录态。"""
    openid, _ = _wechat_identity()
    if not openid:
        return jsonify({"ok": False, "error": "NO_WECHAT_IDENTITY"}), 200
    st, rows = _wb_req("GET", "/sv_users?openid=eq.%s&limit=1" % urllib.parse.quote(openid))
    if not isinstance(rows, list) or not rows:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 200
    u = rows[0]
    return jsonify({"ok": True, "user": {
        "openid": openid,
        "sid": hashlib.md5(openid.encode("utf-8")).hexdigest()[:12],
        "nickname": u.get("nickname") or "", "avatar": u.get("avatar") or "",
        "loginCount": u.get("login_count") or 0,
        "firstLogin": u.get("first_login") or "",
        "lastLogin": u.get("last_login") or "",
    }})


@app.route("/auth/whoami", methods=["GET"])
def auth_whoami():
    """诊断：回显网关注入了哪些微信身份头（只回长度，不回明文），验证链路。"""
    h = request.headers
    return jsonify({
        "ok": True,
        "hasOpenid": bool((h.get("X-WX-OPENID") or "").strip()),
        "hasUnionid": bool((h.get("X-WX-UNIONID") or "").strip()),
        "wxHeaders": sorted([k for k in h.keys() if k.lower().startswith("x-wx-")]),
        "fromService": h.get("X-WX-SERVICE") or "",
    })


@app.route("/net-check", methods=["GET"])
def net_check():
    """网络自检：让容器实测海外检索源/公众号源的可达性与产出。
    只读探测（GET/小样本），不写库、不采集入库。带令牌鉴权。
    用于判定 FAST_MODE 跳过的 Google News/Bing/GDELT 在容器内是否真的不可用。"""
    if not _authorized():
        return jsonify({"ok": False, "error": "未授权（缺 COLLECT_TOKEN）"}), 401
    import urllib.request
    import time as _t
    targets = [
        ("Google News RSS", "https://news.google.com/rss/search?q=music%20album&hl=en-US&gl=US&ceid=US:en"),
        ("Bing News RSS", "https://www.bing.com/news/search?q=music&format=rss"),
        ("GDELT", "https://api.gdeltproject.org/api/v2/doc/doc?query=music&mode=ArtList&maxrecords=5&format=json&timespan=1d"),
        ("Sogou WeChat", "https://weixin.sogou.com/weixin?type=2&query=%E9%9F%B3%E4%B9%90&page=1&ie=utf8"),
        ("Google Translate", "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=hello"),
        # 境内可达的普通搜索源（提问当场联网搜索的后端）
        ("Toutiao Search", "https://so.toutiao.com/search?keyword=BIGBANG%202026"),
        ("Bing Web Search", "https://cn.bing.com/search?q=Taylor%20Swift%202026&ensearch=0"),
    ]
    out = {}
    for name, url in targets:
        t0 = _t.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read(4000).decode("utf-8", "ignore")
                info = {"status": r.status, "sec": round(_t.time() - t0, 1), "reachable": True}
                if name == "Sogou WeChat":
                    info["captcha"] = ("验证码" in body or "antispider" in body or "请输入验证码" in body)
                elif "RSS" in name:
                    info["items"] = body.count("<item>")
                elif name == "Toutiao Search":
                    info["hits"] = body.count("<em>") + body.count("u003cem")
                elif name == "Bing Web Search":
                    info["items"] = body.count("b_algo")
                out[name] = info
        except Exception as e:
            out[name] = {"reachable": False, "sec": round(_t.time() - t0, 1), "error": str(e)[:100]}
    return jsonify({"ok": True, "results": out})


# 启动进程内调度线程（gunicorn -w 1 单 worker，只会有一个调度器）
threading.Thread(target=_scheduler_loop, daemon=True, name="cycle-scheduler").start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
