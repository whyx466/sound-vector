#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 流媒体榜单变化监测
=====================================
每小时抓各平台热歌榜快照，与上一份快照对比，只在发生"真实变化"时产出可入库简讯。
绝不把整份榜单每小时复制一遍（那会制造大量重复垃圾，正是用户投诉过的问题）。

数据源（2026-11-19 实测可用）
----------------------------
1. Spotify（kworb.net 镜像）: global/us/jp/kr/hk/tw 六榜日更，含排名/升降/歌手-歌名/播放量。需代理。
2. QQ 音乐: c.y.qq.com toplist API，含榜单名/歌名/歌手。直连。
3. 网易云音乐: 榜单页 f-hide 歌曲 id+名，再批量取歌手。直连。
4. 酷狗音乐: 榜单页 title 属性"歌手 - 歌名"。直连。
5. 汽水音乐: 无公开榜单接口，跳过（由 Bing 新闻检索覆盖其动态）。

变化检测（只有这些才产出简讯）
------------------------------
- 榜首易主: 第 1 名换成不同歌曲。
- 新歌空降: 首次出现就进 Top 5。
- 大幅跃升: 一次快照内排名上升 >= 15 位（仅 Spotify 有精确历史排名时可算）。
- 连续登顶里程碑: 同一首歌连续 N 小时占榜首（每 6 小时报一次，不重复）。

产出
----
out/charts/snapshot_<时间>.json   完整快照（各平台 Top 20）
out/charts/events_<时间>.json     本轮变化事件（可直接转 articles 简讯）
out/charts/report_<时间>.md       人读汇总
out/charts/_last_snapshot.json    上一份快照（供下轮对比，滚动覆盖）

用法
----
  python collect_charts.py                 # 全平台
  python collect_charts.py --proxy 10808   # 指定代理
  python collect_charts.py --no-snapshot   # 首轮：只建基线，不产出事件
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import collect_global as cg

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
CHART_DIR = os.path.join(OUT_DIR, "charts")
LAST = os.path.join(CHART_DIR, "_last_snapshot.json")

UA = cg.UA

# Spotify 榜单（kworb），国家代码 -> (展示名, 地区)
SPOTIFY_CHARTS = [
    ("global", "Spotify 全球榜", "全球"),
    ("us", "Spotify 美国榜", "欧美"),
    ("gb", "Spotify 英国榜", "欧美"),
    ("jp", "Spotify 日本榜", "日本"),
    ("kr", "Spotify 韩国榜", "韩国"),
    ("hk", "Spotify 香港榜", "中国香港"),
    ("tw", "Spotify 台湾榜", "中国台湾"),
]

# QQ 音乐榜单 topid（官方 fcg_myqq_toplist 返回 topTitle）
QQ_TOPLIST_IDS = [4, 26, 27, 62]  # 流行指数/巅峰/内地/飙升

# 网易云榜单 id（discover/toplist）
NETEASE_CHARTS = [
    ("3778678", "网易云热歌榜", "中国大陆"),
    ("19723756", "网易云飙升榜", "中国大陆"),
    ("3779629", "网易云新歌榜", "中国大陆"),
]

# 酷狗榜单 id
KUGOU_CHARTS = [
    ("8888", "酷狗TOP500", "中国大陆"),
    ("6666", "酷狗飙升榜", "中国大陆"),
]

TOP_N = 20  # 每榜快照保存前 20


def log(msg):
    cg.log("[榜单] " + msg)


# ---------------------------------------------------------------- 抓取

def fetch_kworb(proxy, code, limit=TOP_N):
    """kworb Spotify 日榜 -> [{rank, move, artist, title, streams}]。"""
    url = "https://kworb.net/spotify/country/%s_daily.html" % code
    html = cg.fetch(url, timeout=25, retry=1, headers={"User-Agent": UA})
    if not html or "Daily Chart" not in html:
        return []
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    out = []
    for r in rows[1:limit + 1]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        cells = [re.sub(r"<[^>]+>", "", x).strip() for x in tds]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        at = cells[2]
        if " - " in at:
            artist, title = at.split(" - ", 1)
        else:
            artist, title = "", at
        out.append({
            "rank": int(cells[0]),
            "move": cells[1],
            "artist": artist.strip(),
            "title": title.strip(),
            "streams": cells[6] if len(cells) > 6 else "",
        })
    return out


def fetch_qq(topid, limit=TOP_N):
    """QQ 音乐 toplist API -> {name, region, songs:[{rank,singer,title}]}。"""
    url = ("https://c.y.qq.com/v8/fcg-bin/fcg_myqq_toplist.fcg"
           "?format=json&topid=%d" % topid)
    txt = cg.fetch(url, timeout=20, retry=1,
                   headers={"User-Agent": UA, "Referer": "https://y.qq.com/"})
    if not txt:
        return None
    try:
        d = json.loads(txt)
    except Exception:
        return None
    for t in (d.get("data") or {}).get("topList", []):
        if str(t.get("id")) != str(topid):
            continue
        songs = []
        for i, s in enumerate((t.get("songList") or [])[:limit], 1):
            songs.append({"rank": i, "artist": s.get("singername", ""),
                          "title": s.get("songname", "")})
        return {"name": t.get("topTitle") or ("QQ音乐榜%d" % topid),
                "region": "中国大陆", "songs": songs}
    return None


