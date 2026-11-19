#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云托管 -> GitHub Actions 海外全量采集的"启动键"。

背景：GitHub 自带 schedule 实测长期不触发（官方承认定时事件会延迟/丢失），
而境内云托管节点访问 Google/YouTube/GDELT/Bing 必然超时，抓不到海外源。
海外全量的采集代码只在 GitHub 境外节点能跑，缺的只是一个可靠的触发者。

本模块让每 5 分钟醒一次的云托管，每小时调一次 GitHub workflow_dispatch
接口触发海外全量。用文件记录上次触发时间做节流（默认 55 分钟），
进程重启也不丢；触发失败只记日志，不影响本轮境内采集。

环境变量：
  GH_TOKEN   必填，GitHub 个人访问令牌（需 repo + workflow 权限）
  GH_REPO    默认 whyx466/sound-vector
  GH_KICK_MIN_INTERVAL  节流间隔分钟，默认 55
"""
import json
import os
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
STATE_FILE = os.path.join(OUT_DIR, "gh_kick_state.json")

REPO = os.environ.get("GH_REPO", "whyx466/sound-vector").strip()
WORKFLOW = "collect.yml"          # 仓库内 workflow 文件名
MIN_INTERVAL = int(os.environ.get("GH_KICK_MIN_INTERVAL", "55")) * 60


def _log(msg):
    print("[gh-kick] %s" % msg, flush=True)


def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(st):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        _log("写状态失败：%s" % str(e)[:100])


def kick(force=False):
    """检查节流后触发一次海外全量。返回 (是否发起触发, 说明)。"""
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        return False, "未配 GH_TOKEN，跳过"
    st = _read_state()
    last = float(st.get("last_ts", 0) or 0)
    now = time.time()
    if not force and now - last < MIN_INTERVAL:
        wait = int(MIN_INTERVAL - (now - last))
        return False, "距上次触发仅 %d 分钟，节流中（再等 %d 分钟）" % (
            int((now - last) / 60), wait // 60)

    url = "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (REPO, WORKFLOW)
    body = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "soundvector-cloudrun",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status  # workflow_dispatch 成功返回 204
        if code not in (200, 201, 204):
            return False, "HTTP %s 非预期" % code
        st.update({"last_ts": now, "last_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "last_result": "ok", "count": int(st.get("count", 0)) + 1})
        _write_state(st)
        msg = "已触发海外全量（第 %s 次，%s）" % (st["count"], st["last_at"])
        _log(msg)
        return True, msg
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        st.update({"last_ts": now, "last_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "last_result": "fail HTTP %s" % e.code, "last_detail": detail})
        _write_state(st)
        msg = "触发失败 HTTP %s：%s" % (e.code, detail[:120])
        _log(msg)
        return False, msg
    except Exception as e:
        msg = "触发异常：%s" % str(e)[:150]
        _log(msg)
        return False, msg


def status():
    st = _read_state()
    return {
        "configured": bool(os.environ.get("GH_TOKEN", "").strip()),
        "repo": REPO,
        "min_interval_min": MIN_INTERVAL // 60,
        "last_at": st.get("last_at"),
        "last_result": st.get("last_result"),
        "count": int(st.get("count", 0) or 0),
    }


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    fired, msg = kick(force=force)
    print(("已发起 " if fired else "未发起 ") + msg)
