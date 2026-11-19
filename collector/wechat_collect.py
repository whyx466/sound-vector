# -*- coding: utf-8 -*-
"""公众号音乐资讯批量采集（近一个月）。

用户诉求：一切音乐相关的公众号，近一个月的资讯尽可能收集录入。
通道：搜狗微信文章搜索（weixin.sogou.com type=2）→ 跟随 /link?url= 拿
mp.weixin.qq.com 真链 → 抓正文与发布时间 → 近 30 天 + 音乐相关性过滤 →
产出 batch_wechat_<ts>.json，交给 cloud_writer.py 入库（自带去重/翻译）。

用法：
  python wechat_collect.py                 # 全量跑
  python wechat_collect.py --pages 1       # 每词只翻 1 页（快速抽样）
  python wechat_collect.py --dry-run       # 只打印，不写文件
"""
import argparse
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_global as cg  # 复用 music_relevant / guess_category / guess_region

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 音乐关键词簇：小到民乐/古琴，大到产业/流媒体，尽量覆盖公众号生态。
KEYWORDS = [
    "音乐", "演唱会", "音乐节", "音乐产业", "唱片", "流媒体音乐",
    "说唱", "摇滚", "爵士乐", "古典音乐", "民乐", "古琴",
    "音乐剧", "音乐教育", "音乐版权", "电子音乐", "乐队", "专辑",
    "歌手", "音乐人", "原声带", "音乐科技", "民歌", "非遗 音乐",
    "音乐颁奖", "音乐榜单", "演出市场", "音乐综艺",
]
# 标题/摘要必须像音乐稿；复用 collect_global 的两级词表。
CUTOFF_DAYS = 30


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


def http_get(url, referer=None, timeout=25, tries=2):
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace"), r.geturl()
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    return None, str(last) if last else "unknown"


def sogou_page(query, page=1):
    q = urllib.parse.quote(query)
    url = ("https://weixin.sogou.com/weixin?type=2&query=%s&page=%d&ie=utf8" % (q, page))
    return http_get(url, referer="https://weixin.sogou.com/")