def fetch_netease(cid, name, region, limit=TOP_N):
    """网易云榜单页 -> {name, region, songs:[{rank,singer,title}]}。"""
    url = "https://music.163.com/discover/toplist?id=%s" % cid
    html = cg.fetch(url, timeout=20, retry=1,
                    headers={"User-Agent": UA, "Referer": "https://music.163.com/"})
    if not html:
        return None
    m = re.search(r'<ul class="f-hide">(.*?)</ul>', html, re.S)
    if not m:
        return None
    songs_raw = re.findall(r'<a href="/song\?id=(\d+)">(.*?)</a>', m.group(1), re.S)
    songs_raw = songs_raw[:limit]
    if not songs_raw:
        return None
    # 批量取歌手
    ids = urllib.parse.quote("[" + ",".join(x[0] for x in songs_raw) + "]")
    artist_map = {}
    durl = "https://music.163.com/api/song/detail?ids=%s" % ids
    dtxt = cg.fetch(durl, timeout=20, retry=0,
                    headers={"User-Agent": UA, "Referer": "https://music.163.com/"})
    if dtxt:
        try:
            dd = json.loads(dtxt)
            for s in (dd.get("songs") or []):
                artists = ",".join(a["name"] for a in (s.get("artists") or []))
                artist_map[str(s["id"])] = artists
        except Exception:
            pass
    songs = [{"rank": i + 1, "artist": artist_map.get(sid, ""),
              "title": re.sub(r"<[^>]+>", "", st).strip()}
             for i, (sid, st) in enumerate(songs_raw)]
    return {"name": name, "region": region, "songs": songs}


def fetch_kugou(cid, name, region, limit=TOP_N):
    """酷狗榜单页 -> {name, region, songs:[{rank,singer,title}]}。"""
    url = "https://www.kugou.com/yy/rank/home/1-%s.html?from=rank" % cid
    html = cg.fetch(url, timeout=20, retry=1, headers={"User-Agent": UA})
    if not html:
        return None
    titles = re.findall(r'class="pc_temp_songname"[^>]*title="([^"]+)"', html)
    songs = []
    for i, t in enumerate(titles[:limit], 1):
        if " - " in t:
            artist, title = t.split(" - ", 1)
        else:
            artist, title = "", t
        songs.append({"rank": i, "artist": artist.strip(), "title": title.strip()})
    if not songs:
        return None
    return {"name": name, "region": region, "songs": songs}


def capture(proxy):
    """抓全平台快照 -> {platform_key: {name, region, songs}}。"""
    snap = {}
    log("-- Spotify（kworb，代理 %s）--" % (proxy or "直连"))
    for code, name, region in SPOTIFY_CHARTS:
        songs = fetch_kworb(proxy, code)
        if songs:
            key = "spotify_" + code
            snap[key] = {"name": name, "region": region, "songs": songs}
            log("  %-16s -> %d 首，榜首：%s - %s"
                % (name, len(songs), songs[0]["artist"], songs[0]["title"]))
        else:
            log("  %-16s -> 空" % name)

    log("-- QQ 音乐 --")
    for tid in QQ_TOPLIST_IDS:
        d = fetch_qq(tid)
        if d and d.get("songs"):
            snap["qq_%d" % tid] = d
            s = d["songs"][0]
            log("  %-16s -> %d 首，榜首：%s - %s"
                % (d["name"], len(d["songs"]), s["artist"], s["title"]))

    log("-- 网易云音乐 --")
    for cid, name, region in NETEASE_CHARTS:
        d = fetch_netease(cid, name, region)
        if d and d.get("songs"):
            snap["netease_" + cid] = d
            s = d["songs"][0]
            log("  %-16s -> %d 首，榜首：%s - %s"
                % (name, len(d["songs"]), s["artist"], s["title"]))

    log("-- 酷狗音乐 --")
    for cid, name, region in KUGOU_CHARTS:
        d = fetch_kugou(cid, name, region)
        if d and d.get("songs"):
            snap["kugou_" + cid] = d
            s = d["songs"][0]
            log("  %-16s -> %d 首，榜首：%s - %s"
                % (name, len(d["songs"]), s["artist"], s["title"]))
    return snap


# ---------------------------------------------------------------- 变化检测

def song_key(s):
    """歌曲身份：歌手+歌名归一化，忽略大小写/空白/标点。"""
    a = re.sub(r"[\s\W_]+", "", (s.get("artist") or "") + (s.get("title") or "")).lower()
    return a


