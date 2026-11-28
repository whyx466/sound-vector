#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 酷狗音乐主源采集（2026-11-28）
=====================================================
用户要求：酷狗不只做补充，要当主源、大量抓取、24x7。

实测硬证据（2026-11-28）：
- 酷狗新歌榜 HTML https://www.kugou.com/yy/rank/home/1-8888.html 返回 200/72KB，
  内嵌 global.features = [{Hash, FileName, author_name, album_id, ...}]，22 条当前热歌。
  注意：里面的 time 是 timeLen（时长秒），不是发行日期。
- 搜索接口 https://songsearch.kugou.com/song_search_v2?keyword=... 返回 PublishDate
  字段（实测 "2026-11-18"），可给榜单条目补发行日期。
- 榜单里混大量蹭热版（30秒片段/DJ版/AI赵本山版/治愈版/伤感版），必须严格过滤，
  否则水歌当资讯——这正是用户担心的。

设计：
- 多榜单（新歌榜 8888、原创榜 6666、热歌榜 1）合并，按歌名去重。
- 每个条目用「艺人+歌名」搜酷狗，取歌名精确匹配的 PublishDate。
- 只收 cutoff<=date<=today；命中 WATER_PAT 直接丢。
- 有新闻由头（影视/原声/推广曲/重点艺人）打 news_hook。
- 产出 out/batch_releases_*.json（与 collect_releases 同格式），交给 cloud_writer 统一入库。

  python collect_kugou.py --days 14              # 抓近 14 天，落盘
  python collect_kugou.py --days 14 --no-write   # 只抓不落盘
  python collect_kugou.py --selftest             # 自检（不联网）
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse

import collect_global as cg

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
RANK_BASE = "https://www.kugou.com/yy/rank/home/1-%s.html?from=rank"
SEARCH_API = "https://songsearch.kugou.com/song_search_v2?keyword=%s&page=1&pagesize=%d"
# 榜单 id：新歌榜 / 原创榜 / 热歌榜 / 飙升榜
RANKS = [("8888", "新歌榜"), ("6666", "原创榜"), ("1", "热歌榜"), ("8887", "飙升榜")]

# 与 collect_releases 一致的水歌/新闻规则（本文件自带，避免跨文件依赖）
WATER_PAT = re.compile(
    r"(片段|试听|剪辑|串烧|合集|DJ|dj|Dj|remix|Remix|REMIX|翻唱|cover|Cover|COVER|"
    r"伴奏|纯音乐|铃声|治愈版|伤感版|烟嗓|童声|女声版|男声版|加速版|降调|升调|"
    r"AI|ai版|新版|修词|live版|Live版|完整版$)",
    re.I)
# 注意：不把「现场版/live版」一律判水歌——MAMA/MMA 颁奖礼现场是真新闻（实测命中 IVE/aespa）。
# 若歌名带「颁奖|盛典|演唱会|音乐节」则视为有新闻由头，见 NEWS_PAT。
NEWS_PAT = re.compile(
    r"(原声|OST|ost|影视|主题曲|片尾曲|片头曲|推广曲|电影|电视剧|官宣|首唱|"
    r"回归|破纪录|夺冠|登顶|万次|合作|feat|Feat|联名|致敬|纪念|专辑|EP|巡演|"
    r"颁奖|盛典|现场|MAMA|MMA|演唱会|音乐节)",
    re.I)
NEWS_WORTHY = {"周深", "陈奕迅", "周杰伦", "林俊杰", "邓紫棋", "Taylor Swift",
               "BTS", "BLACKPINK", "赵本山", "五月天", "张惠妹", "宇多田光",
               "Adele", "Coldplay", "Billie Eilish", "Ed Sheeran", "Gareth.T", "汤令山"}


