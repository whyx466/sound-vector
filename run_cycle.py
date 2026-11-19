#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 云托管采集编排（每 5 分钟轻量一轮）
========================================================
与 GitHub Actions 的 collect.yml 同构，并补上公众号通道（境内节点直连优势）。
节奏（用户 2026-11-19 定）：GitHub 每小时 / WorkBuddy 自动化每小时 / 云托管每 5 分钟。

  1. 榜单监测   collect_charts.py --proxy direct
  2. 公众号采集 wechat_collect.py --pages 1 --max-fetch N （国内直连搜狗微信，云托管强项）
  3. 全球资讯   collect_global.py --proxy direct --limit N （Google/YouTube 境内受限属预期，
                                                  Bing/RSS/GDELT 照常，失败不挡整轮）
  4. 写库       cloud_writer.py（合并本轮新 batch 一次写入，自带去重/提炼/去噪）
  5. 本地自检   local_tidy.py --once（产 tidy_queue，供每小时 WorkBuddy 自动化消费）

5 分钟节奏的关键约束：
- 每轮必须轻量（默认全球 30 条 / 公众号抓 30 篇正文），大批次留给手动触发；
- 防重入锁 cycle.lock：上一轮没跑完，本轮直接跳过（不叠加、不重复写库）；
- 逐步容错：任何一步失败只记录，不中断后续步骤。
产出 out/last_cycle.json 供 /status 查询；日志进 stdout（云托管控制台可看）。

  python run_cycle.py            # 跑一轮（云托管 cron 与 /collect 都走这里）
  CYCLE_WECHAT=0 python run_cycle.py   # 跳过公众号通道
  CYCLE_GLOBAL_LIMIT=120 CYCLE_WECHAT_FETCH=140 python run_cycle.py  # 手动大批次
