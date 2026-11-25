#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 云端写库器
================================
把采集器/榜单脚本的产出写入 WorkBuddy 云数据库，供 GitHub Actions 等外部节点调用，
不依赖 WorkBuddy 桌面端与 MCP。

鉴权与权限（2026-11-19 实测）
--------------------------
- 端点：https://mp-api.app.workbuddy.host/.cloud/database/rest
- 头：x-wb-webapp-access-key: wbpk_...（小程序公钥，只读+受限插入）
- anon 只有 SELECT + 受限 INSERT（策略 articles_insert_collector）：
  只能插入 featured=false、title 4–200 字、source_url 以 http 开头、
  publish_date 不晚于今天、sort_order<100000、category 在白名单内的行。
  不能 UPDATE / DELETE / 置顶。所以本脚本只做插入，且必须先自行去重。

去重
----
articles 表没有 source_url 唯一索引，因此先拉云端现有 source_url + title，
归一化后比对，已存在的跳过。写入用逐条 POST（批量数组若被网关拒绝会自动降级）。

用法
----
  python cloud_writer.py                      # 自动找最新的 batch/events 文件
  python cloud_writer.py --batch out/batch_x.json --events out/charts/events_y.json
  python cloud_writer.py --dry-run            # 只统计不写入
"""

import argparse
import glob
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
CHART_DIR = os.path.join(OUT_DIR, "charts")

REST = os.environ.get("WB_REST_URL", "https://mp-api.app.workbuddy.host/.cloud/database/rest").rstrip("/")
# 不硬编码：本机用 WB_KEY 环境变量或 .wb_key 文件；公开仓库由 GitHub Secrets 注入。
def _load_key():
    k = os.environ.get("WB_KEY", "").strip()
    if k:
        return k
    for p in (os.path.join(HERE, ".wb_key"),
              os.path.join(os.path.dirname(HERE), ".workbuddy", ".wb_key")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            continue
    return ""


KEY = _load_key()

VALID_CATEGORIES = {
    "game", "kpop", "jpop", "cpop", "vocal", "west", "globalpop", "folk",
    "heritage", "guofeng", "electro", "hiphop", "funk", "blues", "rock",
    "classical", "musical", "ost", "industry", "report", "history",
    "gossip", "greaterchina",
}

# 非音乐内容硬过滤（与自动化任务的内容审核红线一致，脚本层再兜一道）
# 2026-11-19 补：NME/Soompi 这类"音乐垂媒"现在也发小游戏攻略、剧集预告，
# 之前靠域名白名单直接放行，导致 Wordle 答案、韩剧预告混进音乐库。
NON_MUSIC_PAT = re.compile(
    r"\b(GTA\s?6|game ?play|gaming|playstation|xbox|nintendo|esports?|"
    r"wordle|quordle|connections answers|strands hints|nyt games|crossword|"
    r"hints? (and )?(answers? )?for today|"
    r"玩家|游戏评分|发售日|抢玩|改区|销量榜)\b", re.I)

# 舞台/文学类干扰：BroadwayWorld、Slipped Disc 这类站既发音乐剧也发话剧、
# 书评与文学节，光靠域名放行会混进「莎士比亚戏剧节」「诗人谈书」。
# 这里点名拦截，标题命中就直接不收（正文里出现不算，避免误杀音乐剧稿件）。
STAGE_NON_MUSIC_PAT = re.compile(
    r"莎士比亚|shakespeare|话剧|話劇|舞台剧《|戏剧节|戲劇節|戏剧评论|"
    r"诗人|詩人|诗集|詩集|书评|書評|文学节|文學節|"
    r"\bplayhouse\b|book festival|poet\b",
    re.I)

# 与上面配套的豁免项：同一标题里出现了这些，说明确实是音乐类演出报道。
STAGE_EXEMPT_PAT = re.compile(
    r"音乐剧|音樂劇|musical\b|歌剧|歌劇|opera\b|concert|演唱会|演唱會|"
    r"交响|交響|symphony|ballet|芭蕾|音乐节|音樂節|festival lineup",
    re.I)

# 攻略/游戏/体育栏目的路径，即便来源是音乐媒体也不收。
NON_MUSIC_URL_PAT = re.compile(r"/(gaming-guides?|game-guides?|wordle|connections|crossword)/", re.I)

# 严格模式：云端没有 LLM 审核，靠"必须出现音乐强关键词 或 来自音乐垂媒"来守质量。
# 只在标题+摘要都看不出音乐相关性、且来源不是音乐媒体时拒绝，避免误杀演出/产业稿。
MUSIC_PAT = re.compile(
    r"music|musical|album|song|single|track|tour|concert|festival|band\b|singer|"
    r"rapper|producer|composer|orchestra|opera|soundtrack|remix|debut|chart|"
    r"billboard|grammy|cma\b|eurovision|coachella|glastonbury|vma\b|"
    r"spotify|vinyl|remaster|duet|mixtape|ep\b|lp\b|"
    r"音乐|专辑|单曲|歌曲|演唱会|音乐节|交响|歌剧|音乐剧|原声|配乐|歌手|"
    r"乐队|作曲|编曲|榜单|巡演|现场|唱片|金曲|歌迷|乐评|声乐|民乐|戏曲",
    re.I)

# 纯音乐垂直媒体：来自这些域名的一律放行（它们不会发纯游戏/体育/影视新闻）。
# 注意不要把 NME、Soompi、Variety、Rolling Stone 放进来——它们同时发游戏攻略与影视稿。
MUSIC_DOMAINS = {
    "billboard.com", "musicbusinessworldwide.com", "digitalmusicnews.com",
    "completemusicupdate.com", "musically.com", "pitchfork.com", "stereogum.com",
    "spin.com", "brooklynvegan.com", "djmag.com",
    "pollstar.com", "operanews.com", "hypebot.com",
}

# 一手来源：机构/公司/平台官网。这些域名上的公告就是原始出处，可直接 verified。
OFFICIAL_DOMAINS = {
    "ifpi.org", "riaa.com", "cisac.org", "soundexchange.com", "ascap.com",
    "bmi.com", "prsformusic.com", "ppluk.com", "sacem.fr", "gema.de",
    "jasrac.or.jp", "komca.or.kr", "mcsc.com.cn", "capa.com.cn",
    "chnmusic.org.cn", "cnosonline.com", "cflac.org.cn", "ihchina.cn",
    "spotify.com", "apple.com", "ticketmaster.com", "livenation.com",
    "damai.cn", "cityline.com", "kktix.com", "cotaiticketing.com",
    "umusic.com", "sonymusic.com", "wmg.com", "universal-music.com",
    "gov.cn", "copyright.gov", "europa.eu",
}

# 弱来源：自媒体、聚合、UGC。不能作为唯一依据，进库也标 suspect 并显示"待核实"。
WEAK_DOMAINS = {
    "mp.weixin.qq.com", "baijiahao.baidu.com", "toutiao.com", "zhihu.com",
    "weibo.com", "douban.com", "kuaishou.com", "douyin.com", "bilibili.com",
    "sohu.com", "163.com", "qq.com", "sina.com.cn", "ifeng.com",
}

# 待核实措辞：非一手来源出现这些词，又没有一手出处，多半是传言，直接不收
RUMOR_PAT = re.compile(
    r"网传|疯传|据传|传闻|爆料|知情人|内部消息|疑似|被曝|"
    r"rumou?r|reportedly|allegedly", re.I)

# 首页 / 栏目页 / 分页 / 搜索页：读者点进去看不到这条新闻，等于无法核对
LISTING_PAT = re.compile(
    r"/(category|categories|tag|tags|topic|topics|channel|section|archive|"
    r"search|author|column|topics?)(/|$)", re.I)


def link_grade(url):
    """判断链接精度：article 直达单篇 / listing 首页或栏目页 / none 无有效链接。

    首页、栏目页、分页、搜索页打开后看不到这条新闻本身，读者没法核对，
    因此不算合格原文链接：内容照样保留，但降级为 suspect 并写明原因。
    """
    m = re.match(r"^https?://([^/?#]+)([^?#]*)", url or "", re.I)
    if not m:
        return "none"
    path = (m.group(2) or "").rstrip("/")
    if not path:
        return "listing"
    if LISTING_PAT.search(path) or re.search(r"/page/\d+$", path, re.I):
        return "listing"
    return "article"


def link_note(url):
    """给前端看的一句话说明，写清链接为什么不够准。"""
    grade = link_grade(url)
    if grade == "listing":
        return "来源链接只到首页或栏目页，未直达单篇，需检索标题核对原文"
    if grade == "none":
        return "来源链接缺失或格式无效"
    return ""


def _in(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def verify_fields(url, title):
    """按来源等级给出可信度三字段。脚本不联网，只做等级判定，不冒充已核实。

    - 一手官网 / 音乐垂媒原文 → verified, count=1（该媒体的原创报道即一手）
    - 弱来源（聚合、自媒体、UGC）→ suspect，前端显示"待核实"
    - 其他未知域名 → unverified，留给每小时自检联网核实
    """
    host = _domain(url)
    if _in(host, OFFICIAL_DOMAINS):
        return {"verify_status": "verified", "cross_source_count": 1,
                "verify_note": "机构/平台官网发布（%s），脚本判定一手" % host}
    if _in(host, MUSIC_DOMAINS):
        return {"verify_status": "verified", "cross_source_count": 1,
                "verify_note": "音乐媒体原创报道（%s）" % host}
    if _in(host, WEAK_DOMAINS):
        return {"verify_status": "suspect", "cross_source_count": 0,
                "verify_note": "聚合/自媒体来源（%s），未回溯一手" % host}
    return {"verify_status": "unverified", "cross_source_count": 0, "verify_note": ""}


def log(msg):
    print("[写库] " + msg, flush=True)


def _req(method, path, body=None, extra_headers=None, retries=0):
    url = REST + path
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hd = {
        "x-wb-webapp-access-key": KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }
    if extra_headers:
        hd.update(extra_headers)
    # GET 幂等，可安全重试：网关 5xx / 429 / 超时 / 网络抖动都先退避再试，
    # 避免一次瞬时故障就把整轮写库打挂（2026-11-21 补，之前连挂多轮的主因）。
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers=hd, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "ignore")
                return r.status, (json.loads(raw) if raw.strip() else [])
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            if attempt <= retries and (e.code >= 500 or e.code == 429):
                time.sleep(3 * attempt)
                continue
            return e.code, raw[:400]
        except Exception as e:
            if attempt <= retries:
                time.sleep(3 * attempt)
                continue
            return 0, str(e)[:200]


def norm_title(t):
    return re.sub(r"[\s\W_]+", "", (t or "")).lower()


def norm_url(u):
    """归一化 source_url 用于去重。

    普通链接剥掉查询串与尾斜杠即可；但公众号文章的唯一标识全在查询串里
    （__biz/mid/idx/sn），剥掉后所有 mp.weixin.qq.com/s?... 会塌成同一个键，
    互相误判重复。因此对公众号链接保留这四个参数、去掉 chksm/#rd 等噪声，
    并统一 http→https（同一篇文章两种 scheme 也应视为同一篇）。
    """
    u = (u or "").split("#")[0].strip()
    m = re.match(r"^(https?://mp\.weixin\.qq\.com/s)\?(.+)$", u, re.I)
    if m:
        keep = [kv for kv in m.group(2).split("&")
                if kv.split("=", 1)[0].lower() in ("__biz", "mid", "idx", "sn")]
        return "https://mp.weixin.qq.com/s" + ("?" + "&".join(keep) if keep else "")
    return re.sub(r"^http://", "https://", u.split("?")[0], flags=re.I).rstrip("/").lower()


def fetch_existing():
    """拉云端现有 source_url 与 title（最多 8000 条，足够覆盖去重窗口）。

    分页拉取（每页 1000 条）+ 每页带重试：表越长单次大请求越容易超时，
    一次失败就整轮中止是之前间歇性失败的主因。任一页重试后仍失败才中止，
    保证去重数据完整——宁可这轮不写，也不带着残缺的去重表硬写出重复数据。
    """
    rows_all = []
    page = 1000
    offset = 0
    while len(rows_all) < 8000:
        status, data = _req(
            "GET",
            "/articles?select=source_url,title&order=id.desc&limit=%d&offset=%d"
            % (page, offset),
            retries=3)
        if status != 200 or not isinstance(data, list):
            log("警告：读取现有数据失败（HTTP %s），去重将不可靠，已中止" % status)
            return None
        rows_all.extend(data)
        if len(data) < page:
            break
        offset += page
    urls = {norm_url(r.get("source_url"))
            for r in rows_all if r.get("source_url")}
    titles = {norm_title(r.get("title")) for r in rows_all if r.get("title")}
    log("云端现有 %d 条，source_url %d 个" % (len(rows_all), len(urls)))
    return urls, titles


# ── 本地去重索引（2026-11-21 补，省云数据库读取额度）──
# 原来每轮写库前都全表回读去重，快采 5 分钟一轮，一天就是十几万行读取，
# 额度主要烧在这里。改法：已入库的 url/标题清单存进仓库文件
# collector/out/state/dedupe_index.json（全量工作流的心跳提交会带上它），
# 每轮只回读云端最近 N 条补上索引之后的新增；索引过期（默认 20 小时）
# 才全量回读一次重建。读不到近端数据时仍安全中止，绝不带着残缺清单硬写。
INDEX_PATH = os.path.join(OUT_DIR, "state", "dedupe_index.json")


def load_index():
    """读本地去重索引，返回 (urls, titles, built_at)；无文件/损坏返回 None。"""
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        urls = {u for u in d.get("urls") or [] if u}
        titles = {t for t in d.get("titles") or [] if t}
        built_at = float(d.get("built_at") or 0)
        if not urls and not titles:
            return None
        return urls, titles, built_at
    except Exception:
        return None


def save_index(urls, titles):
    try:
        os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump({"built_at": time.time(),
                       "urls": sorted(urls), "titles": sorted(titles)},
                      f, ensure_ascii=False)
        log("去重索引已更新：%d 个链接 / %d 个标题" % (len(urls), len(titles)))
    except Exception as e:
        log("去重索引保存失败（不影响本轮写库）：%s" % str(e)[:80])


def fetch_recent(limit):
    """只拉云端最近 limit 条（按 id 倒序），覆盖索引建成之后的新增。"""
    status, data = _req(
        "GET",
        "/articles?select=source_url,title&order=id.desc&limit=%d" % limit,
        retries=3)
    if status != 200 or not isinstance(data, list):
        log("警告：读取近端数据失败（HTTP %s），去重将不可靠，已中止" % status)
        return None
    urls = {norm_url(r.get("source_url"))
            for r in data if r.get("source_url")}
    titles = {norm_title(r.get("title")) for r in data if r.get("title")}
    return urls, titles


def load_existing(recent_limit=300, max_age_hours=20.0, use_index=True,
                  persist_index=True):
    """组装去重清单：本地索引 + 云端近端回读；无索引或索引过期才全量回读。"""
    idx = load_index() if use_index else None
    if idx is not None:
        urls, titles, built_at = idx
        age_h = (time.time() - built_at) / 3600.0
        if age_h <= max_age_hours:
            recent = fetch_recent(recent_limit)
            if recent is None:
                return None
            ru, rt = recent
            urls |= ru
            titles |= rt
            log("去重：本地索引（%.1f 小时前）+ 云端近端 %d 条" % (age_h, len(ru)))
            return urls, titles
        log("去重索引已 %.1f 小时，全量回读重建" % age_h)
    full = fetch_existing()
    if full is None:
        return None
    if use_index and persist_index:
        save_index(full[0], full[1])
    return full


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def latest(pattern):
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def today_str():
    """业务日期按北京时间算。

    GitHub Actions 跑在 UTC，直接用本机时间会让北京时间 0:00–8:00 这段
    把「今天」算成昨天，凌晨的稿子日期就被钳错一天。
    """
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def clamp_date(d):
    """publish_date 不得晚于今天（云端策略会拒未来日期）。"""
    d = (d or "").strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return today_str()
    return d if d <= today_str() else today_str()


def build_rows(batch_path, events_path):
    """把批采与榜单事件统一成 articles 行。"""
    rows = []
    if batch_path:
        j = load_json(batch_path) or {}
        for a in (j.get("articles") or []):
            body = (a.get("body") or "").strip()
            rows.append({
                "category": a.get("category"),
                "title": (a.get("title") or "").strip(),
                "summary": (a.get("summary") or "").strip()[:400],
                # 采集阶段已翻好的中文（见 translate_zh.py）；没有就留空，下面统一补。
                "title_cn": (a.get("title_cn") or "").strip()[:200],
                "summary_cn": (a.get("summary_cn") or "").strip()[:400],
                # 公众号等渠道有全文正文的保留；没有就空串（简讯）。
                "body": body[:4000],
                "source": (a.get("source") or "").strip()[:120],
                "source_url": (a.get("source_url") or "").strip(),
                "tag": "",
                "publish_date": clamp_date(a.get("publish_date")),
                "read_minutes": int(a.get("read_minutes") or (min(9, max(1, len(body) // 350)) if body else 1)),
                "featured": False,
                "region": a.get("region") or "全球",
                "is_brief": bool(a.get("is_brief")) if a.get("is_brief") is not None else not body,
                # gap = 用户问了但站内没有，采集器定向补抓回来的稿件
                "_origin": a.get("_origin") or "batch",
            })
    if events_path:
        j = load_json(events_path) or {}
        for e in (j.get("events") or []):
            rows.append({
                "category": e.get("category") or "gossip",
                "title": (e.get("title") or "").strip(),
                "summary": (e.get("summary") or "").strip()[:400],
                "title_cn": (e.get("title_cn") or "").strip()[:200],
                "summary_cn": (e.get("summary_cn") or "").strip()[:400],
                "body": "",
                "source": (e.get("source") or "").strip()[:120],
                "source_url": (e.get("source_url") or "").strip(),
                "tag": "流媒体榜",
                "publish_date": today_str(),
                "read_minutes": 1,
                "featured": False,
                "region": e.get("region") or "全球",
                "is_brief": True,
                "_origin": "chart",
            })
    return rows


def _domain(url):
    m = re.match(r"https?://([^/]+)", url or "", re.I)
    d = (m.group(1) if m else "").lower()
    return d[4:] if d.startswith("www.") else d


def _is_music_domain(url):
    d = _domain(url)
    return any(d == x or d.endswith("." + x) for x in MUSIC_DOMAINS)


def filter_rows(rows, existing, limit=0):
    urls, titles = existing
    kept, stats = [], {"empty": 0, "bad_url": 0, "dup_url": 0, "dup_title": 0,
                       "non_music": 0, "weak_music": 0, "bad_cat": 0,
                       "rumor": 0, "suspect": 0, "listing_url": 0, "thin_title": 0,
                       "ops_reject": 0, "ops_rewrite": 0, "scrubbed": 0, "briefed": 0}
    for r in rows:
        if not r["title"] or len(r["title"]) < 4:
            stats["empty"] += 1
            continue
        # 标题质量（2026-11-19 补）：RSS 首条常是栏目名本身，例「Latest Hits!」「Music」，
        # 长度够但没有信息量，进库后像一张空白卡片。8 字符以下或纯栏目词一律丢。
        t_raw = r["title"].strip()
        if len(t_raw) < 8 or re.fullmatch(
                r"(latest\s+)?(hits?|news|music|entertainment|videos?|photos?|"
                r"reviews?|features?|trending|hot\s+news|top\s+stories)\W*",
                t_raw, re.I):
            stats["thin_title"] += 1
            continue
        r["title"] = r["title"][:200]
        if not r["source_url"].startswith("http"):
            stats["bad_url"] += 1
            continue
        # 运营稿提炼（2026-11-19 用户投诉：观演攻略/票务提示不该原文入库）：
        # 有演出事实的压成一句话简讯，纯通知直接拒录。必须在去重前做，
        # 否则同题材稿会以原长标题占住标题位，压成的一句话反而被判重复。
        d = distill.analyze(r["title"], r.get("body") or "", r.get("source") or "")
        if d["action"] == "reject":
            stats["ops_reject"] += 1
            log("  [拒录] %s | %s" % (d["reason"], r["title"][:50]))
            continue
        if d["action"] == "rewrite":
            stats["ops_rewrite"] += 1
            r["title"] = d["title"]
            r["summary"] = d["summary"]
            r["body"] = ""
            r["is_brief"] = True
            r["read_minutes"] = 1
            log("  [提炼] %s" % d["title"])
        # 去噪声（2026-11-19 用户二次投诉：关注xxx/油价xxx/编辑署名/此前报道跨推
        # 这类没用信息不该出现在正文里）。保留新闻事实，清掉引流与运营尾巴。
        b0, s0 = r.get("body") or "", r.get("summary") or ""
        b1, s1 = distill.scrub(b0), distill.scrub(s0)
        if b1 != b0 or s1 != s0:
            stats["scrubbed"] += 1
            r["body"], r["summary"] = b1, s1
            log("  [去噪] %s" % r["title"][:40])
        # 简讯优先（2026-11-19 用户要求：重点一句话讲透，只有热点才多内容）：
        # 非热点长文压缩成一句话简讯；热点/报告/音乐史保留正文与数字。
        if not r.get("is_brief"):
            chg, ns, nb, nib, nrm = distill.brief_check(
                r["title"], r.get("summary") or "", r.get("body") or "",
                r.get("category") or "", bool(r.get("featured")),
                r.get("external_heat") or 0)
            if chg:
                stats["briefed"] += 1
                r["summary"], r["body"] = ns, nb
                r["is_brief"], r["read_minutes"] = nib, nrm
                log("  [简讯] %s" % r["title"][:40])
        u = norm_url(r["source_url"])
        if u in urls:
            stats["dup_url"] += 1
            continue
        nt = norm_title(r["title"])
        if nt in titles:
            stats["dup_title"] += 1
            continue
        # 真假核实（脚本层只能判来源等级）：一手/垂媒原文记 verified，弱来源记 suspect。
        # 传言类标题若没有一手出处，直接不收 —— 用户明确要求把假新闻挡在库外。
        vf = verify_fields(r["source_url"], r["title"])
        # 链接不直达单篇：内容保留入库，但降级为存疑并写明原因，读者点原文能看出问题。
        if link_grade(r["source_url"]) != "article":
            stats["listing_url"] += 1
            vf = {"verify_status": "suspect", "cross_source_count": 0,
                  "verify_note": link_note(r["source_url"])}
        if RUMOR_PAT.search(r["title"]) and vf["verify_status"] != "verified":
            stats["rumor"] += 1
            continue
        # 榜单事件天然是音乐，不做相关性判断
        if r.get("_origin") != "chart":
            if NON_MUSIC_PAT.search(r["title"] + " " + (r.get("summary") or "")):
                stats["non_music"] += 1
                continue
            if NON_MUSIC_URL_PAT.search(r["source_url"]):
                stats["non_music"] += 1
                continue
            # 舞台/文学类：标题点名「莎士比亚/话剧/诗人/书评」就挡，除非同句里有
            # 明确的音乐形式（"音乐剧《莎士比亚》"/"musical at Playhouse"），
            # 那种是正经音乐剧报道，不能误杀。
            if STAGE_NON_MUSIC_PAT.search(r["title"]) and not STAGE_EXEMPT_PAT.search(r["title"]):
                stats["non_music"] += 1
                continue
            # 严格模式：既不含音乐关键词、又非音乐垂媒 → 判为不相关
            # gap 例外：用户点名问的人/事，标题里往往没有 music/album
            # （如「BIGBANG 近况」），再卡关键词就永远补不上。
            hay = r["title"] + " " + (r.get("summary") or "")
            if (r.get("_origin") != "gap"
                    and not MUSIC_PAT.search(hay)
                    and not _is_music_domain(r["source_url"])):
                stats["weak_music"] += 1
                continue
        if r["category"] not in VALID_CATEGORIES:
            r["category"] = "gossip"
            stats["bad_cat"] += 1
        if vf["verify_status"] == "suspect":
            stats["suspect"] += 1
        r.update(vf)
        urls.add(u)
        titles.add(nt)
        kept.append(r)
        if limit and len(kept) >= limit:
            break
    return kept, stats


def write_rows(rows, dry_run=False):
    if not rows:
        return 0, 0
    status, data = _req("GET", "/articles?select=sort_order&order=sort_order.desc&limit=1", retries=2)
    base = 0
    if status == 200 and isinstance(data, list) and data:
        base = int(data[0].get("sort_order") or 0)
    log("当前最大 sort_order=%d，本轮待写 %d 条" % (base, len(rows)))

    if dry_run:
        for r in rows[:50]:
            log("  [dry] %s | %s | %s" % (r["category"], r["title"][:60], r["source_url"][:60]))
        return len(rows), 0

    ok = fail = 0
    for i, r in enumerate(rows, 1):
        payload = {k: v for k, v in r.items() if not k.startswith("_")}
        payload["sort_order"] = base + i
        payload["tag"] = payload.get("tag") or ""
        # 写库前最后一道门：严格对齐 RLS 策略 articles_insert_collector 的 7 条约束。
        # 大脑 enrich_rows 在 filter_rows 之后改 category，若输出 RLS 不认的类目
        # （旧版 "indie"），这里兜底纠正，避免整条撞 42501 写不进去（2026-11-21）。
        payload["featured"] = False
        payload["title"] = (payload.get("title") or "").strip()[:200]
        payload["source_url"] = (payload.get("source_url") or "").strip()
        payload["publish_date"] = clamp_date(payload.get("publish_date"))
        if payload.get("category") not in VALID_CATEGORIES:
            log("  [RLS兜底] 分类 %r 非法 → gossip | %s" %
                (payload.get("category"), payload["title"][:40]))
            payload["category"] = "gossip"
        status, resp = _req("POST", "/articles", payload)
        if status in (200, 201):
            ok += 1
        else:
            fail += 1
            log("  写入失败(id=%d) HTTP %s %s | %s" % (i, status, str(resp)[:140], r["title"][:40]))
        time.sleep(0.15)  # 轻微节流，避免网关限速
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None)
    ap.add_argument("--events", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多写多少条（0=不限）")
    ap.add_argument("--region", default=None,
                    help="只写指定地区，逗号分隔（如 新加坡,马来西亚,泰国）。留空=不限")
    ap.add_argument("--proxy", default=os.environ.get("https_proxy") or os.environ.get("http_proxy"),
                    help="翻译走代理，如 7897；GitHub Actions 直连可留空")
    ap.add_argument("--no-translate", action="store_true", help="跳过翻译，直接写原文")
    ap.add_argument("--no-llm", action="store_true",
                    help="关闭云托管大模型增强（降级回机器翻译+关键词分类）")
    ap.add_argument("--recent-limit", type=int, default=300,
                    help="用本地索引去重时，回读云端最近多少条补新增（默认 300）")
    ap.add_argument("--index-max-age", type=float, default=20.0,
                    help="本地去重索引超过多少小时就全量回读重建（默认 20）")
    ap.add_argument("--no-index", action="store_true",
                    help="不用本地去重索引，每轮全量回读（旧行为）")
    ap.add_argument("--tcb", action="store_true",
                    help="写微信云开发（2026-11-25 起主力出口；需要 TCB_SECRET）")
    args = ap.parse_args()

    if args.tcb:
        try:
            import tcb_sink
            if not tcb_sink._secret():
                log("错误：--tcb 需要 AppSecret。设置 TCB_SECRET 或写 collector/.tcb_secret")
                return 2
        except Exception as e:
            log("错误：云开发写入模块不可用（%s）" % str(e)[:120])
            return 2
    elif not KEY:
        log("错误：缺少写入密钥。请设置环境变量 WB_KEY，或写入 采集器/.wb_key")
        return 2

    batch = args.batch or latest(os.path.join(OUT_DIR, "batch_*.json"))
    events = args.events or latest(os.path.join(CHART_DIR, "events_*.json"))
    log("批采：%s" % (os.path.basename(batch) if batch else "无"))
    log("榜单：%s" % (os.path.basename(events) if events else "无"))

    rows = build_rows(batch, events)
    log("候选 %d 条（批采+榜单）" % len(rows))
    if not rows:
        log("无候选，结束")
        return 0

    if args.tcb:
        import tcb_sink
        try:
            existing = tcb_sink.existing(2000)
        except Exception as e:
            log("读云端去重集失败（%s），中止" % str(e)[:120])
            return 2
    else:
        existing = load_existing(recent_limit=args.recent_limit,
                                 max_age_hours=args.index_max_age,
                                 use_index=not args.no_index,
                                 persist_index=not args.dry_run)
    if existing is None:
        return 2
    kept, stats = filter_rows(rows, existing, args.limit)
    log("过滤：空标题 %(empty)d / 标题无信息量 %(thin_title)d / 坏链接 %(bad_url)d / 重复链接 %(dup_url)d / "
        "重复标题 %(dup_title)d / 非音乐 %(non_music)d / 音乐相关性弱 %(weak_music)d / "
        "纠正分类 %(bad_cat)d / 传言剔除 %(rumor)d / 弱来源标存疑 %(suspect)d / "
        "链接不达单篇 %(listing_url)d / 运营稿提炼 %(ops_rewrite)d / 运营稿拒录 %(ops_reject)d / "
        "噪声清理 %(scrubbed)d / 简讯压缩 %(briefed)d" % stats)
    log("待写入 %d 条" % len(kept))

    if args.region:
        want = {x.strip() for x in args.region.split(",") if x.strip()}
        before = len(kept)
        kept = [r for r in kept if r.get("region") in want]
        log("地区过滤 %s：%d → %d 条" % ("/".join(sorted(want)), before, len(kept)))

    # ── 云托管大脑：大模型全量增强（中文译名/摘要/分类/音乐相关性）──
    # 用户 2026-11-20 要求"给云托管长个大脑"。实测 publishableKey 可匿名直连
    # keyless LLM 端点，便宜模型 glm-5.3-flash 可用。大模型产出中文后无需再机器翻译。
    # 任何失败自动降级回规则引擎（机器翻译+关键词分类），绝不中断写库。
    llm_used = False
    if not args.no_llm and kept and not args.dry_run:
        try:
            from llm_brain import enrich_rows
            st = enrich_rows(kept, log=log)
            llm_used = not bool(st.get("error"))
            if st.get("nonmusic"):
                before = len(kept)
                kept = [r for r in kept if not r.get("_llm_nonmusic")]
                log("  大脑剔除非音乐 %d 条（%d → %d）" % (st["nonmusic"], before, len(kept)))
            # 清掉内部标记，避免写进库字段
            for r in kept:
                r.pop("_llm_nonmusic", None)
                r.pop("_llm_done", None)
        except Exception as e:
            log("  大脑模块加载失败，降级规则引擎：%s" % str(e)[:80])

    # 外文稿件补中文：大模型已产出就跳过；否则（--no-llm/大模型失败）走机器翻译兜底。
    # 翻不出来也不拦，小程序端还有前端 LLM 兜底翻译。
    if not llm_used and not args.no_translate and kept:
        try:
            from translator import translate_row
            done = 0
            for r in kept:
                try:
                    done += translate_row(r)
                except Exception:
                    pass
            log("逐条翻译完成：补中文 %d 条（词典纠察已启用）" % done)
        except Exception as e:
            log("翻译步骤跳过（%s），入库后由前端兜底" % str(e)[:80])

    if args.tcb:
        import tcb_sink
        ok, fail = tcb_sink.write_rows(kept, args.dry_run)
    else:
        ok, fail = write_rows(kept, args.dry_run)
    log("=== 完成：成功 %d，失败 %d ===" % (ok, fail))
    if fail:
        return 1
    # 全部写成功才把本轮新增并进本地索引（filter_rows 已把 kept 的
    # url/标题加进 existing 集合）。有失败就不更新，宁可下轮多回读一点，
    # 也不能把没写进去的稿子误记成「已入库」害它永远补不上。
    if ok and not args.dry_run and not args.no_index:
        save_index(existing[0], existing[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
