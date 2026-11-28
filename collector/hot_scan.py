#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热点扫描器：把「同一件事被几家媒体在报」算成可用的热度信号。

为什么需要它
------------
用户要求「实时热点要随热点变化，持续抓全球和国内各种热点，看阅读量」。
站内阅读量在早期只有几十次，撑不起排序；微信阅读量被反爬拦死拿不到。
最可靠、最免费、且真正反映「多少人关注」的信号是：**同一事件被多少家不同媒体报道**。
一家在报是新闻，五家在报就是热点——这正是媒体阅读覆盖的量化代理。

它做什么
--------
1. 抓全球 + 国内出版社 RSS 的最近若干条（复用 collect_global 的源表与抓取器）
2. 按标题聚类，识别「同一事件的多家报道」
3. 对本站已收录的稿子，把媒体家数回填 cross_source_count（写库 SQL）
4. 对本站还没收录的热点，单独列出来 —— 这是「持续抓热点」的选题清单

产出
----
  out/hot_events.json  热点事件清单（已收录 / 未收录分开，带代表链接）
  out/hot_scan.sql     UPDATE articles SET cross_source_count=N 的语句

用法
----
  python hot_scan.py                 # 全量扫描（约 1-2 分钟）
  python hot_scan.py --quick         # 只扫前 16 家，快速看当前热点
  python hot_scan.py --refresh-articles   # 先拉本站最新文章再比对（否则用本地缓存）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
ART_CACHE = os.path.join(OUT, "hot_scan_articles.json")

PK = "wbpk_4fi278HywUucwhoWLRCk74_gReLViFF5413E2eAME6wy8ACnp7XeXLe"
REST = "https://mp-api.app.workbuddy.host/.cloud/database/rest"

# 与前端 utils/hot.js 完全一致的口径：命中 >=5 个词且覆盖率 >=0.55 视为同一件事。
MIN_TOKENS = 8
MIN_HIT = 5
MIN_RATIO = 0.55


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 标题分词
def tokenize(title):
    """中文取二元组、英文取词，和前端 hot.js 的 tokenize 保持同一套口径。"""
    s = str(title or "").lower()
    toks = set()
    for w in re.findall(r"[a-z0-9]{2,}", s):
        toks.add(w)
    for run in re.findall(r"[\u4e00-\u9fa5]+", s):
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    return toks


def same_event(a_toks, b_toks):
    if len(a_toks) < MIN_TOKENS or len(b_toks) < MIN_TOKENS:
        return False
    small, big = (a_toks, b_toks) if len(a_toks) <= len(b_toks) else (b_toks, a_toks)
    hit = len(small & big)
    if hit < MIN_HIT:
        return False
    return hit / float(len(small)) >= MIN_RATIO


# ---------------------------------------------------------------- 抓 RSS
def load_feeds(quick=False):
    """复用 collect_global 的源表：不重复维护一份媒体清单。"""
    try:
        sys.path.insert(0, HERE)
        import collect_global as cg
        feeds = list(cg.PUBLISHER_FEEDS)
        return cg, feeds[:16] if quick else feeds
    except Exception as e:
        log("  [警告] 无法加载 collect_global 的源表：%s" % str(e)[:100])
        return None, []


def grab_headlines(cg, feeds, per_feed=8):
    """抓每家 RSS 最近 per_feed 条。热点通常在头条，不需要深挖历史。"""
    items = []
    for idx, (name, url, _hint) in enumerate(feeds):
        xml = cg.fetch(url, timeout=18, retry=0)
        if not xml:
            continue
        ents = cg.parse_feed(xml)
        got = 0
        for title, link, date, _desc in ents[:per_feed]:
            title = (title or "").strip()
            if len(title) < 10:
                continue
            items.append({
                "source": name,
                "title": title,
                "link": link,
                "date": date,
                "tokens": tokenize(title),
            })
            got += 1
        if got:
            log("  %-22s %d 条" % (name[:22], got))
        time.sleep(0.15)
    return items


