#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云托管 -> GitHub Actions 海外采集的"启动键"（分层触发）。

背景：GitHub 自带 schedule 实测长期不触发（官方承认定时事件会延迟/丢失），
而境内云托管节点访问 Google/YouTube/GDELT/Bing 必然超时，抓不到海外源。
海外采集代码只在 GitHub 境外节点能跑，缺的只是一个可靠的触发者。

本模块让每 5 分钟醒一次的云托管，按分层节奏触发两类海外 workflow：
  - 快采 collect-fast.yml：每 5 分钟一次（无限流的 RSS+Bing，十几秒~1分钟跑完）
  - 全量 collect.yml    ：每 55 分钟一次（GDELT/YouTube/GNews + 90 天大事回填）
用文件记录上次触发时间分别节流，进程重启也不丢；失败只记日志，不影响本轮境内采集。

环境变量：
  GH_TOKEN              必填，GitHub 个人访问令牌（需 Actions: Read and write）
  GH_REPO               默认 whyx466/sound-vector
  GH_KICK_FAST_INTERVAL 快采节流分钟，默认 4（<5分钟调度间隔，避开边界抖动）
  GH_KICK_MIN_INTERVAL  全量节流分钟，默认 55（兼容旧变量名）
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
FAST_WORKFLOW = "collect-fast.yml"
FULL_WORKFLOW = "collect.yml"
# 快采每轮都发（云托管每 5 分钟一轮，节流设 4 分钟 < 调度间隔，避开 300s 边界
# 抖动导致隔轮才触发）；全量 55 分钟一次（留余量，靠 schedule 8/28/48 错峰补）。
FAST_INTERVAL = int(os.environ.get("GH_KICK_FAST_INTERVAL", "4")) * 60
FULL_INTERVAL = int(os.environ.get("GH_KICK_MIN_INTERVAL", "55")) * 60


def _log(msg):
    print("[gh-kick] %s" % msg, flush=True)


def _read_state():
    """读状态；兼容旧格式（顶层 last_ts/count 即全量槽位）。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if "full" not in d and "fast" not in d and "last_ts" in d:
        d = {"full": d, "fast": {}}
    return d


def _write_state(st):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        _log("写状态失败：%s" % str(e)[:100])


def _trigger(workflow, force=False):
    """通用触发：按 workflow 独立节流。返回 (是否发起, 说明)。"""
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        return False, "未配 GH_TOKEN，跳过"
    interval = FAST_INTERVAL if workflow == FAST_WORKFLOW else FULL_INTERVAL
    key = "fast" if workflow == FAST_WORKFLOW else "full"
    label = "快采" if key == "fast" else "全量"

    st = _read_state()
    slot = st.get(key) if isinstance(st.get(key), dict) else {}
    last = float(slot.get("last_ts", 0) or 0)
    now = time.time()
    if not force and now - last < interval:
        wait = int(interval - (now - last))
        return False, "%s节流中（再等 %d 分钟）" % (label, wait // 60)

    url = "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (REPO, workflow)
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
        slot.update({"last_ts": now, "last_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "last_result": "ok", "count": int(slot.get("count", 0)) + 1})
        st[key] = slot
        _write_state(st)
        msg = "已触发海外%s（第 %s 次，%s）" % (label, slot["count"], slot["last_at"])
        _log(msg)
        return True, msg
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        slot.update({"last_ts": now, "last_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "last_result": "fail HTTP %s" % e.code, "last_detail": detail})
        st[key] = slot
        _write_state(st)
        msg = "触发失败 HTTP %s：%s" % (e.code, detail[:120])
        _log(msg)
        return False, msg
    except Exception as e:
        msg = "触发异常：%s" % str(e)[:150]
        _log(msg)
        return False, msg


def kick_fast(force=False):
    """触发 5 分钟快采（RSS+Bing，无限流）。"""
    return _trigger(FAST_WORKFLOW, force=force)


def kick(force=False):
    """触发全量采集（GDELT/YouTube/GNews + 回填），保留旧签名兼容。"""
    return _trigger(FULL_WORKFLOW, force=force)


def status():
    st = _read_state()
    full = st.get("full") if isinstance(st.get("full"), dict) else {}
    fast = st.get("fast") if isinstance(st.get("fast"), dict) else {}
    return {
        "configured": bool(os.environ.get("GH_TOKEN", "").strip()),
        "repo": REPO,
        "min_interval_min": FULL_INTERVAL // 60,  # 顶层字段保持旧结构供 app.py 读取（全量）
        "last_at": full.get("last_at"),
        "last_result": full.get("last_result"),
        "count": int(full.get("count", 0) or 0),
        "fast": {
            "workflow": FAST_WORKFLOW,
            "min_interval_min": FAST_INTERVAL // 60,
            "last_at": fast.get("last_at"),
            "last_result": fast.get("last_result"),
            "count": int(fast.get("count", 0) or 0),
        },
        "full": {
            "workflow": FULL_WORKFLOW,
            "min_interval_min": FULL_INTERVAL // 60,
            "last_at": full.get("last_at"),
            "last_result": full.get("last_result"),
            "count": int(full.get("count", 0) or 0),
        },
    }


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    fast = "--fast" in sys.argv
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    if fast:
        fired, msg = kick_fast(force=force)
    else:
        fired, msg = kick(force=force)
    print(("已发起 " if fired else "未发起 ") + msg)
