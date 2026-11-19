#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 云端数据库查询器
====================================
只读。不用装任何第三方库，只用 Python 标准库。

用法
----
  python query.py                     # 最近 15 条
  python query.py -n 30               # 最近 30 条
  python query.py -r 新加坡            # 只看某地区（region 精确匹配）
  python query.py -s 格莱美            # 标题/摘要关键词（中英文都能搜）
  python query.py -c kpop             # 按分类
  python query.py --since 2026-11-18  # 某天之后发布的
  python query.py --stats             # 总量统计：各地区 / 各分类 / 今日新增

密钥来源（与 cloud_writer.py 相同，按顺序）：
  1) 环境变量 WB_KEY
  2) 同目录 .wb_key
  3) ../.workbuddy/.wb_key
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REST = os.environ.get("WB_REST_URL",
                      "https://mp-api.app.workbuddy.host/.cloud/database/rest").rstrip("/")

SELECT = ("id,title,title_cn,summary,summary_cn,source,source_url,region,"
          "category,publish_date,featured,view_count,external_heat")

CN_ONLY = None  # 延迟加载，避免无 tty 环境里 import 报错


def load_key():
    k = os.environ.get("WB_KEY", "").strip()
    if k:
        return k
    for p in (os.path.join(HERE, ".wb_key"),
              os.path.join(HERE, "..", ".workbuddy", ".wb_key")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            continue
    return ""


KEY = load_key()


def get(path):
    url = REST + path
    req = urllib.request.Request(url, headers={
        "x-wb-webapp-access-key": KEY,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "ignore")
            return True, (json.loads(raw) if raw.strip() else [])
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        return False, str(e)[:200]


def q(v, safe="*"):
    return urllib.parse.quote(str(v), safe=safe)


def build_query(args):
    cols = "select=" + q(SELECT, safe="(),*")
    params = [cols, "order=id.desc", "limit=%d" % args.limit]
    if args.region:
        params.append("region=eq." + q(args.region, safe=""))
    if args.category:
        params.append("category=eq." + q(args.category, safe=""))
    if args.since:
        params.append("publish_date=gte." + q(args.since, safe="-"))
    if args.search:
        kw = q(args.search)
        # 中英文标题、摘要任一命中即可
        params.append("or=(title.ilike.*%s*,title_cn.ilike.*%s*,summary.ilike.*%s*)"
                      % (kw, kw, kw))
    return "/articles?" + "&".join(params)


def title_of(r):
    return (r.get("title_cn") or "").strip() or (r.get("title") or "").strip()


def show(rows):
    if not rows:
        print("没有匹配的稿件。")
        return
    for r in rows:
        flag = "★" if r.get("featured") else " "
        print("%s #%-5s %s | %-6s | %-10s | %s" % (
            flag,
            r.get("id"),
            (r.get("publish_date") or "")[:10],
            (r.get("region") or "-")[:6],
            (r.get("category") or "-"),
            title_of(r)[:52],
        ))
    print("\n共 %d 条（★=编辑置顶）" % len(rows))


def stats():
    ok, rows = get("/articles?select=region,category,publish_date&limit=8000")
    if not ok:
        print("读取失败：%s" % rows)
        return
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    by_region, by_cat = {}, {}
    today_n = 0
    for r in rows:
        by_region[r.get("region") or "未标注"] = by_region.get(r.get("region") or "未标注", 0) + 1
        by_cat[r.get("category") or "未分类"] = by_cat.get(r.get("category") or "未分类", 0) + 1
        if (r.get("publish_date") or "")[:10] == today:
            today_n += 1

    print("总条数：%d" % len(rows))
    print("今日（北京 %s）新增：%d" % (today, today_n))
    print("\n按地区：")
    for k, v in sorted(by_region.items(), key=lambda x: -x[1]):
        print("  %-10s %4d" % (k, v))
    print("\n按分类：")
    for k, v in sorted(by_cat.items(), key=lambda x: -x[1]):
        print("  %-12s %4d" % (k, v))


def main():
    ap = argparse.ArgumentParser(description="声向云端库查询（只读）")
    ap.add_argument("-n", "--limit", type=int, default=15)
    ap.add_argument("-r", "--region", default=None)
    ap.add_argument("-c", "--category", default=None)
    ap.add_argument("-s", "--search", default=None, help="关键词，中英文均可")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD，只看这之后发布的")
    ap.add_argument("--stats", action="store_true", help="只输出统计")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    if not KEY:
        print("缺少访问密钥：请设置环境变量 WB_KEY，或把密钥写进 采集器/.wb_key")
        return

    if args.stats:
        stats()
        return

    ok, data = get(build_query(args))
    if not ok:
        print("查询失败：%s" % data)
        return
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        show(data)


if __name__ == "__main__":
    main()
