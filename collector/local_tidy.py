#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 每分钟本地自检（内置大模型本地整理层）
============================================================
用户 2026-11-19 要求的分层自检架构：

  每分钟（本脚本，本地规则引擎，零成本不调大模型）
      → 增量检查云库新入库条目：运营稿/噪声/非热点长文
      → 生成修正队列 out/tidy_queue.json（不下直接改库——WB_KEY 只有 INSERT 权）
      → 噪声候选短语学习，累计晋升进 noise_patterns.json（自我增长）
  每小时（WorkBuddy 大模型自动化，联网）
      → 消费 tidy_queue：用写权限逐条执行修正（去噪/提炼/压缩/删除）
      → 复核 learn_candidates，晋升或驳回（一步步引导规则库成长）
      → 联网采集新内容，录入时遵守「简讯优先」红线

本脚本只依赖标准库，读云端用 REST GET（只读），学习写本地 JSON。
计划任务每分钟执行一次；带锁文件防重入，增量处理（只看 id > last_id）。

  python local_tidy.py            # 正常跑一轮
  python local_tidy.py --once     # 同上（显式）
  python local_tidy.py --scan 200 # 回溯扫描最近 N 条（忽略断点，用于初始化）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import distill

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
QUEUE_FILE = os.path.join(OUT_DIR, "tidy_queue.json")
STATE_FILE = os.path.join(OUT_DIR, "tidy_state.json")
LOCK_FILE = os.path.join(OUT_DIR, "tidy.lock")
LOG_FILE = os.path.join(OUT_DIR, "tidy_log.jsonl")
PATTERNS_FILE = distill.PATTERNS_FILE

REST = os.environ.get(
    "WB_REST_URL", "https://mp-api.app.workbuddy.host/.cloud/database/rest").rstrip("/")

LOCK_STALE_SEC = 120      # 锁超过 2 分钟视为残留
PROMOTE_AT = 5            # 候选短语累计出现次数达到后自动晋升
MAX_QUEUE = 300           # 队列上限，防止无人对接时无限膨胀
FETCH_LIMIT = 150         # 每轮最多回看多少条


def _load_key():
    k = os.environ.get("WB_KEY", "").strip()
    if k:
        return k
    for p in (os.path.join(HERE, ".wb_key"), os.path.join(OUT_DIR, ".wb_key")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass
    return ""


KEY = _load_key()


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _req_get(path):
    url = REST + path
    hd = {
        "x-wb-webapp-access-key": KEY,
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=hd, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(raw) if raw.strip() else [])
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:200]
    except Exception as e:
        return 0, str(e)[:200]


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ── 候选短语学习：含噪声暗示词、但当前规则库没接住的独立短行 ──
# 晋升门槛：跨文章累计 ≥ PROMOTE_AT 次，且不含「是/的/了/在/之」等叙述性字
# （防止「值得关注的是」这类正文句子被误杀）。晋升后写入 noise_phrases 立即生效。
SENTENCE_TOKEN = re.compile(r"是|的|了|在|之|因为|所以|但是|然而|认为|表示|指出|报道|介绍|记者|今天|昨天")


def collect_candidates(text, bag):
    """从保下来的行里挑可疑短行计入候选（每篇每条只计一次）。"""
    seen = set()
    for ln in re.split(r"[\r\n]+", text or ""):
        s = ln.strip()
        if not (4 <= len(s) <= 24):
            continue
        if any(p.search(s) for p in distill.NOISE_LINE_PATS):
            continue          # 已被规则库接住，无需学习
        if not distill.SUSPECT_TOKEN.search(s):
            continue
        core = re.sub(r"[\s，。！？、：:；;【】\[\]\"'“”|丨·．.\-—~～!?！()（）]", "", s)
        if not (4 <= len(core) <= 16):
            continue
        if SENTENCE_TOKEN.search(core):
            continue          # 像正文句子，不学
        if core in seen:
            continue
        seen.add(core)
        bag[core] = bag.get(core, 0) + 1


def learn_patterns(bag):
    """把本轮候选并进 noise_patterns.json，达到阈值自动晋升。返回(新增候选, 晋升数)。"""
    data = load_json(PATTERNS_FILE, {})
    cands = data.get("learn_candidates") or {}
    phrases = data.get("noise_phrases") or []
    llog = data.get("learn_log") or []
    promoted = 0
    for core, n in bag.items():
        if core in phrases:
            continue
        cands[core] = cands.get(core, 0) + n
        if cands[core] >= PROMOTE_AT and core not in phrases:
            phrases.append(core)
            del cands[core]
            promoted += 1
            llog.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "action": "promote", "phrase": core,
                         "note": "本地自检累计 %d 次自动晋升" % PROMOTE_AT})
            log("[学习] 晋升噪声短语：%s" % core)
    data["learn_candidates"] = cands
    data["noise_phrases"] = phrases
    data["learn_log"] = llog[-200:]
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_json(PATTERNS_FILE, data)
    distill.reload_patterns()
    return len(bag), promoted