# ---------------------------------------------------------------- 本站文章
def fetch_articles(refresh=False):
    """本站最近文章，用来判断热点是否已收录。缓存到本地避免每次打网络。"""
    # 缓存不能永久有效：几小时前还「未收录」的热点，agent 补采完就变成「已收录」了。
    # 用过期缓存会把已补的稿子一直判成缺失，导致重复补采。
    CACHE_TTL = 6 * 3600
    if not refresh and os.path.exists(ART_CACHE):
        try:
            age = time.time() - os.path.getmtime(ART_CACHE)
            if age < CACHE_TTL:
                rows = json.load(open(ART_CACHE, encoding="utf-8"))
                if rows:
                    log("  用本地缓存 %d 篇（%.1f 小时前，--refresh-articles 可强制重拉）"
                        % (len(rows), age / 3600.0))
                    return rows
        except Exception:
            pass
    url = (REST + "/articles?select=id,title,title_cn,source,publish_date,cross_source_count"
           "&limit=500&order=id.desc")
    try:
        req = urllib.request.Request(url, headers={"x-wb-webapp-access-key": PK, "Accept": "application/json"})
        rows = json.loads(urllib.request.urlopen(req, timeout=40).read().decode("utf-8"))
    except Exception as e:
        log("  [警告] 拉本站文章失败：%s" % str(e)[:100])
        return []
    os.makedirs(OUT, exist_ok=True)
    json.dump(rows, open(ART_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    log("  已拉取本站 %d 篇" % len(rows))
    return rows


# ---------------------------------------------------------------- 聚类
def cluster(items):
    """把说同一件事的标题并成簇。朴素两两比较，几百条量级够用。"""
    used = [False] * len(items)
    clusters = []
    for i in range(len(items)):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            if same_event(items[i]["tokens"], items[j]["tokens"]):
                group.append(j)
                used[j] = True
        clusters.append(group)
    return clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只扫前 16 家")
    ap.add_argument("--refresh-articles", action="store_true", help="强制重拉本站文章")
    ap.add_argument("--per-feed", type=int, default=8, help="每家取最近几条")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    log("[热点扫描] 开始")

    cg, feeds = load_feeds(quick=args.quick)
    if not feeds:
        log("没有可用源，退出")
        return 1
    log("-- 抓取 %d 家媒体最新头条 --" % len(feeds))
    items = grab_headlines(cg, feeds, per_feed=args.per_feed)
    log("共 %d 条标题" % len(items))
    if not items:
        log("没抓到内容，退出")
        return 1

    arts = fetch_articles(refresh=args.refresh_articles)
    # 跨语言匹配的关键：外文稿在本站存的是「中文译文 title_cn + 英文原题 title」。
    # 只拿译文去比英文 RSS 标题永远对不上（中文二元组与英文词零交集），
    # 所以两份都建 token，任一命中即算已收录。
    art_toks = []
    for r in arts:
        cn = (r.get("title_cn") or "").strip()
        raw = (r.get("title") or "").strip()
        cands = []
        if cn:
            cands.append(tokenize(cn))
        if raw:
            cands.append(tokenize(raw))
        if cands:
            art_toks.append((r, cands))

    def find_cover(tokens):
        for row, cands in art_toks:
            for tk in cands:
                if same_event(tokens, tk):
                    return row
        return None

    log("-- 聚类识别同一事件 --")
    groups = cluster(items)
    events = []
    for g in groups:
        srcs = {}
        for idx in g:
            srcs[items[idx]["source"]] = 1
        media = len(srcs)
        if media < 2:
            continue

        # 簇里的代表条目：优先取有日期的第一条
        rep = items[g[0]]
        members = [{"source": items[i]["source"], "title": items[i]["title"], "link": items[i]["link"]}
                   for i in g[:12]]

        # 本站是否已收录：拿簇里任意一条标题去比，命中即算收录（中英两份标题都比）。
        hit_art = None
        for idx in g:
            hit_art = find_cover(items[idx]["tokens"])
            if hit_art:
                break

        events.append({
            "title": rep["title"],
            "media_count": media,
            "sources": sorted(srcs.keys()),
            "link": rep["link"],
            "date": rep.get("date", ""),
            "members": members,
            "covered": bool(hit_art),
            "article_id": (hit_art or {}).get("id"),
            "article_title": ((hit_art or {}).get("title_cn") or (hit_art or {}).get("title") or "").strip(),
        })

    events.sort(key=lambda x: (-x["media_count"], x["covered"]))
    covered = [e for e in events if e["covered"]]
    missing = [e for e in events if not e["covered"]]

    log("-- 结果 --")
    log("热点事件 %d 个：已收录 %d，未收录 %d" % (len(events), len(covered), len(missing)))
    for e in events[:12]:
        mark = "已收录#%s" % e["article_id"] if e["covered"] else "未收录"
        log("  %d家 [%s] %s" % (e["media_count"], mark, e["title"][:56]))

    payload = {
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "feeds": len(feeds),
        "headlines": len(items),
        "events": events,
        "missing": missing,
    }
    json.dump(payload, open(os.path.join(OUT, "hot_events.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 写库 SQL：只回填「已收录且家数确有提高」的，不无脑覆盖。
    # 必须用 GREATEST 而不是直接 SET：本轮只扫了一部分源（--quick 更少），
    # 算出来的家数可能低于库里上一轮全量扫描的结果，直接覆盖会把真实热度改小。
    stmts = []
    for e in covered:
        aid = e["article_id"]
        if not aid:
            continue
        stmts.append("UPDATE articles SET cross_source_count = "
                     "GREATEST(COALESCE(cross_source_count,0), %d) WHERE id = %d;"
                     % (e["media_count"], int(aid)))
    with open(os.path.join(OUT, "hot_scan.sql"), "w", encoding="utf-8") as f:
        f.write("\n".join(stmts) + "\n" if stmts else "")
    log("写库语句 %d 条 -> out/hot_scan.sql" % len(stmts))
    log("热点清单 -> out/hot_events.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