def parse_sogou(html):
    """解析搜狗微信结果页：标题 / 摘要 / 公众号 / 日期 / 跳转链接。"""
    out = []
    if not html:
        return out
    if "请输入验证码" in html or "antispider" in html:
        raise RuntimeError("sogou-captcha")
    # 每条结果一个 <div class="txt-box">… 区块；宽松切分后逐块提取
    blocks = re.split(r'<div class="txt-box">', html)[1:]
    for b in blocks:
        m = re.search(r'<h3>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        href, raw_title = m.group(1), m.group(2)
        title = re.sub(r"<[^>]+>", "", raw_title)
        title = title.replace("&ldquo;", "“").replace("&rdquo;", "”").replace("&hellip;", "…").replace("&amp;", "&").strip()
        sm = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', b, re.S)
        summary = re.sub(r"<[^>]+>", "", sm.group(1)) if sm else ""
        summary = summary.replace("&ldquo;", "“").replace("&rdquo;", "”").replace("&hellip;", "…").replace("&amp;", "&").strip()
        am = re.search(r'<a[^>]*class="account"[^>]*>(.*?)</a>', b)
        account = re.sub(r"<[^>]+>", "", am.group(1)).strip() if am else ""
        dm = re.search(r'timeConvert\(\'(\d+)\'\)|<span class="s2">([^<]+)</span>', b)
        date_str = ""
        if dm:
            if dm.group(1):
                date_str = datetime.fromtimestamp(int(dm.group(1))).strftime("%Y-%m-%d")
            else:
                date_str = dm.group(2).strip()
        if href.startswith("/link"):
            href = "https://weixin.sogou.com" + href.replace("&amp;", "&")
        out.append({"title": title, "summary": summary, "account": account,
                    "date": date_str, "link": href})
    return out


def parse_sogou_date(s):
    """搜狗日期可能是 '2026年11月18日' / '昨天' / '4小时前' / 空。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    now = datetime.now()
    if "小时前" in s or "小时" in s:
        return now.strftime("%Y-%m-%d")
    if "天前" in s:
        mm = re.search(r"(\d+)\s*天前", s)
        d = now - timedelta(days=int(mm.group(1)) if mm else 1)
        return d.strftime("%Y-%m-%d")
    if "昨天" in s:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def resolve_mp(sogou_link):
    """跟随搜狗 /link?url= 跳转，拿到 mp.weixin.qq.com 真链。"""
    html, final = http_get(sogou_link, referer="https://weixin.sogou.com/")
    if final and "mp.weixin.qq.com" in final:
        return final.split("#")[0]
    # 有些返回带 JS 跳转的 HTML
    if html:
        m = re.search(r'(https?://mp\.weixin\.qq\.com/s[^"\'\s<>]+)', html)
        if m:
            return m.group(1).split("#")[0]
    return None


def fetch_mp(url):
    """抓 mp.weixin.qq.com 文章：标题 / 公众号 / 发布时间 / 正文。"""
    html, _ = http_get(url, timeout=30)
    if not html or "已停止访问" in html or "该内容已被发布者删除" in html:
        return None

    def clean(s):
        return re.sub(r"<[^>]+>", "", s or "").strip()

    tm = re.search(r'var msg_title = htmlDecode\("([^"]*)"\)', html)
    title = clean(tm.group(1)) if tm else ""
    if not title:
        hm = re.search(r'id="activity-name"[^>]*>(.*?)</h1>', html, re.S) or \
             re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        title = clean(hm.group(1)) if hm else ""
    nm = re.search(r'var nickname = htmlDecode\("([^"]*)"\)', html)
    account = clean(nm.group(1)) if nm else ""
    ct = re.search(r'var ct = "(\d+)"', html)
    pub = datetime.fromtimestamp(int(ct.group(1))).strftime("%Y-%m-%d") if ct else ""
    cm = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', html, re.S)
    body = ""
    if cm:
        text = re.sub(r"<[^>]+>", "\n", cm.group(1))
        import html as htmllib
        text = htmllib.unescape(text)
        body = "\n".join(l.strip() for l in text.splitlines() if l.strip())
    return {"title": title, "account": account, "publish_date": pub, "body": body}


def estimate_minutes(body):
    n = len(body or "")
    return min(9, max(1, n // 350))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="每个词翻几页搜狗结果")
    ap.add_argument("--max-fetch", type=int, default=140, help="最多抓多少篇正文")
    ap.add_argument("--links-file", default=None,
                    help="直接读 mp.weixin.qq.com 链接清单（每行一条，# 注释），跳过搜狗搜索")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = (datetime.now() - timedelta(days=CUTOFF_DAYS)).strftime("%Y-%m-%d")
    log("近一个月公众号音乐资讯采集 | 截止日期 %s" % cutoff)

    cands = []
    if args.links_file:
        # 直链模式：链接清单来自搜索工具（支持公众号源），只做正文抓取与过滤。
        log("直链模式：%s" % args.links_file)
        with open(args.links_file, encoding="utf-8") as f:
            for line in f:
                u = line.strip().split("#")[0].strip()
                if not u.startswith("http"):
                    continue
                if "mp.weixin.qq.com" not in u:
                    continue
                cands.append({"title": "", "summary": "", "account": "",
                              "date": "", "link": u})
        log("载入链接 %d 条" % len(cands))
    else:
        seen = set()
        captcha_hits = 0
        for kw in KEYWORDS:
            for p in range(1, args.pages + 1):
                try:
                    html, _ = sogou_page(kw, p)
                    items = parse_sogou(html)
                except RuntimeError:
                    captcha_hits += 1
                    log("!! 搜狗验证码（第 %d 次），暂停 45s 继续" % captcha_hits)
                    time.sleep(45)
                    continue
                except Exception as e:
                    log("搜狗异常 %s: %s" % (kw, str(e)[:60]))
                    items = []
                fresh = 0
                for it in items:
                    t = it["title"]
                    if not t or t in seen:
                        continue
                    seen.add(t)
                    d = parse_sogou_date(it["date"])
                    # 搜狗日期解析不出的也先留着（正文里有准确 ct）；明确太旧的丢掉
                    if d and d < cutoff:
                        continue
                    # 首轮音乐相关性（标题+摘要）
                    if not cg.music_relevant(t, it["summary"]):
                        continue
                    it["_date"] = d
                    cands.append(it)
                    fresh += 1
                log("  [%s p%d] 结果 %d 条 -> 新增候选 %d（累计 %d）" % (kw, p, len(items), fresh, len(cands)))
                time.sleep(random.uniform(1.6, 3.0))
            if len(cands) >= args.max_fetch * 2:
                log("候选已够，提前结束搜索")
                break

    log("候选 %d 条，开始抓正文（最多 %d 篇）" % (len(cands), args.max_fetch))
    rows = []
    for i, c in enumerate(cands[: args.max_fetch], 1):
        mp_url = resolve_mp(c["link"])
        time.sleep(random.uniform(0.6, 1.4))
        if not mp_url:
            continue
        art = fetch_mp(mp_url)
        time.sleep(random.uniform(0.6, 1.3))
        if not art or not art["title"]:
            continue
        pub = art["publish_date"] or c.get("_date") or ""
        if pub and pub < cutoff:
            continue
        body = art["body"] or ""
        # 二次音乐相关性：正文前 200 字一起判
        if not cg.music_relevant(art["title"], (c["summary"] + " " + body[:200])):
            continue
        if not pub:
            pub = datetime.now().strftime("%Y-%m-%d")
        rows.append({
            "title": art["title"][:200],
            "summary": (c["summary"] or body[:120])[:400],
            "body": body[:4000],
            "source": (art["account"] or c["account"] or "微信公众号")[:60],
            "source_url": mp_url,
            "publish_date": pub,
            "category": cg.guess_category(art["title"], body[:300]) or "gossip",
            "region": cg.guess_region(art["title"], body[:300], mp_url),
            "read_minutes": estimate_minutes(body),
            "is_brief": len(body) < 120,
            "via": "wechat",
        })
        if i % 20 == 0:
            log("  正文进度 %d/%d，通过 %d 条" % (i, min(len(cands), args.max_fetch), len(rows)))

    log("正文抓取完成：入库候选 %d 条" % len(rows))
    by_cat = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    log("分类分布：%s" % by_cat)

    if args.dry_run:
        for r in rows[:15]:
            log("  [dry] %s | %s | %s" % (r["category"], r["title"][:50], r["publish_date"]))
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(OUT_DIR, "batch_wechat_%s.json" % ts)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"articles": rows, "via": "wechat", "generated": ts}, f, ensure_ascii=False, indent=1)
    log("已写 %s（%d 条）" % (path, len(rows)))


if __name__ == "__main__":
    main()