def analyze_article(a, bag):
    """对单条资讯做四项检查，返回修正操作列表。"""
    ops = []
    aid = a.get("id")
    title = (a.get("title") or "").strip()
    summary = a.get("summary") or ""
    body = a.get("body") or ""
    source = a.get("source") or ""

    collect_candidates(body + "\n" + summary, bag)

    # 1. 运营稿提炼/拒录
    d = distill.analyze(title, body, source)
    if d["action"] == "reject":
        ops.append({"op": "reject", "id": aid, "title": title[:80],
                    "reason": d.get("reason", "")})
        return ops
    if d["action"] == "rewrite":
        ops.append({"op": "rewrite", "id": aid, "title": title[:80],
                    "fields": {"title": d["title"], "summary": d["summary"],
                               "body": "", "is_brief": True, "read_minutes": 1,
                               "tag": "一句话资讯"},
                    "reason": d.get("reason", "")})
        return ops

    # 2. 噪声清理（关注引流/油价/署名/此前报道……）
    b1, s1 = distill.scrub(body), distill.scrub(summary)
    fields = {}
    if b1 != body:
        fields["body"] = b1
    if s1 != summary:
        fields["summary"] = s1
    if fields:
        ops.append({"op": "scrub", "id": aid, "title": title[:80],
                    "fields": fields,
                    "reason": "去噪声（%d字→%d字）" % (len(body), len(b1))})

    # 3. 非热点长文压缩（一句话讲透，热点才多内容）
    chg, ns, nb, nib, nrm = distill.brief_check(
        title, s1 or summary, b1 or body, a.get("category") or "",
        bool(a.get("featured")), a.get("external_heat") or 0)
    if chg:
        f = {"summary": ns, "body": nb, "is_brief": nib, "read_minutes": nrm}
        if ops and ops[-1]["op"] == "scrub":
            ops[-1]["fields"].update(f)
            ops[-1]["reason"] += "；非热点压缩为一句话"
        else:
            ops.append({"op": "compress", "id": aid, "title": title[:80],
                        "fields": f,
                        "reason": "非热点长文压缩为一句话（简讯优先）"})
    return ops


def acquire_lock():
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(LOCK_FILE):
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age < LOCK_STALE_SEC:
            return False
        log("发现残留锁（%.0fs），清理后继续" % age)
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--scan", type=int, default=0,
                    help="忽略断点，回扫最近 N 条（初始化用）")
    a = ap.parse_args()

    if not KEY:
        log("缺少 WB_KEY（环境变量或 .wb_key 文件），跳过本轮")
        return 1
    if not acquire_lock():
        return 0

    try:
        state = load_json(STATE_FILE, {})
        last_id = 0 if a.scan else int(state.get("last_id") or 0)

        status, data = _req_get(
            "/articles?select=id,title,summary,body,source,source_url,"
            "category,featured,external_heat,is_brief,read_minutes"
            "&order=id.desc&limit=%d" % max(FETCH_LIMIT, a.scan or 0))
        if status != 200 or not isinstance(data, list):
            log("读云端失败（HTTP %s），本轮跳过" % status)
            return 0

        fresh = [x for x in data if int(x.get("id") or 0) > last_id]
        if a.scan:
            fresh = data[:a.scan]
        if not fresh:
            return 0
        log("增量检查 %d 条（断点 id=%d）" % (len(fresh), last_id))

        bag = {}
        all_ops = []
        for art in fresh:
            try:
                all_ops.extend(analyze_article(art, bag))
            except Exception as e:
                log("检查 id=%s 出错：%s" % (art.get("id"), e))

        n_new, n_promoted = learn_patterns(bag)

        if all_ops:
            queue = load_json(QUEUE_FILE, {"pending": []})
            pending = queue.get("pending") or []
            have = {(x.get("id"), x.get("op")) for x in pending}
            added = 0
            for op in all_ops:
                if (op.get("id"), op.get("op")) in have:
                    continue
                if len(pending) >= MAX_QUEUE:
                    log("队列已满 %d 条，暂停累积（请尽快对接）" % MAX_QUEUE)
                    break
                op["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                pending.append(op)
                added += 1
            queue["pending"] = pending
            queue.setdefault("_说明",
                "每分钟本地自检产出的修正队列。每小时由 WorkBuddy 大模型对接："
                "对每条 pending 用 mcp__genie-baas__workbuddy_cloudservice_db_exec_sql"
                "（mode=write）执行 fields 里的 UPDATE（reject 用 DELETE FROM articles WHERE id=...），"
                "处理完把该条从 pending 移到 applied（或直接删除）。")
            queue["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_json(QUEUE_FILE, queue)
            log("产出修正 %d 条（新入队 %d）：%s" % (
                len(all_ops), added,
                " / ".join("%s×%d" % (k, sum(1 for x in all_ops if x["op"] == k))
                           for k in ("reject", "rewrite", "scrub", "compress")
                           if any(x["op"] == k for x in all_ops))))
        else:
            log("检查 %d 条，无问题（候选短语 %d 种）" % (len(fresh), n_new))

        if n_promoted:
            log("规则库晋升 %d 条新噪声短语（已即时生效）" % n_promoted)

        # 更新断点
        max_id = max(int(x.get("id") or 0) for x in fresh)
        state["last_id"] = max(max_id, last_id)
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        state["last_checked"] = len(fresh)
        save_json(STATE_FILE, state)

        # 日志瘦身：只留最近 2000 行
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 2000:
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[-2000:])
        except Exception:
            pass
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