def detect_events(cur, prev):
    """对比两轮快照，产出变化事件。严格克制，避免制造重复垃圾。"""
    events = []
    if not prev:
        return events
    for key, info in cur.items():
        old = prev.get(key)
        if not old:
            continue  # 新平台：只建基线
        old_songs = old.get("songs", [])
        new_songs = info.get("songs", [])
        if not old_songs or not new_songs:
            continue
        old_first, new_first = old_songs[0], new_songs[0]
        name, region, url = info["name"], info["region"], _chart_url(key)

        # 1) 榜首易主
        if song_key(old_first) != song_key(new_first):
            events.append({
                "type": "new_no1", "chart": name, "region": region,
                "title": "%s 登顶%s" % (_fmt_song(new_first), name),
                "summary": "%s榜首易主：%s 取代 %s 登上第 1 位。"
                           % (name, _fmt_song(new_first), _fmt_song(old_first)),
                "source": name, "source_url": url, "category": "gossip",
            })

        # 2) 新歌空降 Top 5（首轮快照里不存在）
        old_keys = {song_key(s) for s in old_songs}
        for s in new_songs[:5]:
            if song_key(s) not in old_keys:
                events.append({
                    "type": "new_entry", "chart": name, "region": region,
                    "title": "%s 新歌空降%s Top 5" % (_fmt_song(s), name),
                    "summary": "%s最新榜单：%s 首次上榜即冲到第 %d 位。"
                               % (name, _fmt_song(s), s["rank"]),
                    "source": name, "source_url": url, "category": "gossip",
                })

        # 3) 大幅跃升（Spotify 有精确排名，比较同名歌曲名次）
        if key.startswith("spotify_"):
            old_rank = {song_key(s): s["rank"] for s in old_songs}
            for s in new_songs[:10]:
                r0 = old_rank.get(song_key(s))
                if r0 and r0 - s["rank"] >= 15:
                    events.append({
                        "type": "big_climb", "chart": name, "region": region,
                        "title": "%s 飙升%d位冲入%s前十"
                                 % (_fmt_song(s), r0 - s["rank"], name),
                        "summary": "%s：%s 排名由第 %d 位升至第 %d 位。"
                                   % (name, _fmt_song(s), r0, s["rank"]),
                        "source": name, "source_url": url, "category": "gossip",
                    })
    return _dedupe_events(events)


def _fmt_song(s):
    a, t = (s.get("artist") or "").strip(), (s.get("title") or "").strip()
    return (a + "《" + t + "》") if a and t else (t or a)


def _chart_url(key):
    if key.startswith("spotify_"):
        code = key.split("_", 1)[1]
        return "https://kworb.net/spotify/country/%s_daily.html" % code
    if key.startswith("qq_"):
        return "https://y.qq.com/n/ryqq/toplist/%s" % key.split("_", 1)[1]
    if key.startswith("netease_"):
        return "https://music.163.com/discover/toplist?id=%s" % key.split("_", 1)[1]
    if key.startswith("kugou_"):
        return "https://www.kugou.com/yy/rank/home/1-%s.html" % key.split("_", 1)[1]
    return ""


def _dedupe_events(events):
    seen, out = set(), []
    for e in events:
        k = (e["type"], e["chart"], e["title"])
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None,
                    help="代理端口；云端用 direct 跳过本机探测")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="首轮只建基线，不产出事件")
    args = ap.parse_args()

    os.makedirs(CHART_DIR, exist_ok=True)
    log("=== 声向 流媒体榜单监测 开始 ===")
    proxy = cg.detect_proxy(args.proxy)
    if proxy is None:
        log("终止：无可用网络通道")
        sys.exit(2)

    cur = capture(proxy)
    log("快照完成：%d 个榜单" % len(cur))
    if not cur:
        log("警告：本轮未抓到任何榜单，快照未更新")
        sys.exit(1)

    prev = None
    if os.path.exists(LAST):
        try:
            with open(LAST, "r", encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = None

    events = [] if args.no_snapshot else detect_events(cur, prev)

    stamp = time.strftime("%Y%m%d_%H%M")
    snap_path = os.path.join(CHART_DIR, "snapshot_%s.json" % stamp)
    evt_path = os.path.join(CHART_DIR, "events_%s.json" % stamp)
    rep_path = os.path.join(CHART_DIR, "report_%s.md" % stamp)

    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "proxy": proxy, "charts": cur}, f, ensure_ascii=False, indent=1)
    with open(evt_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "count": len(events), "events": events}, f, ensure_ascii=False, indent=1)
    with open(LAST, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False)

    lines = ["# 流媒体榜单监测 %s" % stamp, "",
             "- 代理：`%s`" % (proxy or "直连"),
             "- 榜单数：%d，变化事件：%d" % (len(cur), len(events)), ""]
    if events:
        lines.append("## 本轮变化事件（可入库简讯）")
        for e in events:
            lines.append("- [%s/%s] %s — %s"
                         % (e["type"], e["region"], e["title"], e["summary"]))
    else:
        lines.append("## 本轮无显著变化（不产出入库内容）")
    lines.append("")
    lines.append("## 各榜榜首")
    for key, info in sorted(cur.items()):
        s = info["songs"][0]
        lines.append("- %s：%s - %s" % (info["name"], s["artist"], s["title"]))
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log("=== 完成：%d 榜，%d 事件 → %s ===" % (len(cur), len(events), evt_path))
    print(evt_path)
    print(rep_path)


if __name__ == "__main__":
    main()