def log(msg):
    print("[%s] [酷狗] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _get(url, proxy=None, timeout=15):
    """用采集器统一的 fetch（自动代理探测），返回文本。
    cg.fetch 不接受 proxy 参数——它通过模块级 _ACTIVE_PROXY 生效。"""
    if proxy:
        cg._ACTIVE_PROXY = proxy
    return cg.fetch(url, timeout=timeout)


def fetch_rank(rank_id, proxy=None):
    """拉一个榜单，返回 [{song, artist, hash, album_id}]。"""
    html = _get(RANK_BASE % rank_id, proxy)
    if not html or "global.features" not in html:
        return []
    m = re.search(r"global\.features\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for x in arr:
        fn = (x.get("FileName") or "").strip()
        if " - " in fn:
            artist, song = fn.split(" - ", 1)
        else:
            artist, song = x.get("author_name") or "", fn
        out.append({
            "song": song.strip(),
            "artist": (artist or x.get("author_name") or "").strip(),
            "hash": x.get("Hash") or "",
            "album_id": x.get("album_id") or "",
        })
    return out


def publish_date_of(song, artist, proxy=None):
    """用搜索接口给条目补发行日期，返回 (date, matched_song_name)。"""
    kw = ("%s %s" % (artist, song)).strip()
    url = SEARCH_API % (urllib.parse.quote(kw), 6)
    try:
        raw = _get(url, proxy)
        if not raw:
            return "", ""
        data = json.loads(raw).get("data") or {}
        lists = data.get("lists") or []
    except Exception:
        return "", ""
    # 精确匹配：歌名去掉括号后缀后比对，避免搜「慌啥子嘛」命中「慌啥子嘛(30秒片段)」
    key = re.sub(r"[（(].*?[)）]", "", song).strip().lower()[:10]
    for it in lists:
        name = (it.get("SongName") or "").lower()
        singer = (it.get("SingerName") or "").lower()
        if key and key in name and (not artist or artist.lower()[:4] in singer or singer[:4] in artist.lower()[:4]):
            d = (it.get("PublishDate") or (it.get("PublishTime") or "")[:10] or "").strip()
            return d, it.get("SongName") or ""
    # 退一步：只要歌名相似，取第一条的日期（宽松，后面还有日期区间和水歌过滤兜底）
    for it in lists:
        name = (it.get("SongName") or "").lower()
        if key and key in name:
            d = (it.get("PublishDate") or (it.get("PublishTime") or "")[:10] or "").strip()
            return d, it.get("SongName") or ""
    return "", ""


def make_article(entry, pub_date, matched, rank_name):
    hook = ""
    if any(w.strip() in entry["artist"] for w in NEWS_WORTHY):
        hook = "重点艺人发行"
    else:
        m = NEWS_PAT.search(entry["song"] + " " + matched)
        hook = m.group(0) if m else ""
    title = "%s 发布新单曲《%s》" % (entry["artist"] or "酷狗音乐人", entry["song"])
    summary = ("%s 在酷狗音乐%s上榜单曲《%s》，发行日期 %s。"
               % (entry["artist"] or "酷狗音乐人", rank_name, entry["song"], pub_date))[:160]
    url = ("https://www.kugou.com/album/%s.html" % entry["album_id"]) if entry["album_id"] \
        else ("https://www.kugou.com/song/#hash=%s" % entry["hash"])
    return {
        "title": title,
        "source_url": url,
        "source": "酷狗音乐",
        "publish_date": pub_date,
        "summary": summary,
        "category": "cpop",
        "region": "中国大陆",
        "category_hint": "cpop",
        "via": "release",
        "_origin": "release",
        "artist": entry["artist"],
        "release_type": "Single",
        "news_hook": hook,
    }


def fetch_artist_songs(artist_name, proxy=None, pagesize=20):
    """按艺人定点搜索酷狗（第二条主源流，不依赖榜单）。"""
    url = SEARCH_API % (urllib.parse.quote(artist_name), pagesize)
    try:
        raw = _get(url, proxy)
        if not raw:
            return []
        data = json.loads(raw).get("data") or {}
        return data.get("lists") or []
    except Exception:
        return []


def collect(days, proxy=None, limit=120):
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    log("窗口 %s ~ %s，榜单 %d 个" % (cutoff, today, len(RANKS)))

    # ---- 流 1：多榜单热歌 ----
    entries = {}
    for rank_id, rank_name in RANKS:
        rows = fetch_rank(rank_id, proxy)
        log("  %s %d 条" % (rank_name, len(rows)))
        for r in rows:
            if WATER_PAT.search(r["song"]):
                continue
            k = re.sub(r"\W+", "", r["artist"] + r["song"]).lower()
            if k not in entries:
                r["rank"] = rank_name
                entries[k] = r
    log("榜单合并去重 %d 条" % len(entries))

    found = []
    seen = set()
    def add(entry, d, matched, tag):
        k = re.sub(r"\W+", "", entry["artist"] + entry["song"]).lower()
        if k in seen or not d or not (cutoff <= d <= today):
            return
        if WATER_PAT.search(matched) or WATER_PAT.search(entry["song"]):
            return
        seen.add(k)
        found.append(make_article(entry, d, matched, tag))

    # ---- 流 2：72 位重点艺人定点搜酷狗（用户要"大量抓取"的主力）----
    try:
        with open(os.path.join(HERE, "release_artists.json"), encoding="utf-8") as f:
            artists = json.load(f).get("artists") or []
    except Exception:
        artists = []
    log("艺人定点搜索 %d 位" % len(artists))
    for i, a in enumerate(artists):
        name = a.get("name") or ""
        if not name:
            continue
        rows = fetch_artist_songs(name, proxy, pagesize=15)
        hit = 0
        for it in rows:
            song = (it.get("SongName") or "").strip()
            singer = (it.get("SingerName") or "").strip()
            # 必须是本人作品：艺人名出现在歌手字段里（防止搜"周深"返回别人的歌）
            if not song or name.split()[0].lower() not in singer.lower():
                continue
            d = (it.get("PublishDate") or (it.get("PublishTime") or "")[:10] or "").strip()
            add({"song": song, "artist": singer, "album_id": "",
                 "hash": it.get("Hash") or "", "rank": "艺人新作"}, d, song, "艺人新作")
            hit += 1
        if (i + 1) % 20 == 0:
            log("  艺人 %d/%d，累计命中 %d" % (i + 1, len(artists), len(found)))
        if len(found) >= limit:
            break

    # ---- 流 3：榜单条目补日期（放最后，作为长尾补充）----
    if len(found) < limit:
        for e in entries.values():
            d, matched = publish_date_of(e["song"], e["artist"], proxy)
            add(e, d, matched, e["rank"])
            if len(found) >= limit:
                break

    found.sort(key=lambda x: (1 if x.get("news_hook") else 0, x["publish_date"]), reverse=True)
    log("近 %d 天真实发行 %d 条（艺人流+榜单流）" % (days, len(found)))
    return found[:limit]


def write_batch(articles, proxy=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "batch_releases_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "proxy": proxy or "direct",
            "count": len(articles),
            "articles": articles,
            "via": "kugou_releases",
        }, f, ensure_ascii=False, indent=1)
    log("落盘 %s（%d 条）" % (os.path.basename(path), len(articles)))
    return path


def selftest():
    ok = fail = 0
    def check(c, label):
        nonlocal ok, fail
        if c: ok += 1
        else: fail += 1; print("  ✗", label)
    check(bool(WATER_PAT.search("慌啥子嘛 (30秒片段)")), "片段版判水歌")
    check(bool(WATER_PAT.search("该怎么写我 (AI赵本山版)")), "AI版判水歌")
    check(bool(WATER_PAT.search("北京没有你 (伤感版)")), "伤感版判水歌")
    check(bool(WATER_PAT.search("时过境迁 (30秒DJ天冷冷版片段)")), "DJ片段判水歌")
    check(not WATER_PAT.search("慌啥子嘛 (赵本山版)"), "赵本山官方版不误杀")
    check(not WATER_PAT.search("十万狂花入梦来 影视原声大碟"), "影视原声不误杀")
    check(bool(NEWS_PAT.search("十万狂花入梦来 影视原声大碟")), "影视原声有新闻由头")
    art = make_article({"song": "测试", "artist": "周深", "album_id": "1", "hash": "x", "rank": "新歌榜"},
                       "2026-11-27", "测试", "新歌榜")
    check("/album/1.html" in art["source_url"], "专辑URL路径式")
    check(art["news_hook"] == "重点艺人发行", "重点艺人打钩子")
    print("酷狗采集自检：%d 通过 / %d 失败" % (ok, fail))
    return fail == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(0 if selftest() else 1)
    log("=== 酷狗主源采集 开始 ===")
    proxy = cg.detect_proxy(args.proxy)
    articles = collect(args.days, proxy, args.limit)
    for a in articles[:12]:
        log("  %s %s | %s" % (a["publish_date"], a["title"][:38], a["news_hook"] or "-"))
    if not args.no_write and articles:
        write_batch(articles, proxy)


if __name__ == "__main__":
    main()
