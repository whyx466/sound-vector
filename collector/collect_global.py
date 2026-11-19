#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 全球音乐资讯采集器
=====================================
目标：在 v2rayN / Clash 代理环境不稳定时，仍能稳定读到全球音乐新闻与市场（report）内容。

设计要点
--------
1. 代理按优先级探测（不按延迟）：**v2rayN 10808 首选，连不通才降级 Clash Verge 7897**，
   再退 7890 / 10809 / 1080 / 环境变量 http_proxy；结果缓存 10 分钟（.proxy.cache）。
   某条线路中途 502/超时，会自动降级到下一个候选，不会让整轮采集挂掉。
2. 四类源，全部产出**可核查的原文链接**：
   - Bing News RSS：一次 12+ 条，link 里的 url= 参数能还原成出版社原文（去掉 c=/mkt= 追踪参数）。
   - GDELT DOC API：全球多语种，直出原文链接，免费无 key，但限制 1 次/5 秒，故 7 秒节流。
   - 出版社自有 RSS（Billboard / MBW / DMN / Musically / NME ...）：一手链接，最可靠。
   - YouTube Data API v3：视频类一手链接（官方频道 / 新歌 / 现场）。
   - Google News RSS：**只做热点发现**。它的 link 是 news.google.com 中转页（JS 跳转，
     batchexecute 解码接口已失效），无法还原原文，因此标题只用于算热度、交叉验证，不入库。
3. 去重：标题归一化哈希（小写、去标点、去空白、去媒体后缀）+ source_url 双重去重。
4. 输出：out/batch_<时间>.json（可入库结构）+ out/report_<时间>.md（人读汇总）。

用法
----
  python collect_global.py                 # 默认：全源，目标 60 条新稿
  python collect_global.py --limit 40      # 指定条数
  python collect_global.py --source bing   # 只跑某个源（bing/gdelt/rss/youtube）
  python collect_global.py --proxy 7897    # 强制指定代理端口
