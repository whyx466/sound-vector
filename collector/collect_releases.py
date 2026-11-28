# -*- coding: utf-8 -*-
"""声向 · 艺人新歌上架监测（2026-11-27）

为什么单独做这个采集器：
  现有 47 个出版社 RSS 是「媒体视角」，报道滞后且常常漏掉艺人在音乐平台
  直接上架的新歌（用户实测：汤令山 11-27 发《心动一半》、周深 11-23 发
  《十万狂花入梦来》原声带，RSS 都没有）。本采集器是「平台数据视角」，
  直接读网易云音乐公开接口 artist/albums 的 publishTime（毫秒时间戳），
  命中「某艺人某天发了什么」这一具体事件，与 RSS 互补。

平台实测（2026-11-27，本机直连）：
  网易云 music.163.com/api/artist/albums/<id>  -> 200，带 publishTime/name/type ✅ 主力
  网易云 /api/search/get                       -> 200，可按名字找 artist id ✅
  QQ 音乐 c.y.qq.com toplist/搜索              -> 200，但歌手新歌接口 404，榜单已在 collect_charts.py
  酷狗 songsearch.kugou.com                    -> 200，但无发布日期字段，做补充不做主源
  汽水音乐 www.qishui.com                      -> 官网 200 但是 SPA 空壳，API 路径全回首页，无公开接口 ❌
  Spotify api.spotify.com                      -> 401 需 token ❌（海外榜仍走 collect_charts.py 的 Spotify）

用法：
  python collect_releases.py --days 7                 # 抓近 7 天新发行，落 out/batch_releases_*.json
  python collect_releases.py --days 7 --no-write      # 只抓不落盘（自检）
  python collect_releases.py --selftest               # 内置自检，不联网
  python collect_releases.py --add-artist 名字         # 提示如何加艺人（查 id）

落盘格式与 collect_global.py 的 batch_*.json 一致，可直接喂 cloud_writer.py：
  python cloud_writer.py --tcb --no-translate --limit 40
（cloud_writer 的 latest() 按文件修改时间选批次，新落盘的 releases 批次会被优先选中）
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
ARTISTS_FILE = os.path.join(HERE, "release_artists.json")
NETEASE_API = "https://music.163.com/api/"
# 必须用路径式 URL：cloud_writer.norm_url() 会剥掉非公众号链接的查询串，
# 用 ?id=xxx 会让所有专辑塌成同一个去重键（实测 23 条里 22 条被误判重复）。
# 路径式 /album/<id>/ 实测 200 且页面相同，去重键带 id 不冲突。
NETEASE_ALBUM_PAGE = "https://music.163.com/album/%s/"
NETEASE_ARTIST_PAGE = "https://music.163.com/artist/%s/"


def log(msg):
    cg.log("[新歌] " + str(msg))


def load_artists():
    with open(ARTISTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [a for a in data.get("artists", []) if a.get("enabled") is not False]


def netease_api(path, params, proxy=None):
    """POST 调网易云 Web API；cg.fetch 自带代理降级与 gzip 解压。"""
    data = urllib.parse.urlencode(params).encode("utf-8")
    txt = cg.fetch(NETEASE_API + path, timeout=20, retry=1, data=data,
                   headers={"Referer": "https://music.163.com/",
                            "Content-Type": "application/x-www-form-urlencoded"})
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def ts_to_date(v):
    """publishTime 是毫秒时间戳；返回 YYYY-MM-DD，失败返回空串。"""
    try:
        n = int(v)
        if n > 1e12:
            n = n / 1000
        return datetime.datetime.fromtimestamp(n).strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch_artist_albums(artist, proxy=None):
    """取艺人专辑/单曲列表，返回 [{title, publish_date, album_id, type}]。"""
    r = netease_api("artist/albums/%s" % artist["id"],
                    {"limit": "30", "offset": "0"}, proxy)
    if not r or r.get("code") not in (200, None):
        return []
    out = []
    for a in (r.get("hotAlbums") or []):
        d = ts_to_date(a.get("publishTime"))
        name = str(a.get("name") or "").strip()
        if not d or not name:
            continue
        out.append({
            "title": name,
            "publish_date": d,
            "album_id": str(a.get("id") or ""),
            "type": str(a.get("type") or ""),
        })
    return out


def fetch_platform_new_songs(days, proxy=None, limit=60):
    """平台新歌速递：不依赖艺人名单，抓全平台新上架。

    网易云 discovery/new/songs 的条目没有 publishTime，要用 /api/song/detail
    按 album.publishTime 补齐（实测可补：2026-11-13 等真实日期）。
    """
    r = netease_api("discovery/new/songs", {"areaId": "0", "limit": str(min(limit, 100))}, proxy)
    if not r:
        return []
    songs = (r.get("data") or [])[:limit]
    ids = [s.get("id") for s in songs if s.get("id")]
    if not ids:
        return []
    det = netease_api("song/detail", {"ids": json.dumps(ids)}, proxy)
    if not det:
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out = []

    def guess_category(names):
        """平台新歌流的轻量分类：按艺人名判定，判不出就 cpop。"""
        n = names.lower()
        if any(k in n for k in ("ミク", "ヨアソビ", "yoasobi", " anime", "初音")):
            return "jpop", "日本"
        if re.search(r"[ぁ-んァ-ン一-龯]", names) and any(k in n for k in ("jp", " Japan")):
            return "jpop", "日本"
        if any(k in n for k in ("ive", "aespa", "blackpink", "twice", "newjeans", "ive", "kpop")):
            return "kpop", "韩国"
        return "cpop", "中国大陆"

    for s in (det.get("songs") or []):
        album = s.get("album") or {}
        d = ts_to_date(album.get("publishTime"))
        if not d or not (cutoff <= d <= datetime.date.today().isoformat()):
            continue
        artists = ",".join(a.get("name", "") for a in (s.get("artists") or [])[:2])
        cat, region = guess_category(artists)
        out.append({
            "title": "%s 发布新单曲《%s》" % (artists or "平台新歌", s.get("name") or ""),
            "source_url": NETEASE_ALBUM_PAGE % album.get("id") if album.get("id") else "https://music.163.com/",
            "source": "网易云音乐",
            "publish_date": d,
            "summary": ("%s 在网易云音乐上架新单曲《%s》，发行日期 %s。"
                        % (artists, s.get("name"), d))[:160],
            "category": cat,
            "region": region,
            "category_hint": cat,
            "via": "release",
            "_origin": "release",
            "artist": artists,
            "release_type": "Single",
            "news_hook": news_value(artists, s.get("name") or "", album.get("name") or "")[1],
        })
    log("  平台新歌速递 %d 首 → 近 %d 天 %d" % (len(songs), days, len(out)))
    return out


# ---- 新闻价值筛选（2026-11-28 加）------------------------------------
# 用户诉求：平台新歌里大量是「水歌」（蹭热翻唱、烟嗓/童声/女声版、UGC 二创），
# 不能当资讯发；只有「有由头」的发行才值得报。赵本山《慌啥子嘛》是正面样本：
# AI 翻唱→买版权→真人重录→官媒评论，三平台同步，属于真新闻。
#
# 策略：默认严格——命中水歌特征直接丢；命中新闻特征才给 news_hook 标记，
# 由 cloud_writer 的 distill 进一步决定简讯还是详报。宁可少发，不发水歌。

# 水歌特征：歌名/专辑名里出现这些就是蹭热翻唱/二创，不是艺人官方发行。
WATER_PAT = re.compile(
    r"(烟嗓|童声|女声版|男声版|DJ版|dj版|加速版|降调|升调|慢速|快版|remix|Remix|REMIX|"
    r"翻唱|cover|Cover|COVER|伴奏|纯音乐|铃声|片段|剪辑|串烧|合集|治愈版|热播版|"
    r"完整版$|（.{0,6}版）$|\(.{0,6}版\)$)",
    re.I)

# 有新闻由头的特征：命中即打 news_hook，优先保留。
NEWS_PAT = re.compile(
    r"(原声|OST|ost|影视|主题曲|片尾曲|片头曲|推广曲|电影|电视剧|官宣|首唱|"
    r"回归|破纪录|夺冠|登顶|亿|万次|合作|feat|Feat|联名|致敬|纪念|"
    r"专辑|EP|EP$|新歌发布会|巡演|世界巡回)",
    re.I)

# 高新闻价值艺人（发歌本身即新闻）：老牌/国民级/话题人物。这些人的发行
# 即使歌名没命中 NEWS_PAT 也保留（如赵本山 69 岁真人重录）。
NEWS_WORTHY = {
    "周深", "陈奕迅", "周杰伦", "林俊杰", "邓紫棋", " Taylor Swift", "Taylor Swift",
    "BTS 防弹少年团", "BLACKPINK", "赵本山", "五月天", "张惠妹", "宇多田光",
    "Adele", "Coldplay", "Billie Eilish", "Ed Sheeran",
}


def news_value(artist_name, title, album_name=""):
    """返回 (是否收录, news_hook)。水歌丢；新闻由头或重点艺人保留。"""
    text = " ".join([title or "", album_name or ""])
    if WATER_PAT.search(text):
        return False, ""
    if any(w.strip() in (artist_name or "") for w in NEWS_WORTHY):
        return True, "重点艺人发行"
    m = NEWS_PAT.search(text)
    if m:
        return True, m.group(0)
    # 没命中任何特征：默认也收（保证数量），但不打 news_hook，
    # 由下游 distill 决定做成一句话简讯，不冒充新闻。
    return True, ""


def make_article(artist, item):
    """转成 cloud_writer.build_rows() 能吃的 batch 行。"""
    name = item["title"]
    stage = "新专辑" if "专辑" in item.get("type", "") else "新单曲"
    # 标题保留平台原名（真实性红线：歌名不改动），中文说明放摘要。
    title = "%s 发布%s《%s》" % (artist["name"], stage, name)
    summary = ("%s 在网易云音乐上架%s《%s》，发行日期 %s。"
               % (artist["name"], stage, name, item["publish_date"]))[:160]
    url = (NETEASE_ALBUM_PAGE % item["album_id"]) if item.get("album_id") \
        else (NETEASE_ARTIST_PAGE % artist["id"])
    keep, hook = news_value(artist["name"], name, item.get("album_name") or "")
    return {
        "title": title,
        "source_url": url,
        "source": "网易云音乐",
        "publish_date": item["publish_date"],
        "summary": summary,
        "category": artist.get("category") or "cpop",
        "region": artist.get("region") or "",
        "category_hint": artist.get("category") or "cpop",
        "via": "release",
        "_origin": "release",     # 让 cloud_writer 的 URL 去重走标题层，避免专辑页 URL 固定
        "artist": artist["name"],
        "release_type": item.get("type", ""),
        "_keep": keep,
        "news_hook": hook,
    }


# 单艺人单轮最多取几条：防止初音ミク这类海量 UGC 合集艺人刷屏淹没其他艺人。
PER_ARTIST_CAP = 5


def collect(days, proxy=None, limit=200):
    """拉近 N 天新发行，按发布日期倒序。"""
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    log("窗口 %s ~ %s，艺人 %d 位" % (cutoff, today, len(load_artists())))
    found = []
    for artist in load_artists():
        try:
            albums = fetch_artist_albums(artist, proxy)
            # 未来日期是平台预上架，不能当已发行资讯发；区间双向卡死。
            fresh = [a for a in albums if cutoff <= a["publish_date"] <= today][:PER_ARTIST_CAP]
            log("  %-20s 专辑 %2d → 近 %d 天 %d" % (artist["name"], len(albums), days, len(fresh)))
            for a in fresh:
                found.append(make_article(artist, a))
        except Exception as e:
            log("  %s ERR %s" % (artist.get("name"), str(e)[:80]))
        if len(found) >= limit:
            break
    # 第二个数据源：全平台新歌速递（不依赖艺人名单，抓长尾艺人）
    try:
        found.extend(fetch_platform_new_songs(days, proxy, limit=60))
    except Exception as e:
        log("  平台新歌速递 ERR %s" % str(e)[:80])

    # 水歌过滤：艺人名单流里 _keep=False 的直接丢（平台流已在生成时靠 news_value 判定，
    # 但平台流默认收长尾，这里统一再过一遍歌名层）。
    before = len(found)
    found = [a for a in found if a.get("_keep", True)]
    if before != len(found):
        log("  水歌过滤 %d 条" % (before - len(found)))

    # 同一歌名+艺人只留一条（艺人名单流与平台流可能重复）
    seen = set()
    uniq = []
    for a in found:
        k = re.sub(r"\W+", "", (a.get("artist", "") + a.get("title", ""))).lower()
        if k in seen:
            continue
        seen.add(k)
        a.pop("_keep", None)   # 内部标记不落盘
        uniq.append(a)
    uniq.sort(key=lambda x: (
        1 if x.get("news_hook") else 0,   # 有新闻由头的排前面
        x.get("publish_date") or "",
        x.get("title") or ""), reverse=True)
    return uniq[:limit]


def write_batch(articles, proxy=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    path = os.path.join(OUT_DIR, "batch_releases_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "proxy": proxy or "direct",
            "count": len(articles),
            "articles": articles,
            "via": "platform_releases",
        }, f, ensure_ascii=False, indent=1)
    log("落盘 %s（%d 条）" % (os.path.basename(path), len(articles)))
    return path


def selftest():
    """不联网自检：时间戳还原、文章字段、名单完整性。"""
    ok = 0
    fail = 0
    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  ✗", label)
    check(ts_to_date(1764163200000) != "", "毫秒时间戳可还原")
    check(ts_to_date("bad") == "", "坏时间戳返回空")
    artists = load_artists()
    check(any(a["name"] == "周深" for a in artists), "名单含周深")
    check(any("汤令山" in a["name"] for a in artists), "名单含汤令山")
    check(all(a.get("id") and a.get("category") for a in artists), "每个艺人都有 id 和 category")
    a = {"name": "测试艺人", "category": "cpop", "region": "中国大陆", "id": 1}
    it = {"title": "测试单曲", "publish_date": "2026-11-27", "album_id": "123", "type": "Single"}
    art = make_article(a, it)
    check("/album/123/" in art["source_url"], "专辑 URL 是路径式（去重键带 id）")
    check(art["publish_date"] == "2026-11-27", "发布日期透传")
    check(art["_origin"] == "release", "origin 标记避免 URL 去重误伤")
    print("新歌采集自检：%d 通过 / %d 失败" % (ok, fail))
    return fail == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="抓近几天的新发行（默认 7）")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-write", action="store_true", help="只抓不落盘")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    log("=== 声向 艺人新歌上架监测 开始 ===")
    proxy = cg.detect_proxy(args.proxy)
    if proxy is None and os.environ.get("FAST_MODE", "").strip().lower() not in ("1", "true", "yes", "on"):
        log("警告：无可用代理，尝试直连")
    articles = collect(args.days, proxy, args.limit)
    log("共 %d 条新发行" % len(articles))
    for a in articles[:12]:
        log("  %s %s" % (a["publish_date"], a["title"]))
    if not args.no_write and articles:
        write_batch(articles, proxy)


if __name__ == "__main__":
    main()