"""
import glob
import json
import os
import subprocess
import sys
import time

# 云托管 5 分钟班次强制快速模式：境内节点直连海外源/搜狗微信必然超时，
# 默认参数一轮要 20-40 分钟，节奏被彻底拖垮。快速模式压缩超时与查询数，
# 让单轮回到 1-3 分钟。大批次（手动/GitHub 境外节点）不设此变量，走完整参数。
os.environ.setdefault("FAST_MODE", "1")
FAST_MODE = os.environ.get("FAST_MODE", "").strip().lower() in ("1", "true", "yes", "on")

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(HERE, "collector")
OUT_DIR = os.path.join(COLLECTOR, "out")
CHART_DIR = os.path.join(OUT_DIR, "charts")
LOCK_FILE = os.path.join(OUT_DIR, "cycle.lock")
PY = sys.executable or "python3"


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def acquire_lock():
    """防重入：cron 每 5 分钟一班，上一轮还在跑则本班跳过。"""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, ("%d" % os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        pass
    except Exception:
        return True  # 锁不可用时不挡采集
    # 陈锁（进程被杀残留）超过 2 小时则抢占
    try:
        if time.time() - os.path.getmtime(LOCK_FILE) > 7200:
            os.remove(LOCK_FILE)
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ("%d" % os.getpid()).encode("ascii"))
            os.close(fd)
            return True
    except Exception:
        pass
    return False


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


PROGRESS_FILE = os.path.join(OUT_DIR, "progress.json")


def write_progress(phase, detail="", steps=None):
    """实时进度落盘，/status 可读——解决"跑到哪一步、卡在哪"的监控盲区。"""
    try:
        d = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": phase,
             "detail": detail, "steps": steps or []}
        with open(PROGRESS_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except Exception:
        pass


def run_step(name, argv, timeout=1200):
    """跑一个子步骤；失败不抛出，返回结果字典。每步完成即刷新进度。"""
    t0 = time.time()
    log("步骤 %s：%s" % (name, " ".join(argv)))
    try:
        p = subprocess.run([PY] + argv, cwd=COLLECTOR, timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = ((p.stdout or "") + (p.stderr or ""))[-1200:]
        result = {"step": name, "ok": p.returncode == 0,
                  "sec": round(time.time() - t0, 1),
                  "code": p.returncode, "log_tail": tail}
    except subprocess.TimeoutExpired as e:
        # 超时也收集已产生的 stdout，便于定位卡在哪
        partial = ""
        if e.stdout:
            partial = (e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes)
                       else e.stdout)
        result = {"step": name, "ok": False,
                  "sec": round(time.time() - t0, 1),
                  "error": "timeout after %ss" % timeout, "log_tail": partial[-1200:]}
    except Exception as e:
        result = {"step": name, "ok": False,
                  "sec": round(time.time() - t0, 1), "error": str(e)[:300]}
    log("步骤 %s 完成 ok=%s %.1fs" % (name, result.get("ok"), result.get("sec", 0)))
    return result


def _run():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CHART_DIR, exist_ok=True)
    limit = int(os.environ.get("CYCLE_LIMIT", "40"))
    global_limit = os.environ.get("CYCLE_GLOBAL_LIMIT", "30")
    wechat_fetch = os.environ.get("CYCLE_WECHAT_FETCH", "30")
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}

    before_batches = set(glob.glob(os.path.join(OUT_DIR, "batch_*.json")))
    before_events = set(glob.glob(os.path.join(CHART_DIR, "events_*.json")))
    write_progress("start", "本轮开始", summary["steps"])

    # 1. 榜单监测（Spotify/QQ/网易云/酷狗）
    write_progress("charts", "榜单监测中", summary["steps"])
    summary["steps"].append(run_step(
        "charts", ["collect_charts.py", "--proxy", "direct"], timeout=180))
    write_progress("charts_done", "榜单完成", summary["steps"])

    # 2. 公众号（境内直连优势通道；每词只翻 1 页、抓正文限量，适配 5 分钟班次）
    if os.environ.get("CYCLE_WECHAT", "1") != "0":
        write_progress("wechat", "公众号采集中", summary["steps"])
        summary["steps"].append(run_step(
            "wechat", ["wechat_collect.py", "--pages", "1",
                       "--max-fetch", wechat_fetch], timeout=150))
        write_progress("wechat_done", "公众号完成", summary["steps"])

    # 3. 全球资讯（境内只跑有效的出版社 RSS，四类海外死源已跳过，实测 ~90s）
    write_progress("global", "全球源采集中（出版社 RSS）", summary["steps"])
    summary["steps"].append(run_step(
        "global", ["collect_global.py", "--proxy", "direct",
                   "--limit", global_limit, "--days", "2"], timeout=200))
    write_progress("global_done", "全球源完成", summary["steps"])

    # 4. 写库：合并本轮新产 batch 文件，一次写入（cloud_writer 自动去重）
    new_batches = sorted(f for f in glob.glob(os.path.join(OUT_DIR, "batch_*.json"))
                         if f not in before_batches)
    new_events = sorted(f for f in glob.glob(os.path.join(CHART_DIR, "events_*.json"))
                        if f not in before_events)
    merged = {"articles": []}
    for f in new_batches:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                j = json.load(fh)
            merged["articles"].extend(j.get("articles") or [])
            log("合并批采 %s（累计 %d 条）" % (os.path.basename(f), len(merged["articles"])))
        except Exception as e:
            log("批采文件读取失败 %s：%s" % (f, str(e)[:120]))

    events = new_events[-1] if new_events else None
    write_progress("write", "写库中（%d 条候选）" % len(merged["articles"]), summary["steps"])
    if merged["articles"] or events:
        merged_path = os.path.join(OUT_DIR, "cycle_merged.json")
        with open(merged_path, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False)
        argv = ["cloud_writer.py", "--batch", merged_path, "--limit", str(limit)]
        if not merged["articles"]:
            argv = ["cloud_writer.py", "--limit", str(limit)]
        if events:
            argv += ["--events", events]
        # 境内云托管节点访问 Google/MyMemory 翻译服务必然超时（每条约 36s），
        # 30 条即撞满 300s 被强杀、整轮写库失败。快速模式下跳过翻译：
        # 原文照常入库，中文标题/摘要由小程序端兜底翻译（cloud_writer 注释同口径）。
        # GitHub 境外节点不设 FAST_MODE，仍逐条翻好中文再入库。
        if FAST_MODE:
            argv += ["--no-translate"]
        summary["steps"].append(run_step("write", argv, timeout=90))
    else:
        summary["steps"].append({"step": "write", "ok": True,
                                 "note": "本轮无新候选，跳过写库"})

    # 5. 本地自检（规则引擎零成本；修正队列由每小时 WorkBuddy 自动化消费）
    write_progress("tidy", "本地自检中", summary["steps"])
    summary["steps"].append(run_step(
        "tidy", ["local_tidy.py", "--once"], timeout=120))

    summary["ok"] = all(s.get("ok") for s in summary["steps"])
    summary["new_batches"] = [os.path.basename(f) for f in new_batches]
    path = os.path.join(OUT_DIR, "last_cycle.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写 last_cycle.json 失败：%s" % str(e)[:120])
    write_progress("done", "本轮结束 ok=%s" % summary["ok"], summary["steps"])
    log("=== 本轮结束 ok=%s ===" % summary["ok"])
    print(json.dumps(summary, ensure_ascii=False)[:4000])
    return 0


def main():
    if not acquire_lock():
        log("上一轮采集未结束（cycle.lock 存在），本班跳过")
        return 0
    try:
        return _run()
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