"""

import argparse
import base64
import gzip
import hashlib
import html
import io
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
CACHE = os.path.join(HERE, ".proxy.cache")

# 用户「问了但站内没有」的提问 → 本轮定向检索词
try:
    from gap_queries import build_gap_queries
except Exception:  # 单文件运行/裁剪部署时不该因此中断采集
    def build_gap_queries(limit=6, log=print):
        return [], []

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 线路优先级（严格按顺序，不按延迟）：用户指定
#   1) v2rayN（xray，10808）首选
#   2) 连不通才降级到 Clash Verge（7897）
#   3) 再退到其他常见端口
PROXY_CANDIDATES = [
    (10808, "v2rayN"),
    (7897, "Clash Verge"),
    (7890, "Clash"),
    (10809, "v2rayN 备用"),
    (7891, "Clash 备用"),
    (1080, "通用"),
    (2080, "通用"),
    (8889, "通用"),
]

# 不硬编码：本机运行时用环境变量或 .google_key 文件；公开仓库部署时由 GitHub Secrets 注入。
def _load_google_key():
    k = os.environ.get("GOOGLE_API_KEY", "").strip()
    if k:
        return k
    for p in (os.path.join(HERE, ".google_key"),
              os.path.join(os.path.dirname(HERE), ".workbuddy", ".google_key")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            continue
    return ""


GOOGLE_API_KEY = _load_google_key()

# ---------------------------------------------------------------- 代理

_ACTIVE_PROXY = None


def _try_proxy(port, timeout=6):
    """对一个端口做真实 HTTPS 健康检查，返回毫秒数或 None。"""
    proxy = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    t0 = time.time()
    try:
        r = opener.open("https://www.gstatic.com/generate_204", timeout=timeout)
        r.read(1)
        if r.status in (204, 200):
            return int((time.time() - t0) * 1000)
    except Exception:
        return None
    return None


def detect_proxy(force=None):
    """返回可用代理 URL；全部失败返回 None（走直连）。"""
    global _ACTIVE_PROXY
    # 云端（GitHub Actions 等境外节点）用 --proxy direct：跳过本机端口探测，
    # 否则会白等 8 个候选端口各 6 秒超时（约 48 秒）。
    if force and str(force).lower() in ("direct", "none", "off"):
        log("直连模式（跳过本机代理探测）")
        _ACTIVE_PROXY = ""
        return ""
    if force:
        port = int(str(force).split(":")[-1])
        ms = _try_proxy(port)
        if ms:
            log("代理 127.0.0.1:%d 可用（%dms，手动指定）" % (port, ms))
            _ACTIVE_PROXY = "http://127.0.0.1:%d" % port
            return _ACTIVE_PROXY
        log("警告：指定代理 127.0.0.1:%d 不可用" % port)
        return None

    # 缓存（10 分钟内复用，避免每轮都探测）
    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - c.get("ts", 0) < 600:
            port = c.get("port")
            if port:
                _ACTIVE_PROXY = "http://127.0.0.1:%d" % port
                log("代理 %s（缓存命中）" % _ACTIVE_PROXY)
                return _ACTIVE_PROXY
    except Exception:
        pass

    env = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    # 严格按优先级：第一条可用即用，绝不因为「另一条更快」而换线
    for idx, (p, name) in enumerate(PROXY_CANDIDATES):
        ms = _try_proxy(p)
        if ms:
            _ACTIVE_PROXY = "http://127.0.0.1:%d" % p
            log("选用代理 %s（%s，优先级 %d，%dms）" % (_ACTIVE_PROXY, name, idx + 1, ms))
            try:
                with open(CACHE, "w", encoding="utf-8") as f:
                    json.dump({"port": p, "name": name, "ts": time.time()}, f)
            except Exception:
                pass
            return _ACTIVE_PROXY
        log("  优先级 %d 的 %s（%d）不通，降级下一条" % (idx + 1, name, p))

    if env:
        log("  环境变量代理 %s，直接采用" % env)
        _ACTIVE_PROXY = env
        return env
    # 直连再试一次
    try:
        r = urllib.request.urlopen(
            "https://www.gstatic.com/generate_204", timeout=6)
        if r.status in (200, 204):
            log("无代理可用，但直连通")
            _ACTIVE_PROXY = ""
            return ""
    except Exception:
        pass
    log("错误：所有代理候选与直连均不可用")
    return None


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def fetch(url, timeout=25, retry=2, headers=None, data=None):
    """带代理降级的抓取：失败时切换下一条线路重试。"""
    global _ACTIVE_PROXY
    hd = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
          "Accept-Encoding": "gzip"}
    if headers:
        hd.update(headers)
    last = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hd)
            r = _opener(_ACTIVE_PROXY).open(req, timeout=timeout)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            enc = r.headers.get("Content-Type", "")
            m = re.search(r"charset=([\w-]+)", enc)
            cs = m.group(1) if m else "utf-8"
            try:
                return raw.decode(cs, "ignore")
            except LookupError:
                return raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            if attempt < retry:
                # 换一条线路：清缓存并重新探测
                log("    重试 %d/%d（%s）" % (attempt + 1, retry, type(e).__name__))
                try:
                    os.remove(CACHE)
                except Exception:
                    pass
                detect_proxy()
                time.sleep(1.2)
    log("    抓取失败 %s -> %s" % (url[:70], last))
    return None


# ---------------------------------------------------------------- 工具

def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return html.unescape(s).strip()


def norm_title(t):
    t = html.unescape(t or "")
    t = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", t)  # 去尾部 " - 媒体名"
    t = t.lower()
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t


def clean_url(u):
    """去掉追踪参数，只留干净原文地址。"""
    u = html.unescape(u or "").strip()
    u = re.sub(r"&amp;", "&", u)
    # Bing apiclick 中转：真正的地址在 url= 参数里
    if "bing.com/news/apiclick" in u or "apiclick.aspx" in u:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        if q.get("url"):
            u = urllib.parse.unquote(q["url"][0])
    # 去掉 c= / mkt= / oc= 等聚合器追踪参数
    parts = urllib.parse.urlsplit(u)
    if parts.query:
        keep = []
        for kv in parts.query.split("&"):
            k = kv.split("=")[0].lower()
            if k in ("c", "mkt", "oc", "ref", "aid", "tid", "cmp", "src",
                     "utm_source", "utm_medium", "utm_campaign", "fbclid", "gclid"):
                continue
            keep.append(kv)
        u = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             "&".join(keep), ""))
    return u.strip()


def domain_of(u):
    try:
        d = urllib.parse.urlsplit(u).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def parse_date(s):
    """把各种 RSS 时间格式压成 YYYY-MM-DD。"""
    s = (s or "").strip()
    if not s:
        return time.strftime("%Y-%m-%d")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % m.groups()
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%d %b %Y %H:%M:%S %Z", "%a, %d %b %Y"):
        try:
            return time.strftime("%Y-%m-%d", time.strptime(s, fmt))
        except Exception:
            continue
    m = re.search(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})", s)
    if m:
        try:
            return time.strftime("%Y-%m-%d", time.strptime(
                "%s %s %s" % (m.group(1), m.group(2), m.group(3)), "%d %b %Y"))
        except Exception:
            pass
    return time.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 分类判定

# (正则, category) —— 从上往下匹配，第一条命中即用
CATEGORY_RULES = [
    # 港澳台放最前：HK/TW 的演出稿常带「演唱會」，否则会被下面的 vocal 抢走
    (r"港澳|香港|澳门|hong kong|macau|macao|hktb|cantopop|粤语|紅館|红馆|啟德|启德", "greaterchina"),
    (r"台湾|taiwan|taipei|金曲奖|golden melody|小巨蛋|台北", "greaterchina"),
    (r"游戏音乐|游戏配乐|game music|game soundtrack|video game|gta\s?6|"
     r"grand theft auto|原声带|soundtrack|配乐|\bost\b|主机游戏|手游", "game"),
    (r"音乐科技|music tech|音频技术|科技大会|ai 音乐|ai music|生成式音乐", "industry"),
    (r"古典|交响|管弦|歌剧|concerthall|symphon|orchestra|opera|philharmon|古典音乐|室内乐", "classical"),
    (r"音乐剧|musical theatre|musical theater|west end|broadway|百老汇|伦敦西区", "musical"),
    (r"k-?pop|韩国|hybe|sm娱乐|jyp|yg娱乐|aespa|blackpink|bts|seventeen|newjeans|ive\b|le sserafim|korea", "kpop"),
    # 东南亚本地语种（泰/越/印尼/菲/马来）：此前这些标题一个关键词都匹配不上，
    # 全部掉进兜底的 west（实测 9 条东南亚稿 9 条被判 west）。必须显式判。
    (r"คอนเสิร์ต|คอนเสริต์|เพลง|อัลบั้ม|นักร้อง|ศิลปิน|ดนตรี|"
     r"ca sĩ|bài hát|âm nhạc|nhạc sĩ|ban nhạc|đêm nhạc|sân khấu|"
     r"\bmusik\b|\blagu\b|\bkonser\b|penyanyi|rilis single|album baru|"
     r"\bp-pop\b|\bopm\b|\bt-pop\b|\bv-pop\b|"
     r"concert film|world tour|nct 127|enhypen|\bbini\b|\bsb19\b", "globalpop"),
    (r"j-?pop|日本|杰尼斯|johnny|sony music japan|japan|oricon|anisong|city pop|japanese", "jpop"),
    (r"说唱|嘻哈|hip-?hop|rap\b|rapper|trap|drill", "hiphop"),
    (r"电子|techno|house music|\bedm\b|\bdj\b|dj mag|synth|dubstep|drum and bass|electronic", "electro"),
    (r"摇滚|rock\b|metal|punk|indie|grunge|shoegaze", "rock"),
    (r"爵士|jazz|blues|蓝调|\bsoul\b|r&b|rnb|funk|motown", "classical"),
    (r"非遗|文化遗产|heritage|传统音乐|昆曲|京剧|古琴|world music|folk music", "heritage"),
    (r"国风|古风|guofeng|chinese style|民乐|古筝|琵琶|二胡", "guofeng"),
    (r"民族音乐|民族|ethnic|minority music|throat sing", "folk"),
    (r"声乐|vocal|美声|bel canto|合唱|choir|歌唱家|歌者|嗓音", "vocal"),
    (r"华语|大陆|中国|chinese mainland|mandarin|tencent music|netease|云音乐|qq音乐|音著协|音集协", "cpop"),
    (r"版税|royalt|版权|copyright|licens|publishing deal|catalog sale|诉讼|lawsuit|集体管理|collecting society|ifpi|cisac", "report"),
    (r"报告|report|survey|study|streaming|market share|revenue|growth|"
     r"mid-?year|luminate|pollstar|billboard 200|chart|统计|数据|"
     r"财报|营收|净利润|毛利|付费用户|月活|订阅数|billion|£\s?\d", "report"),
    (r"融资|收购|acquisition|merger|funding|ipo|valuation|ceo|layoff|裁员|股价|earnings|quarterly|"
     r"raises \$|secures \$|\$\d+[mb]\b|capital|appoint|president of|joins\b|partnership|deal\b", "industry"),
    (r"巡演|tour|concert|gig|festival|ticket|票务|ticketmaster|live nation|livenation|stadium|arena", "industry"),
]
GOSSIP = (r"分手|恋情|结婚|离婚|怀孕|道歉|去世|离世|die[sd]?\b|death|passed away|hospital|arrest|"
          r"drama|feud|beef|回应|澄清|恋情|绯闻|自拍|造型|代言|联名|官宣|结婚|求婚|婚变|"
          # 东南亚本地语的讣告与婚恋词，否则泰国歌手讣告会被判成新歌
          r"อาลัย|เสียชีวิต|จากไป|qua đời|chia tay|kết hôn|đám cưới|"
          r"meninggal|berpulang|menikah|bercerai|pacar")


def guess_category(title, body=""):
    t = (title or "") + " " + (body or "")
    tl = t.lower()
    # 艺人动态优先（软新闻）
    if re.search(GOSSIP, t, re.I):
        return "gossip"
    for pat, cat in CATEGORY_RULES:
        if re.search(pat, tl, re.I):
            return cat
    # 兜底：带泰文/越南语声调符号的稿子是东南亚本地内容，归 west 明显不对。
    # 这些源在采集阶段已过音乐相关性过滤，剩下的都是本地流行艺人动态。
    if re.search(r"[\u0e00-\u0e7f]|[ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]", tl):
        return "globalpop"
    return "west"


GEO_RULES = [
    # ===== 地理名词（国家 / 城市）。必须排在流派词之前 =====
    # 澳门必须单列在香港之前：原先和「香港」写在同一条里，凡提到澳门的稿子
    # 一律被归成中国香港，「梓渝澳门演唱会」这种本地演出就挂错了地区。
    (r"澳门|澳門|macau|macao|cotai|威尼斯人|新濠|银河综艺", "中国澳门"),
    (r"香港|hong kong|hktb|红馆|红磡", "中国香港"),
    (r"台湾|taiwan|taipei", "中国台湾"),
    (r"中国|大陆|mainland|北京|上海|深圳|广州|腾讯音乐|网易云|音著协|音集协|"
     # 英文公司/平台名：标题只写 "Tencent Music reports..." 时无中文地理词，
     # 会被来源媒体的语区兜底挂错（实测挂在 Music In Africa 名下变成「非洲」）。
     r"tencent|netease|bytedance|字节跳动|qq\s*music|kugou|kuwo|"
     r"中国音乐著作权协会|中国音像|"
    # 省市名（2026-11-19 补）：岳阳古琴稿此前无「中国」字样被兜到「全球」。
    r"湖南|岳阳|长沙|成都|重庆|杭州|南京|武汉|西安|天津|苏州|郑州|青岛|厦门|"
    r"昆明|贵阳|合肥|济南|福州|南昌|南宁|石家庄|太原|沈阳|哈尔滨|兰州", "中国大陆"),
    # 东南亚：先判具体国家，再兜到「东南亚」。顺序不能倒，否则「新加坡演唱会」
    # 会被泛东南亚规则吃掉，地区筛选就分不出国别了。
    (r"新加坡|singapore", "新加坡"),
    (r"马来西亚|馬來西亞|malaysia|kuala lumpur|吉隆坡|槟城|檳城|新山", "马来西亚"),
    (r"泰国|泰國|thailand|bangkok|曼谷|清迈|清邁", "泰国"),
    (r"印尼|印度尼西亚|印度尼西亞|indonesia|jakarta|雅加达|雅加達|峇里|巴厘", "印度尼西亚"),
    (r"菲律宾|菲律賓|philippines|manila|马尼拉|馬尼拉|cebu|宿务|宿霧", "菲律宾"),
    (r"越南|vietnam|hanoi|ho chi minh|河内|河內|胡志明|岘港|峴港", "越南"),
    (r"东南亚|東南亞|southeast asia|south-east asia|asean|东盟|東盟", "东南亚"),
    # --- 全球补源地区（2026-11-19）：地理词 ---
    (r"\bindia\b|indian|mumbai|delhi|kolkata|chennai|bollywood|भारत|भारतीय|"
     # 印度各邦与货币线索：「阿萨姆邦音乐会经济」这类稿标题里没有 india 也该归印度
     r"assam|meghalaya|kerala|punjab|rajasthan|gujarat|tamil nadu|"
     r"rupee|rupees|₹|卢比|印度卢比", "印度"),
    (r"brasil|brazil|méxico|mexico|argentina|colombia|chile|perú|peru|venezuela|"
     r"latin america|latinoamérica|latinoamerica|são paulo|sao paulo|buenos aires|"
     r"mexico city|rio de janeiro|\brio\b|salsa|reggaeton|cumbia", "拉丁美洲"),
    (r"africa|nigeria|afrobeats|lagos|kenya|ghana|south africa|johannesburg|"
     r"cape town|egypt|cairo|morocco|tanzania", "非洲"),
    (r"russia|russian|москва|москов|россий|moscow|saint petersburg|краснодар", "俄罗斯"),
    (r"türkiye|turkiye|turkey\b|istanbul|ankara|dubai|emirates|saudi|doha|qatar|"
     r"israel|tel aviv|iran|tehran|الرياض|دبي", "中东"),
    # 日韩：地理词（首尔/东京），流派词挪到 GENRE_RULES 做兜底
    (r"韩国|한국|korea|seoul|서울|hybe|sm娱乐|jyp|yg娱乐", "韩国"),
    (r"日本|japan|tokyo|osaka|kyoto|oricon|natalie", "日本"),
    (r"英国|uk\b|britain|london|manchester|billboard uk|nme|"
     r"germany|deutschland|france|frança|italy|italia|spain|españa|portugal|"
     r"berlin|berlim|paris|madrid|rome|roma|milan|milano|munich|münchen|hamburg|"
     r"barcelona|lyon|lisbon|lisboa|amsterdam|stockholm|oslo|copenhagen|"
     r"wien|vienna|zürich|zurich|prague|praha|brussels", "欧美"),
    (r"美国|us\b|america|new york|los angeles|nashville|chicago|austin|atlanta|"
     r"cma awards|acm awards|billboard music awards|mtv vmas?|grammy awards|rock and roll hall", "欧美"),
    # 只收真正代表全球口径的机构；worldwide/global/world 常是发行范围
    # （"Worldwide Concert Film"），不能当报道地区。
    (r"ifpi|cisac", "全球"),
]

# ===== 流派 / 机构名兜底。地理词都没命中时才用 =====
# 「K-pop comeback in Jakarta」若按流派判会挂到韩国，实际发生在印尼。
# 所以流派只能当兜底：2026-11-19 把 k-?pop / j-?pop 从国家规则里拆出来挪到这里。
GENRE_RULES = [
    (r"cantopop|粤语|粵語|粵語歌", "中国香港"),
    (r"mandopop|华语流行", "中国大陆"),
    (r"k-?pop|kpop", "韩国"),
    (r"j-?pop|jpop", "日本"),
]

# 兼容旧调用：完整规则序列
REGION_RULES = GEO_RULES + GENRE_RULES


SEA_REGIONS = {"新加坡", "马来西亚", "泰国", "印度尼西亚", "菲律宾", "越南", "东南亚"}


def guess_region(title, body="", url=""):
    """地区判定：标题地理词 > 域名语区 > 流派兜底。

    2026-11-19 改为「标题地理词优先」：Rolling Stone Brasil 报「Bruce Springsteen
    柏林演唱会」这类稿，若按域名归拉丁美洲就挂错地区了。只有标题里没有地理词时
    （例：海峡时报报 Taylor Swift 新歌、RS India 报 Michael Jackson 官司）才用
    媒体所在语区兜底。
    """
    d = domain_of(url)
    dom_reg = None
    for dom, reg in DOMAIN_REGION.items():
        if d == dom or d.endswith("." + dom):
            dom_reg = reg
            break
    t = ((title or "") + " " + (body or "")).lower()
    for pat, reg in GEO_RULES:
        if re.search(pat, t, re.I):
            return reg
    if dom_reg:
        return dom_reg
    for pat, reg in GENRE_RULES:
        if re.search(pat, t, re.I):
            return reg
    return "全球"


# ---------------------------------------------------------------- 源 1：Bing News RSS

BING_QUERIES = [
    # query 只决定"抓什么"，不决定分类：同一 query 的返回里混着新歌、巡演、财报，
    # 查询级 hint 曾把 Consequence 的新专辑稿全塞进 report，故一律交给关键词判定。

    # --- 产业 / 版权 / 市场（重点，必抓）---
    ("music industry report", "en-US", "en", None),
    ("music streaming revenue", "en-US", "en", None),
    ("record label acquisition", "en-US", "en", None),
    ("live music touring box office", "en-US", "en", None),
    ("music copyright lawsuit", "en-US", "en", None),
    ("global music awards Grammy Billboard", "en-US", "en", None),
    ("音乐产业 报告", "zh-CN", "zh", None),

    # --- 流媒体平台动态（榜单榜位由 collect_charts.py 抓，这里抓平台新闻）---
    ("Spotify new feature chart", "en-US", "en", None),
    ("网易云音乐 新功能 榜单", "zh-CN", "zh", None),
    ("QQ音乐 酷狗音乐 汽水音乐 动态", "zh-CN", "zh", None),
    ("腾讯音乐 网易云音乐 财报", "zh-CN", "zh", None),

    # --- 演出场所：演唱会 / 音乐节 / Livehouse / 场馆（小到各场所）---
    ("演唱会 官宣 定档 开票 加场", "zh-CN", "zh", None),
    ("音乐节 阵容 公布", "zh-CN", "zh", None),
    ("音乐厅 音乐会 交响乐 演出", "zh-CN", "zh", None),
    ("livehouse 演出 独立音乐现场", "zh-CN", "zh", None),
    ("concert tour dates announcement venue", "en-US", "en", None),
    ("music festival lineup 2026", "en-US", "en", None),

    # --- 音乐剧 / 古典 / 歌剧 / 民族（细分品类）---
    ("音乐剧 首演 巡演 中文版", "zh-CN", "zh", None),
    ("musical theater opening Broadway West End", "en-US", "en", None),
    ("classical concert orchestra opera house", "en-US", "en", None),
    ("民族音乐 非遗 民歌 演出", "zh-CN", "zh", None),

    # --- 游戏音乐 / 影视原声 ---
    ("游戏音乐 原声 OST", "zh-CN", "zh", None),
    ("video game soundtrack composer", "en-US", "en", None),

    # --- 港澳台 / 日韩 ---
    ("香港 音樂 演唱會 紅館", "zh-HK", "zh-HK", None),
    ("台灣 音樂 演唱會 音樂節", "zh-TW", "zh-TW", None),
    ("音楽 業界 ライブ コンサート", "ja-JP", "ja", None),
    ("케이팝 컴백 콘서트", "ko-KR", "ko", None),

    # --- 东南亚（2026-11-19 新增；此前只有 1 条新加坡收录）---
    ("东南亚 音乐 演唱会 音乐节", "zh-CN", "zh", None),
    ("新加坡 演唱会 音乐节 歌手", "zh-CN", "zh", None),
    ("马来西亚 演唱会 音乐节 歌手", "zh-CN", "zh", None),
    ("泰国 音乐节 演唱会 歌手", "zh-CN", "zh", None),
    ("印尼 音乐 演唱会 歌手", "zh-CN", "zh", None),
    ("Southeast Asia music festival concert", "en-US", "en", None),
    ("Singapore Malaysia concert tour K-pop", "en-US", "en", None),
    ("Thailand Indonesia Philippines music festival lineup", "en-US", "en", None),

    # --- 印度（2026-11-19 补：南亚最大市场，此前完全空白）---
    ("India music industry concert", "en-IN", "en", None),
    ("Bollywood music release tour", "en-IN", "en", None),
    ("भारत संगीत कंसर्ट", "hi-IN", "hi", None),

    # --- 拉丁美洲（2026-11-19 补）---
    ("industria musical México concierto gira", "es-MX", "es", None),
    ("música latina concierto festival", "es-US", "es", None),
    ("indústria da música Brasil concerto festival", "pt-BR", "pt", None),

    # --- 欧洲本地（2026-11-19 补：德法意西，此前只抓英语媒体）---
    ("Musikindustrie Deutschland Konzert Festival", "de-DE", "de", None),
    ("industrie de la musique France concert", "fr-FR", "fr", None),
    ("industria musicale Italia concerto festival", "it-IT", "it", None),
    ("actualidad música España concierto gira", "es-ES", "es", None),

    # --- 俄罗斯 / 中东 / 非洲（2026-11-19 补）---
    ("музыкальная индустрия Россия концерт", "ru-RU", "ru", None),
    ("Türkiye müzik endüstrisi konser festival", "tr-TR", "tr", None),
    ("Nigeria Afrobeats concert tour", "en-US", "en", None),
    ("Africa music industry festival", "en-US", "en", None),
]


def _bing_fetch(q, mkt, lang):
    """Bing 对部分市场参数会 302 掉格式，取不到就退回不带市场参数。"""
    qq = urllib.parse.quote(q)
    variants = [
        "https://www.bing.com/news/search?q=%s&format=RSS&setmkt=%s&setlang=%s" % (qq, mkt, lang),
        "https://www.bing.com/news/search?q=%s&format=RSS&setlang=%s" % (qq, lang),
        "https://www.bing.com/news/search?q=%s&format=RSS" % qq,
    ]
    for i, u in enumerate(variants):
        xml = fetch(u, timeout=22, retry=0)
        if xml and "<item>" in xml:
            return xml
    return None


def _bing_one(q, mkt, lang, hint, per, out, is_gap=False):
    """跑一个检索词，把结果追加进 out。返回本次条数。"""
    xml = _bing_fetch(q, mkt, lang)
    if not xml:
        log("  bing[%s] %-28s -> 0 条" % (mkt, q))
        return 0
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    got = 0
    for it in items[:per]:
        m_t = re.search(r"<title>(.*?)</title>", it, re.S)
        m_l = re.search(r"<link>(.*?)</link>", it, re.S)
        m_d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        m_desc = re.search(r"<description>(.*?)</description>", it, re.S)
        if not (m_t and m_l):
            continue
        title = strip_tags(m_t.group(1))
        link = clean_url(m_l.group(1))
        if not title or not link.startswith("http"):
            continue
        if "bing.com" in link:   # 没解析出原文的丢弃
            continue
        out.append({
            "title": title,
            "source_url": link,
            "source": domain_of(link),
            "publish_date": parse_date(m_d.group(1) if m_d else ""),
            "summary": strip_tags(m_desc.group(1) if m_desc else "")[:160],
            "category_hint": hint,
            "via": "bing",
            # 用户点名要的选题：写库时跳过「音乐关键词」严格过滤，
            # 但仍要过非音乐拦截（游戏/体育账号的推文照样挡）。
            "_origin": "gap" if is_gap else "",
        })
        got += 1
    log("  bing[%s] %-28s -> %d 条%s" % (mkt, q, got, "  ← 用户提问" if is_gap else ""))
    time.sleep(1.0)
    return got


def source_bing(limit=30, queries=None, gap_queries=None):
    out = []
    # 关键词已扩到 30+ 个（含东南亚）。写死「每词 6 条」会让总条数在第 20 个词就吃满
    # limit，排队尾的东南亚/音乐剧/场馆词永远跑不到。改成按词数动态分配每词条数：
    # 33 词 / limit 120 → 每词 3 条，全部关键词都能执行。
    # gap_queries 是用户「问了但站内没有」的提问，必须单独先跑 --
    # 排在队尾会被 limit*2 的提前 break 直接跳过，那等于没补。
    queue = list(BING_QUERIES if queries is None else queries)
    gaps = set(gap_queries or [])
    gap_queue = [x for x in queue if x[0] in gaps]
    main_queue = [x for x in queue if x[0] not in gaps]

    for q, mkt, lang, hint in gap_queue:
        _bing_one(q, mkt, lang, hint, 6, out, is_gap=True)

    per = max(2, min(6, limit // max(1, len(main_queue))))
    for q, mkt, lang, hint in main_queue:
        if len(out) >= limit * 2:
            break
        _bing_one(q, mkt, lang, hint, per, out)
    return out


# ---------------------------------------------------------------- 源 2：GDELT

GDELT_QUERIES = [
    ('"music industry" sourcelang:english', "report"),
    ('音乐产业 sourcelang:chinese', "cpop"),
    ('"live music" sourcelang:english', "industry"),
    ('케이팝 sourcelang:korean', "kpop"),
]


def source_gdelt(limit=20):
    """GDELT 走共享出口 IP，限流极重（1 次/5 秒，经常直接 429）。
    因此设成弱源：最多试 4 条查询，一次失败即整体放弃，绝不拖慢主流程。"""
    out = []
    for q, hint in GDELT_QUERIES:
        if len(out) >= limit:
            break
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
               "&mode=ArtList&maxrecords=25&format=json&timespan=1d"
               % urllib.parse.quote(q))
        txt = fetch(url, timeout=18, retry=0)
        if not txt or txt.lstrip().startswith("Please limit"):
            log("  gdelt 限流，跳过剩余查询")
            break
        try:
            data = json.loads(txt)
        except Exception:
            log("  gdelt 返回非 JSON，跳过")
            break
        arts = data.get("articles") or []
        got = 0
        for a in arts:
            u = a.get("url") or ""
            if not u.startswith("http"):
                continue
            out.append({
                "title": (a.get("title") or "").strip(),
                "source_url": clean_url(u),
                "source": a.get("domain") or domain_of(u),
                "publish_date": (a.get("seendate") or "")[:4] + "-" +
                                (a.get("seendate") or "")[4:6] + "-" +
                                (a.get("seendate") or "")[6:8],
                "summary": "",
                "category_hint": hint,
                "via": "gdelt",
            })
            got += 1
        log("  gdelt %-34s -> %d 条" % (q[:34], got))
        time.sleep(7)
    return out


# ---------------------------------------------------------------- 源 3：出版社 RSS

PUBLISHER_FEEDS = [
    ("Billboard", "https://www.billboard.com/feed/", None),
    # 产业媒体不预设 category，交给关键词判定，否则并购/人事新闻会被误塞进 report
    ("Music Business Worldwide", "https://www.musicbusinessworldwide.com/feed/", None),
    # Digital Music News：整站 Cloudflare 520（源站故障），2026-11-19 实测本机与云主机均挂，暂时移除，恢复后可加回
    # ("Digital Music News", "https://www.digitalmusicnews.com/feed/", None),
    # CMU：www 子域证书异常（525），去掉 www 正常，2026-11-19 实测 200
    ("Complete Music Update", "https://completemusicupdate.com/feed", None),
    ("Musically", "https://musically.com/feed/", None),
    ("Variety Music", "https://variety.com/v/music/feed/", None),
    ("Rolling Stone Music", "https://www.rollingstone.com/music/feed/", None),
    ("NME", "https://www.nme.com/feed", None),
    ("Pitchfork", "https://pitchfork.com/feed/feed-news/rss", "rock"),
    ("Stereogum", "https://www.stereogum.com/feed/", "rock"),
    ("Consequence", "https://consequence.net/feed/", None),
    ("Spin", "https://www.spin.com/feed/", "rock"),
    ("Brooklyn Vegan", "https://www.brooklynvegan.com/feed/", "rock"),
    ("The Guardian Music", "https://www.theguardian.com/music/rss", None),
    ("NPR Music", "https://www.npr.org/rss/rss.php?id=1039", None),
    ("DJ Mag", "https://djmag.com/rss", "electro"),
    ("Slipped Disc", "https://slippedisc.com/feed/", "classical"),
    ("Soompi", "https://www.soompi.com/feed", "kpop"),
    # Hypebot：/hypebot/atom.xml 等全部 404，网站已无 RSS，2026-11-19 实测，移除
    # ("Hypebot", "https://www.hypebot.com/hypebot/atom.xml", None),
    # --- 演出/现场/场馆/票务 ---
    ("Pollstar", "https://www.pollstar.com/feed", "industry"),
    # IQ Magazine：国际现场演出产业媒体，补 Hypebot 留下的产业空缺，2026-11-19 实测 200
    ("IQ Magazine", "https://www.iq-mag.net/feed/", "industry"),
    # --- 音乐剧 / 戏剧 ---
    ("BroadwayWorld", "https://www.broadwayworld.com/rss.cfm", "musical"),
    # --- 古典 / 歌剧 ---
    ("Opera News", "https://www.operanews.com/rss", "classical"),
    # --- 大型演出主办 ---
    ("Live Nation", "https://www.livenation.com/feed", None),
    # --- 东南亚（2026-11-19 新增，逐个实测可达）---
    # 综合媒体（海峡时报/泰国 The Standard/越南 Thanh Nien/菲律宾 Inquirer）不是纯音乐源，
    # 由 MUSIC_ONLY_FEEDS + MUSIC_HINT 在 source_rss 里做音乐相关性硬过滤，
    # 否则会把社会新闻、时尚、影视一起拖进来，破坏内容审核红线。
    ("Hype MY", "https://hype.my/feed", None),                               # 马来西亚 娱乐/音乐
    ("Rappler Ent", "https://www.rappler.com/entertainment/rss", None),      # 菲律宾 娱乐
    ("Inquirer Lifestyle", "https://lifestyle.inquirer.net/feed", None),     # 菲律宾 综合
    ("VnExpress Giai Tri", "https://vnexpress.net/rss/giai-tri.rss", None),  # 越南 娱乐
    ("Thanh Nien Van Hoa", "https://www.thanhnien.vn/rss/van-hoa.rss", None),  # 越南 文化
    ("Detik Hot", "https://hot.detik.com/rss", None),                        # 印尼 娱乐
    ("The Standard TH", "https://www.thestandard.co/rss", None),             # 泰国 综合
    ("Straits Times Life", "https://www.straitstimes.com/news/life/rss.xml", None),  # 新加坡 生活

    # --- 全球补源（2026-11-19 实测 200 且为有效 RSS）：印度 / 拉美 / 欧洲本地 / 非洲 / 俄 / 波兰 ---
    ("Rolling Stone India", "https://rollingstoneindia.com/feed/", None),           # 印度
    ("MusicPlus India", "https://www.musicplus.in/feed/", None),                    # 印度
    ("The Hindu Music", "https://www.thehindu.com/entertainment/music/feeder/default.rss", None),  # 印度（综合媒体音乐频道）
    ("Rolling Stone Brasil", "https://www.rollingstone.com.br/feed/", None),        # 巴西
    ("Musikexpress", "https://www.musikexpress.de/feed/", None),                    # 德国
    ("Tsugi", "https://www.tsugi.fr/feed/", "electro"),                             # 法国 电子
    ("Rockit", "https://www.rockit.it/rss", None),                                  # 意大利
    ("MondoSonoro", "https://www.mondosonoro.com/feed/", None),                     # 西班牙
    ("Music In Africa", "https://www.musicinafrica.net/rss", None),                 # 非洲
    ("InterMedia", "https://intermedia.ru/feed/", None),                            # 俄罗斯
    ("Muno", "https://www.muno.pl/rss", None),                                      # 波兰
]

# 这些源是综合媒体而不是音乐垂直媒体，必须逐条过音乐关键词才收。
# 东南亚本地很难找到稳定的纯音乐 RSS（Bandwagon 无 feed、8world/ABS-CBN 403），
# 只能拿综合媒体的娱乐/生活频道 + 关键词过滤，宁可少收也不能让非音乐内容混进来。
MUSIC_ONLY_FEEDS = {
    "Inquirer Lifestyle", "Thanh Nien Van Hoa", "The Standard TH", "Straits Times Life",
    "Rappler Ent", "VnExpress Giai Tri", "Detik Hot", "Hype MY",
    # 2026-11-19 全球补源里三个非纯音乐源，同样必须过关键词
    "The Hindu Music", "Rolling Stone Brasil", "InterMedia",
}

# 多语言音乐相关性判据（英/中/泰/印尼/越），只在 MUSIC_ONLY_FEEDS 上生效。
# 分强/弱两档：强词单独出现即可判音乐（music/歌手/เพลง/ca sĩ…），
# 弱词（awards/chart/orchestra…）单独出现往往是影视、亲子、生活方式稿的顺带提及，
# 必须凑够两个才认。这样 "Big adventures ... Chinese orchestra / LEGO" 这类亲子稿
# 不会被一条 orchestra 就拖进音乐库。
MUSIC_STRONG = re.compile(
    r"\bmusic\b|\bsongs?\b|\balbums?\b|\bsingles?\b|\bconcerts?\b|\btours?\b|"
    r"\bfestivals?\b|\bsingers?\b|\bbands?\b|\brappers?\b|\bdjs?\b|"
    r"k-?pop|\bp-pop\b|\bopm\b|\bv-pop\b|grammy|eurovision|billboard|"
    r"record label|spotify|"
    r"音乐|音樂|歌曲|专辑|專輯|单曲|單曲|演唱会|演唱會|音乐节|音樂節|"
    r"歌手|乐团|樂團|乐队|樂隊|巡演|音乐剧|音樂劇|原声|原聲|歌词|歌詞|音乐人|音樂人|"
    r"เพลง|คอนเสิร์ต|อัลบั้ม|นักร้อง|ดนตรี|ศิลปิน|"
    r"\bmusik\b|\blagu\b|\bkonser\b|penyanyi|"
    r"ca sĩ|bài hát|âm nhạc|nhạc sĩ|ban nhạc|"
    # --- 多语种（2026-11-19 补：西/葡/德/法/意/俄/波/印地/土/阿）---
    r"música|musica|canción|cancion|álbum|álbum|concierto|conciertos|"
    r"\bgira\b|cantante|cantantes|\bbandas?\b|"
    r"musik|lied|konzert|konzerte|sänger|saenger|"
    r"musique|chanson|chanteur|chanteuse|tournée|tournee|"
    r"canzone|canzoni|concerto|concerti|cantante|"
    r"музыка|песн|альбом|концерт|концерты|певец|певица|группа|фестивал|"
    r"muzyka|piosenka|koncert|piosenkarz|zespół|zespol|festiwal|"
    r"संगीत|गाना|एल्बम|कंसर्ट|गायक|"
    r"müzik|muzik|şarkı|sarki|albüm|album|şarkıcı|sarkici|"
    r"موسيقى|أغنية|اغنية|ألبوم|البوم|حفلة|حفلة|مغن|"
    # 音乐节专名（2026-11-19 补）：葡语/西语标题常只报节名不带 música，
    # 例 "Rock in Rio anuncia lineup" 会被音乐相关性误判成非音乐而丢弃。
    r"rock in rio|lollapalooza|coachella|glastonbury|primavera sound|"
    r"fuji rock|summerfest|sziget|rock am ring|rock im park|"
    r"download festival|wacken|hellfest|roskilde|exit festival|"
    r"woodstock|reading festival|leeds festival|splendour|"
    # --- 中文民族乐器 / 非遗 / 戏曲（2026-11-19 补：国琴网古琴稿此前被误杀）---
    r"古琴|古筝|二胡|琵琶|柳琴|扬琴|中阮|大阮|箜篌|编钟|唢呐|马头琴|冬不拉|热瓦普|"
    r"斫琴|琴人|琴家|琴社|古琴制作|民乐|民歌|山歌|花儿|呼麦|长调|侗族大歌|木卡姆|"
    r"非遗|昆曲|京剧|越剧|粤剧|评弹|南音|戏曲|曲艺|梨园|唱腔",
    re.I)

MUSIC_WEAK = re.compile(
    r"\bawards?\b|\bcharts?\b|playlist|remix|producer|vocalist|lyrics?|"
    r"orchestra|soundtrack|\bmusicals?\b|\bidols?\b|trainee|comeback|"
    r"\bdebut\b|\bgigs?\b|cma\b|mnet|golden disc|"
    r"唱片|交响|交響|榜单|榜單|音乐奖|音樂獎|颁奖|頒獎|金曲",
    re.I)


def music_relevant(title, desc):
    """综合媒体的音乐相关性判定。

    只看「标题 + 前 160 字摘要」而不是全文：source_rss 只把 desc[:160] 写进库，
    用全文判定会出现「按全文收下、用户却只看到无关摘要」的错配
    （实测新加坡亲子活动稿因全文提到 Chinese orchestra 被误收）。
    """
    head = (title or "") + " " + (desc or "")[:160]
    if MUSIC_STRONG.search(head):
        return True
    weak = set(m.group(0).lower() for m in MUSIC_WEAK.finditer(head))
    return len(weak) >= 2

# 域名画像：媒体所在语区基本固定，比正文关键词更准，优先采用
DOMAIN_REGION = {
    "billboard.com": "欧美", "variety.com": "欧美", "rollingstone.com": "欧美",
    "nme.com": "欧美", "spin.com": "欧美", "pitchfork.com": "欧美",
    "stereogum.com": "欧美", "consequence.net": "欧美", "brooklynvegan.com": "欧美",
    "theguardian.com": "欧美", "npr.org": "欧美", "slippedisc.com": "欧美",
    "soompi.com": "韩国", "koreatimes.co.kr": "韩国", "koreaherald.com": "韩国",
    "japantimes.co.jp": "日本", "oricon.co.jp": "日本", "natalie.mu": "日本",
    "scmp.com": "中国香港", "hkej.com": "中国香港", "hk01.com": "中国香港",
    "udn.com": "中国台湾", "ltn.com.tw": "中国台湾", "cna.com.tw": "中国台湾",
    "sina.com.cn": "中国大陆", "163.com": "中国大陆", "qq.com": "中国大陆",
    "thepaper.cn": "中国大陆", "yicai.com": "中国大陆", "jiemian.com": "中国大陆",
    "musicbusinessworldwide.com": "全球", "digitalmusicnews.com": "全球",
    "musically.com": "全球", "completemusicupdate.com": "全球", "djmag.com": "全球",
    # 票务/场馆/演出
    "damai.cn": "中国大陆", "maoyan.com": "中国大陆", "showstart.com": "中国大陆",
    "moretickets.com": "中国大陆", "cityline.com.hk": "中国香港",
    "cotaiticketing.com": "中国澳门", "kktix.com": "中国台湾",
    "ticketmaster.com": "欧美", "pollstar.com": "欧美", "livenation.com": "欧美",
    "playbill.com": "欧美", "broadway.com": "欧美",
    # 垂直媒体
    "gramophone.co.uk": "欧美", "operanews.com": "欧美", "residentadvisor.net": "欧美",
    "oricon.co.jp": "日本", "natalie.mu": "日本", "billboard-japan.com": "日本",
    # 东南亚（2026-11-19 新增）：本地媒体语区固定，比正文关键词可靠
    "straitstimes.com": "新加坡", "channelnewsasia.com": "新加坡",
    "zaobao.com.sg": "新加坡", "todayonline.com": "新加坡",
    "hype.my": "马来西亚", "thestar.com.my": "马来西亚", "malaymail.com": "马来西亚",
    "bangkokpost.com": "泰国", "thestandard.co": "泰国", "khaosodenglish.com": "泰国",
    "detik.com": "印度尼西亚", "kompas.com": "印度尼西亚", "thejakartapost.com": "印度尼西亚",
    "rappler.com": "菲律宾", "inquirer.net": "菲律宾", "abs-cbn.com": "菲律宾",
    "vnexpress.net": "越南", "thanhnien.vn": "越南", "vietnamnet.vn": "越南",
    "bandwagon.asia": "东南亚",
    # --- 全球补源域名（2026-11-19）：本地媒体语区固定，比正文关键词可靠 ---
    "rollingstoneindia.com": "印度", "musicplus.in": "印度", "thehindu.com": "印度",
    "rollingstone.com.br": "拉丁美洲",
    "musicinafrica.net": "非洲", "intermedia.ru": "俄罗斯",
    # 欧洲本地媒体归「欧美」，与既有 203 条欧美稿件口径一致，不另开筛选项
    "musikexpress.de": "欧美", "tsugi.fr": "欧美", "rockit.it": "欧美",
    "mondosonoro.com": "欧美", "muno.pl": "欧美",
}


def parse_feed(xml):
    """兼容 RSS 2.0 / Atom，返回 [(title, link, date, desc)]。"""
    out = []
    try:
        root = ET.fromstring(xml.encode("utf-8", "ignore"))
    except Exception:
        # 兜底正则
        for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            l = re.search(r"<link>(.*?)</link>", it, re.S)
            d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            de = re.search(r"<description>(.*?)</description>", it, re.S)
            if t and l:
                out.append((strip_tags(t.group(1)), l.group(1).strip(),
                            d.group(1) if d else "", strip_tags(de.group(1)) if de else ""))
        return out

    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for c in el:
            ct = c.tag.split("}")[-1]
            if ct == "link":
                href = c.attrib.get("href")
                d["link"] = href if href else (c.text or "")
            elif ct in ("title", "pubDate", "published", "updated", "description", "summary"):
                d[ct] = "".join(c.itertext())
        out.append((strip_tags(d.get("title", "")),
                    (d.get("link") or "").strip(),
                    d.get("pubDate") or d.get("published") or d.get("updated") or "",
                    strip_tags(d.get("description") or d.get("summary") or "")))
    return out


def source_rss(limit=25):
    out = []
    # 源已到 30+（含东南亚）。固定每源 5 条会让排在队尾的东南亚源吃不到配额，
    # 改为按源数动态分配每源条数：34 源 / limit 120 → 每源 3 条。
    per = max(2, min(5, limit // max(1, len(PUBLISHER_FEEDS))))
    for name, url, hint in PUBLISHER_FEEDS:
        if len(out) >= limit * 2:
            break
        xml = fetch(url, timeout=20, retry=0)
        if not xml:
            continue
        ents = parse_feed(xml)
        music_only = name in MUSIC_ONLY_FEEDS
        # 综合媒体先多取 5 倍候选，再用音乐关键词筛出真正能收的那几条。
        cand = ents[:per * 5] if music_only else ents[:per]
        got = 0
        skipped = 0
        for title, link, date, desc in cand:
            if got >= per:
                break
            if not title or not link.startswith("http"):
                continue
            if music_only and not music_relevant(title, desc):
                skipped += 1
                continue
            out.append({
                "title": title,
                "source_url": clean_url(link),
                "source": name,
                "publish_date": parse_date(date),
                "summary": desc[:160],
                "category_hint": hint,
                "via": "rss",
            })
            got += 1
        if got or skipped:
            log("  rss %-26s -> %d 条%s" % (
                name, got, ("（滤掉 %d 条非音乐）" % skipped) if skipped else ""))
    return out


# ---------------------------------------------------------------- 源 4：Google News（热度）

GNEWS_QUERIES = [
    ("music industry", "en-US", "US", "en"),
    ("music streaming royalties", "en-US", "US", "en"),
    ("live music touring", "en-US", "US", "en"),
    ("Spotify chart music", "en-US", "US", "en"),
    ("concert tour music festival", "en-US", "US", "en"),
    ("音乐产业 演唱会", "zh-CN", "CN", "zh-CN"),
    ("网易云音乐 QQ音乐 榜单", "zh-CN", "CN", "zh-CN"),
    ("香港 演唱會", "zh-HK", "HK", "zh-HK"),
    ("台灣 音樂", "zh-TW", "TW", "zh-TW"),
    ("音楽 業界", "ja", "JP", "ja"),
    ("케이팝", "ko", "KR", "ko"),
    # 东南亚（2026-11-19 新增）
    ("Southeast Asia music concert", "en-SG", "SG", "en"),
    ("Singapore concert music festival", "en-SG", "SG", "en"),
    ("Malaysia concert K-pop music", "en-MY", "MY", "en"),
    ("东南亚 音乐 演唱会", "zh-CN", "CN", "zh-CN"),
]


def source_gnews_heat():
    """只产热度榜单：哪个媒体在写什么，用于校核别家源是否漏稿。"""
    rows = []
    for q, hl, gl, ceid in GNEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s:%s"
               % (urllib.parse.quote(q), hl, gl, gl, ceid))
        xml = fetch(url, timeout=25, retry=0)
        if not xml:
            continue
        items = re.findall(r"<item>(.*?)</item>", xml, re.S)
        titles = []
        for it in items[:40]:
            m_t = re.search(r"<title>(.*?)</title>", it, re.S)
            m_s = re.search(r'<source url="([^"]+)">([^<]+)</source>', it, re.S)
            if m_t:
                titles.append({"title": strip_tags(m_t.group(1)),
                               "publisher": m_s.group(2) if m_s else ""})
        rows.append({"query": q, "locale": "%s:%s" % (gl, ceid), "count": len(items),
                     "sample": titles[:8]})
        log("  gnews[%s] %-22s -> %d 条（仅计热度）" % (gl, q, len(items)))
        time.sleep(1.0)
    return rows


# ---------------------------------------------------------------- 源 5：YouTube

YT_QUERIES = [
    ("music industry interview 2026", "en"),
    ("new album release", "en"),
    ("official music video", "en"),
    ("音乐 现场", "zh"),
    ("K-pop comeback stage", "en"),
]


def source_youtube(limit=10):
    if not GOOGLE_API_KEY:
        return []
    out = []
    for q, _ in YT_QUERIES:
        if len(out) >= limit:
            break
        url = ("https://www.googleapis.com/youtube/v3/search?part=snippet"
               "&maxResults=6&order=date&type=video&q=%s&key=%s"
               % (urllib.parse.quote(q), GOOGLE_API_KEY))
        txt = fetch(url, timeout=25, retry=0)
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        if "error" in data:
            log("  youtube API 错误：%s" % data["error"].get("message", "")[:80])
            continue
        got = 0
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            out.append({
                "title": sn.get("title", ""),
                "source_url": "https://www.youtube.com/watch?v=%s" % vid,
                "source": "YouTube / %s" % sn.get("channelTitle", ""),
                "publish_date": (sn.get("publishedAt") or "")[:10],
                "summary": (sn.get("description") or "")[:160],
                "category_hint": "west",
                "via": "youtube",
            })
            got += 1
        log("  youtube %-30s -> %d 条" % (q[:30], got))
    return out


# ---------------------------------------------------------------- 汇总

def dedupe(rows):
    seen_t, seen_u, out = set(), set(), []
    for r in rows:
        nt = norm_title(r["title"])
        if len(nt) < 6:
            continue
        u = r["source_url"].split("?")[0].rstrip("/").lower()
        if nt in seen_t or u in seen_u:
            continue
        seen_t.add(nt)
        seen_u.add(u)
        out.append(r)
    return out


def recent_only(rows, days=3):
    # 与 cloud_writer 一致，按北京时间算窗口（GitHub Actions 是 UTC 机器）
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600 - days * 86400))
    keep = [r for r in rows if (r.get("publish_date") or "") >= cutoff]
    return keep or rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--source", default="all",
                    choices=["all", "bing", "gdelt", "rss", "youtube", "gnews"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--gaps", type=int, default=6,
                    help="额外为「用户问过但站内没有」的提问跑几个定向检索（0=关闭）")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    log("=== 声向 SOUND VECTOR 全球采集 开始 ===")
    p = detect_proxy(args.proxy)
    if p is None:
        log("终止：无可用网络通道")
        sys.exit(2)

    rows, heat = [], []
    per = args.limit

    # 先取「未收录提问」：它们决定这一轮额外要搜什么。取不到不影响正常采集。
    gap_qs, gap_items = [], []
    if args.gaps > 0 and args.source in ("all", "bing"):
        try:
            gap_qs, gap_items = build_gap_queries(args.gaps, log=log)
        except Exception as e:
            log("未收录提问读取失败，跳过：%s" % str(e)[:100])

    if args.source in ("all", "rss"):
        log("-- 出版社 RSS --")
        rows += source_rss(per)
    if args.source in ("all", "bing"):
        log("-- Bing News RSS --")
        rows += source_bing(per, queries=BING_QUERIES + gap_qs,
                            gap_queries=[q[0] for q in gap_qs])
    if args.source in ("all", "gdelt"):
        log("-- GDELT --")
        rows += source_gdelt(max(12, per // 3))
    if args.source in ("all", "youtube"):
        log("-- YouTube Data API v3 --")
        rows += source_youtube(min(12, per // 4))
    if args.source in ("all", "gnews"):
        log("-- Google News（热度） --")
        heat = source_gnews_heat()

    # 分类 / 地区 / 摘要补齐
    for r in rows:
        r["title"] = r["title"].strip()
        # 只按标题判定分类：RSS 描述里 MBW 每篇都带 revenue/streaming，
        # 把描述一起喂进去会让并购、人事稿成批误判成 report。
        r["category"] = r.get("category_hint") or guess_category(r["title"])
        # 明确的「报告 / 数据 / 版税」稿才纠正为 report；"Stream" 结尾的歌名不算
        if r["category"] != "gossip" and re.search(
                r"\breport\b|survey|study|mid-?year|market share|royalt|"
                r"streaming|revenue|earnings|quarterly|\d+%|billion|£\s?\d|"
                r"季度|报告|数据|版税|收入|财报|营收|净利润|毛利|付费用户|月活|订阅数",
                r["title"], re.I) and not re.search(
                r"announce|tour dates|new song|new album|share new|unleash|"
                r"listen to|watch\b|stream:\s*$|debut solo|remix|cover",
                r["title"], re.I):
            r["category"] = "report"
        # 并购 / 人事 / 资本类最后压过 report（语气最强的一类，放最后）
        if re.search(r"acquisition|merger|acquire|appoint|named |joins\b|"
                     r"partnership|funding|raises \$|secures \$|capital|stake\b|"
                     r"收购|并购|任命|融资|入股|签约", r["title"], re.I):
            r["category"] = "industry"
        r["region"] = guess_region(r["title"], r.get("summary", ""),
                                   r.get("source_url", ""))
        if not r.get("summary"):
            r["summary"] = r["title"][:120]

    rows = dedupe(rows)
    fresh = recent_only(rows, args.days)
    log("去重后 %d 条，近 %d 天 %d 条" % (len(rows), args.days, len(fresh)))

    # 按分类分布排序，保证板块不偏科
    fresh.sort(key=lambda r: r["publish_date"], reverse=True)
    # 稳定排序再按来源分组：用户点名要的选题排最前，别被 limit 卡在门外
    fresh.sort(key=lambda r: 0 if r.get("_origin") == "gap" else 1)
    picked, cap = [], {}
    for r in fresh:
        c = r["category"]
        if cap.get(c, 0) >= 14:
            continue
        cap[c] = cap.get(c, 0) + 1
        r["_idx"] = len(picked) + 1
        picked.append(r)
        if len(picked) >= args.limit:
            break

    stamp = time.strftime("%Y%m%d_%H%M")
    batch_path = os.path.join(OUT_DIR, "batch_%s.json" % stamp)
    with open(batch_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "proxy": p, "count": len(picked),
                   "articles": picked, "heat": heat,
                   "gap_queries": gap_items}, f, ensure_ascii=False, indent=1)

    # 人读汇总
    lines = ["# 全球采集批次 %s" % stamp, "",
             "- 代理：`%s`" % (p or "直连"),
             "- 候选：%d 条 → 去重 %d → 近 %d 天 %d → 采用 %d"
             % (len(rows) + 0, len(rows), args.days, len(fresh), len(picked)), ""]
    by_cat = {}
    for r in picked:
        by_cat.setdefault(r["category"], []).append(r)
    for c, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lines.append("## %s（%d）" % (c, len(items)))
        for r in items:
            lines.append("- [%s] %s — %s（%s）"
                         % (r["publish_date"], r["title"][:70], r["source"], r["region"]))
        lines.append("")
    if heat:
        lines.append("## Google News 热度榜（仅选题参考，链接不可入库）")
        for h in heat:
            lines.append("- %s `%s` %d 条" % (h["query"], h["locale"], h["count"]))
    rep_path = os.path.join(OUT_DIR, "report_%s.md" % stamp)
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log("=== 完成：%d 条 → %s ===" % (len(picked), batch_path))
    print(batch_path)
    print(rep_path)


if __name__ == "__main__":
    main()
