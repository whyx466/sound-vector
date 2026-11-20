#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 资讯提炼与拒录
==================================
用户 2026-11-19 投诉：观演攻略、卡司变更、取票须知这类「票务系统提示」不该原文入库，
要提炼成一句话新闻（如「11月21日音乐剧《博物馆奇遇记》将在广州海珠大戏院上演午晚两场」），
没有新闻价值的运营通知直接拒录。

本模块只依赖标准库，供 cloud_writer.py 在写库前调用，也可单独调试：

  python distill.py --title "观演攻略|音乐剧《博物馆奇遇记》..." --body-file out/xxx.txt
  python distill.py --selftest

判定分两级：
1. 标题命中强运营词（观演攻略/购票须知/卡司变更/取票/票根/盖章…）→ 进入提炼；
   正文前 800 字命中 5 种以上运营噪声词 → 也进入提炼。
2. 提炼时从正文抽「节目名 + 演出场馆 + 日期 + 演出类型 + 事件」，能凑齐节目与场馆
   就重写成一句话简讯（body 清空）；凑不齐（纯通知/纯票务提示）直接拒录。

设计原则：宁可漏提炼（保留原文），不可误杀真正的演出报道与产业新闻。
"""

import argparse
import json
import os
import re
import sys

# 标题里出现即判定为运营稿（强特征）
OPS_TITLE_PAT = re.compile(
    r"观演攻略|观演指南|观演须知|观演提示|购票须知|购票指南|取票指南|检票须知|"
    r"座位图|卡司变更|演员变更|变更公告|退票规则|退票须知|票根|盖章|"
    r"福利领取|领取福利|入场须知|观众须知|温馨提示|观演提醒",
    re.I)

# 正文运营噪声词：命中种类多，说明正文主体是票务/现场提示，而非新闻报道
OPS_BODY_WORDS = (
    "票根", "盖章", "印章", "打卡", "小红书", "扫码", "二维码", "满意度问卷",
    "关注公众号", "领取", "福利", "取票机", "取票", "纸质票", "检票", "座位号",
    "对号入座", "严禁跨层", "早鸟", "折后", "票价信息", "购票链接", "购票须知",
    "退票", "入场", "观众席", "实名", "限购",
)
OPS_BODY_MIN_KINDS = 5
OPS_BODY_SCAN_LEN = 800

PROGRAM_PAT = re.compile(r"《([^》]{2,40})》")
VENUE_PAT = re.compile(
    r"([\u4e00-\u9fff]{2,14}(?:大戏院|大剧院|剧院|剧场|音乐厅|艺术中心|音乐广场|"
    r"体育馆|体育场|文化中心|演艺中心|Livehouse|livehouse|俱乐部))")
DATE_PAT = re.compile(
    r"(?:20\d{2}\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*"
    r"(上午|下午|晚间|晚上|中午)?\s*(\d{1,2}[:：]\d{2})?")
YEAR_PAT = re.compile(r"(20\d{2})\s*年")

TYPE_PAT = re.compile(
    r"音乐剧|音乐节|音乐会|演唱会|交响乐?|歌剧|民族歌剧|戏曲|昆曲|京剧|"
    r"民乐|爵士|摇滚|说唱|合唱|独唱|声乐|舞剧", re.I)

# 卡司变更：抽取「某角色的演员由 A 变更为 B」
CAST_PAT = re.compile(
    r"[“\"']?([^“”\"'\n]{1,12})[”\"']?\s*(?:一角|角色)?\s*的?演员由\s*"
    r"([^\n，,。；;]{1,12})[\n，,。；;]?\s*变更为\s*([^\n，,。；;\s]{1,12})")

# ── 噪声清理（2026-11-19 用户二次投诉：「关注xxx」「油价xxx」这类没用信息不要）──
# 公众号/媒体稿的正文常混入：关注引流（点击关注视频号/设为星标/回复【免费】）、
# 无关便民信息（油价调整/限行）、采编署名（编辑/审稿/监制）、跨推区（此前报道/精彩推荐）、
# 票务模板话术（不退不换/每人一券/敬请留意）。这些行直接丢弃，保留新闻事实。

# 整行丢弃（行内任意位置命中）——内置基础规则
BASE_NOISE_LINE_PATS = [re.compile(p) for p in (
    r"点击(关注|蓝字|领取|查看|进入|阅读|这里|下方)",
    r"(扫码|长按识别|识别二维码|识别小程序)(关注|领取|加入|进群)?",
    r"(搜索|微信搜索|请)?关注\s*[“\"']?[\u4e00-\u9fff]{2,10}[”\"']?\s*"
    r"(视频号|抖音号?|公众号|微博|小红书|官微|平台)",
    r"关注.{0,8}(视频号|抖音号?|公众号|微博|小红书|官微|平台)",
    r"设为星标|没有星标|星标.{0,12}(公众号|推文)|第一时间看不到",
    r"回复\s*[【\[【]",
    r"获取.{0,12}(汇总|资料|入口|信息|攻略|名单)",
    r"小编(提醒|说|会|将持续|将持续关注)",
    r"带你玩转|遇见最美的自己|准时收看|提前关注|索票|免费领取",
    r"油价|成品油|汽油|柴油",
    r"^(?:来源|编辑|审稿|监制|责编|校对|校审|出品|图源|摄影|图片|视觉)\s*[：:｜|、]",
    r"编辑\s*[、，]\s*校审",
    r"戳(这里|链接|下方)",
    r"不退不换|每人一券|一人一券|1\.2米|请谨慎选购|敬请留意|微醺专座|"
    r"视觉受限|可能存在遮挡|每座附赠|凭票领取|剧照为",
    # 独字/短词噪声行（公众号排版把"点击/蓝字/关注/回复/【】"拆成单行）
    r"^(?:点击|蓝字|关注|回复|获取|免费|扫码|长按|搜索|这里|更多|更多消息|小提示|温馨提示|"
    r"立即|报名|入口|详情|下图|上图|如下|请关注)$",
    r"^[【】\[\]•·●｜|\-—－~～!！。.、,，:：;；*＊↓↑→←]+$",
    r"^<?\s*Enjoy the music\s*>?$",
    # 平台名单独成行（"XX视频号"/"XX公众号"）
    r"^[“\"']?[\u4e00-\u9fff]{2,10}[”\"']?\s*(?:视频号|抖音号?|快手号?|公众号|微博|官微|小红书)$",
    # WordPress 文章末尾样板句："The post XXX appeared first on SITE."（含被截断的 "The post Th"）
    # 实测 id815 `<em></em>` 乱码、id871 "帖子Th" 残片都来自这里，整行丢弃。
    r"^The post\b",
    r"appeared first on\b",
    r"^帖子\s*T?h?e?\s*$",
)]

# 命中即从该行截断到结尾（跨推区/运营尾巴从这里开始）——内置基础规则
BASE_TAIL_CUT_PAT = re.compile(
    r"^(?:此\s*前\s*报\s*道|精彩推荐\s*[：:]?|猜你喜欢|往期(?:回顾|精彩)|索票方式|"
    r"加入我们|扫码入群|点击阅读原文|长按扫码关注|具体操作如下图)\s*[：:！!…。.]*$")

# ── 可增长规则库（noise_patterns.json）──
# 每分钟本地自检（local_tidy.py）累计候选短语，每小时大模型对接复核后晋升到
# noise_phrases / tail_markers。本模块启动与每次写库前调用 reload_patterns() 热加载。
PATTERNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "noise_patterns.json")

NOISE_LINE_PATS = list(BASE_NOISE_LINE_PATS)   # 基础 + 外部规则（合并后）
TAIL_CUT_PATS = [BASE_TAIL_CUT_PAT]            # 基础 + 外部截断标记（合并后）


def reload_patterns():
    """从 noise_patterns.json 热加载外部噪声规则，合并进匹配列表。"""
    global NOISE_LINE_PATS, TAIL_CUT_PATS
    data = {}
    try:
        with open(PATTERNS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    phrases = [p for p in (data.get("noise_phrases") or []) if p]
    tails = [t for t in (data.get("tail_markers") or []) if t]
    NOISE_LINE_PATS = list(BASE_NOISE_LINE_PATS) + [re.compile(re.escape(p)) for p in phrases]
    TAIL_CUT_PATS = [BASE_TAIL_CUT_PAT] + [
        re.compile(r"^\s*" + re.escape(t)) for t in tails]
    return len(phrases), len(tails)


reload_patterns()


# ── 学习规则库（learning_rules.json）──
# 由大模型从客户纠错中提炼，反哺写库前的术语纠正/分类线索。热加载，与 noise_patterns 同构。
LEARNING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learning_rules.json")
_TERM_FIXES = []       # [(re.compile, replacement), ...]
_CATEGORY_HINTS = []   # [(re.compile, category), ...]
_SUMMARY_PLAYBOOK = [] # 文字范式，供 agent 总结/归类时遵循


def reload_learning():
    global _TERM_FIXES, _CATEGORY_HINTS, _SUMMARY_PLAYBOOK
    data = {}
    try:
        with open(LEARNING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    fixes, hints = [], []
    for it in (data.get("term_fixes") or []):
        w = (it.get("wrong") or "").strip()
        if not w:
            continue
        try:
            fixes.append((re.compile(re.escape(w), re.I), it.get("right") or ""))
        except re.error:
            pass
    for it in (data.get("category_hints") or []):
        pat = (it.get("pattern") or "").strip()
        cat = (it.get("category") or "").strip()
        if not pat or not cat:
            continue
        try:
            hints.append((re.compile(pat, re.I), cat))
        except re.error:
            pass
    _TERM_FIXES = fixes
    _CATEGORY_HINTS = hints
    _SUMMARY_PLAYBOOK = [s for s in (data.get("summary_playbook") or []) if s]
    return len(fixes), len(hints)


def apply_term_fixes(text):
    """把客户纠错学到的术语纠正应用到中文文本（写库前调用）。"""
    if not text:
        return text
    reload_learning()
    for pat, rep in _TERM_FIXES:
        text = pat.sub(rep, text)
    return text


def learned_category(title, default=""):
    """用学到的分类线索修正分类；无命中返回 default。"""
    reload_learning()
    t = title or ""
    for pat, cat in _CATEGORY_HINTS:
        if pat.search(t):
            return cat
    return default


reload_learning()


def scrub(text):
    """清掉关注引流/无关便民信息/署名/跨推区，返回干净文本（保留新闻事实）。"""
    if not text:
        return ""
    reload_patterns()   # 热加载可增长规则库（每小时对接后即时生效）
    out = []
    for ln in re.split(r"[\r\n]+", text):
        s = ln.strip()
        # 先去掉 WordPress/RSS 残留的行内 HTML 标签（<em></em>、<a …>…</a> 等），
        # 否则标签会混进译文变成 "的消<em></em>息" 这类乱码（实测 id815）。
        if s:
            s = re.sub(r"</?[A-Za-z][^>]*>", "", s).strip()
        if not s:
            continue
        if any(p.match(s) for p in TAIL_CUT_PATS):
            break
        if any(p.search(s) for p in NOISE_LINE_PATS):
            continue
        out.append(s)
    return apply_term_fixes("\n".join(out))


# 场次描述
SESSIONS_PAT = re.compile(r"午晚两场|上下午两场|两场|三场|午场|晚场|下午场|上午场|加场")
# 演出事件动词（用于判断是预告还是已发生）
PAST_PAT = re.compile(r"成功开演|圆满|落幕|收官|已演|刚刚结束", re.I)

CITIES = (
    "北京", "上海", "广州", "深圳", "成都", "重庆", "杭州", "南京", "武汉",
    "长沙", "西安", "苏州", "青岛", "厦门", "昆明", "贵阳", "福州", "济南",
    "天津", "郑州", "沈阳", "哈尔滨", "长春", "大连", "宁波", "无锡", "合肥",
    "南昌", "太原", "石家庄", "兰州", "南宁", "海口", "珠海", "佛山", "东莞",
    "中山", "惠州", "汕头", "洛阳", "柳州", "岳阳", "常州", "临沂", "湛江",
    "香港", "澳门", "台北", "高雄", "台中",
)


def _program(title, body):
    """取节目名：标题优先（标题里的《》最可能是节目），其次正文首段。"""
    for text in (title, body[:400]):
        m = PROGRAM_PAT.search(text or "")
        if m:
            return m.group(1).strip()
    return ""


def _venue(title, body, source=""):
    def clean(v):
        # 正则会贪婪吞掉前面的介词与日期残片：
        #   「即将在海珠大戏院」→「海珠大戏院」
        #   「11月21日在海珠大戏院」→「日在海珠大戏院」→「海珠大戏院」
        v = re.sub(r"^(?:即将|将于|将在|在|于|到|前往|登陆|亮相|走进)+", "", v)
        v = re.sub(r"^[\d年月日号上午下午晚间:：\s]+", "", v)
        return re.sub(r"^(?:在|于|的|位于)+", "", v).strip()

    for text in (body[:800], title, source):
        m = VENUE_PAT.search(text or "")
        if m:
            return clean(m.group(1))
    return ""


def _date(body):
    m = DATE_PAT.search(body[:500] or "")
    if not m:
        return "", ""
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return "", ""
    period = m.group(3) or ""
    year_m = YEAR_PAT.search((body[:500] or "")[max(0, m.start() - 12):m.end()])
    year = year_m.group(1) if year_m else ""
    if year:
        return "%s年%d月%d日%s" % (year, month, day, period), period
    return "%d月%d日%s" % (month, day, period), period


def _type(title, body):
    m = TYPE_PAT.search((title or "") + " " + (body[:300] or ""))
    return m.group(0) if m else ""


def _city(body, source):
    text = (body[:1200] or "") + " " + (source or "")
    for c in CITIES:
        if c in text:
            return c
    return ""


def _sessions(body):
    m = SESSIONS_PAT.search(body[:800] or "")
    return m.group(0) if m else ""


def analyze(title, body, source=""):
    """判定并提炼。

    返回 dict：
      action  'keep'（原文保留） / 'rewrite'（重写为一句话） / 'reject'（拒录）
      title   重写后的标题（action=rewrite）
      summary 一句话摘要
      reason  判定说明（写进日志）
    """
    title = (title or "").strip()
    body = (body or "").strip()
    head = body[:OPS_BODY_SCAN_LEN]

    title_hit = bool(OPS_TITLE_PAT.search(title))
    noise_kinds = sum(1 for w in OPS_BODY_WORDS if w in head)
    if not title_hit and noise_kinds < OPS_BODY_MIN_KINDS:
        return {"action": "keep", "reason": ""}

    program = _program(title, body)
    venue = _venue(title, body, source)
    date_str, period = _date(body)
    show_type = _type(title, body)
    city = _city(body, source)
    sessions = _sessions(body)
    cast = CAST_PAT.search(body[:800] or "")

    # 缺节目名：不是一条可成立的演出资讯，纯运营通知，拒录。
    # 场馆允许缺省（如巡演多站通知），此时用城市或来源兜底，不再硬拒。
    if not program:
        return {"action": "reject",
                "reason": "运营稿无演出事实（缺节目名）"}

    if venue:
        where = (city + venue) if city and not venue.startswith(city) else venue
    else:
        where = city or source or "当地剧院"
    subject = ("%s《%s》" % (show_type, program)) if show_type else "《%s》" % program

    prefix = (date_str + "，") if date_str else ""
    if cast:
        when = date_str + ("场" if period and not date_str.endswith("场") else "")
        when = (when + "，") if when else ""
        sentence = "%s%s%s角色演员由%s变更为%s" % (
            when, subject, cast.group(1), cast.group(2), cast.group(3))
    elif PAST_PAT.search(body[:200]):
        sentence = "%s%s在%s成功开演%s" % (prefix, subject, where, sessions)
    else:
        sentence = "%s%s将在%s上演%s" % (prefix, subject, where, sessions)

    sentence = re.sub(r"\s+", "", sentence).strip("，。 ")
    return {"action": "rewrite",
            "title": sentence[:100],
            "summary": sentence[:200],
            "reason": "运营稿提炼（标题命中=%s，正文噪声词=%d种）" % (title_hit, noise_kinds)}


# ── 简讯优先（2026-11-19 用户要求：重点一句话讲透，只有热点才多内容）──
# 热点 = featured 编辑推荐 / 外部热度高 / 标题含重大事件词 / 报告与音乐史（默认保留正文与数字）。
HOT_TITLE_PAT = re.compile(
    r"官宣|首演|首唱|破纪录|售罄|加场|定档|开唱|巡演|回归|重组|解散|复出|加盟|签约|"
    r"去世|逝世|获奖|夺冠|格莱美|金钟|新专辑|专辑发布|单曲首发|票房|破亿|世界纪录|"
    r"里程碑|创纪录|史诗|世纪|重大|独家|首发|重磅", re.I)
HOT_CATEGORIES = ("report", "history")

# 运营噪声候选词：用于 local_tidy 学习——含这些词的独立短行没被规则库命中时计入候选
SUSPECT_TOKEN = re.compile(
    r"关注|扫码|点击|回复|领取|福利|客服|名额|报名|进群|星标|置顶|转发|点赞|收藏|"
    r"戳这里|戳链接|戳下方|戳图|速来|手慢|别错过|不见不散")


def is_hot(title, category="", featured=False, external_heat=0):
    """是否热点：热点保留多内容，非热点压成一句话。"""
    if featured:
        return True
    try:
        if external_heat and int(external_heat) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if (category or "").strip().lower() in HOT_CATEGORIES:
        return True
    return bool(HOT_TITLE_PAT.search(title or ""))


def one_liner(title, summary, body):
    """重点一句话讲透：摘要首句 > 正文首句 > 标题。"""
    for src in (summary, body):
        s = (src or "").strip()
        if not s:
            continue
        for piece in re.split(r"(?<=[。！？!?；;])", s):
            piece = piece.strip()
            if len(piece) >= 10:
                return piece[:80]
    return (title or "").strip()[:60]


def brief_check(title, summary, body, category="", featured=False, external_heat=0):
    """非热点长文压缩为一句话简讯；热点/报告/音乐史原样保留。

    返回 (changed, summary, body, is_brief, read_minutes)
      changed=False 时后面字段都是 None，表示不需要动。
    """
    if is_hot(title, category, featured, external_heat):
        return False, None, None, None, None
    body = body or ""
    summary = summary or ""
    # 已经是简讯就不动（正文不超过一句、摘要不超长）
    sents = [x for x in re.split(r"(?<=[。！？!?；;])", body) if x.strip()]
    if len(body) <= 60 and len(sents) <= 1 and len(summary) <= 80:
        return False, None, None, None, None
    sent = one_liner(title, summary, body)
    return True, sent, "", True, 1


def selftest():
    cases = [
        {
            "title": "观演攻略|音乐剧《博物馆奇遇记》，听文物在静默中的呐喊",
            "body": "欢迎你来到博物馆，辉煌的宫殿在眼前。11月21日，音乐剧《博物馆奇遇记》即将在海珠大戏院上演午晚两场。1.凭当场纸质票根，您可领取剧目纪念票根及盖章。2.关注公众号并小红书打卡，即可领取剧目小卡。3.扫码填写满意度问卷。取票机位于剧院前厅右侧售票处，座位号1-15由前厅右手边检票进入。",
            "source": "海珠大戏院",
            "want": "rewrite",
        },
        {
            "title": "卡司变更公告|音乐剧《博物馆奇遇记》广州站",
            "body": "亲爱的观众朋友们:非常感谢大家的关注与支持!因演员个人原因，经协调，音乐剧《博物馆奇遇记》广州站11月21日下午场“小善”的演员由师帅，变更为蒋婵。因更换卡司给您造成的不便，我们深表歉意。",
            "source": "海珠大戏院",
            "want": "rewrite",
        },
        {
            "title": "某剧院取票与座位须知",
            "body": "请观众凭纸质票入场，取票机位于前厅右侧。座位号按单双号检票，严禁跨层入场。",
            "source": "某剧院",
            "want": "reject",
        },
        {
            "title": "Taylor Swift 新专辑首周销量破纪录",
            "body": "Taylor Swift 的新专辑首周卖出 120 万张，刷新今年纪录。",
            "source": "Billboard",
            "want": "keep",
        },
        {
            "title": "2026中国音乐产业发展总报告：大型演唱会票房295.58亿元",
            "body": "报告显示大型演唱会票房295.58亿元同比增长13.7%，Livehouse票房突破50亿元。",
            "source": "三个皮匠报告",
            "want": "keep",
        },
    ]
    ok = True
    for i, c in enumerate(cases, 1):
        got = analyze(c["title"], c["body"], c["source"])
        good = got["action"] == c["want"]
        ok = ok and good
        print("[%s] %d. %s -> %s | %s" % (
            "OK" if good else "FAIL", i, c["title"][:28],
            got["action"], got.get("title") or got.get("reason", "")))

    # ── scrub 去噪声用例（2026-11-19 用户投诉：关注xxx/油价xxx 不要）──
    scrub_cases = [
        {
            "name": "斑马音乐节稿：油价/关注视频号/署名/此前报道全清",
            "text": "刚刚\n斑马音乐节发布重要提醒\n禁止任何形式夜排\n这个周末\n斑马音乐节将在颍泉文创园\n激情开唱\n"
                    "更多消息\n阜阳刚刚通告！本周末部分道路实施临时交通管制！\n事关斑马音乐节！阜阳市公安局温馨提示\n"
                    "此 前 报 道\n油价最新调整通知！\n阜阳刚刚通告！\n来源：斑马音乐节\n编辑：马黎\n审稿：张宏伟 刘畅\n"
                    "监制：王少喜\n点击关注“阜小旅”视频号\n带你玩转世界\n遇见最美的自己\n!",
            "want_absent": ["油价", "点击关注", "编辑：", "审稿", "监制", "此前", "带你玩转", "遇见最美"],
            "want_present": ["斑马音乐节", "颍泉文创园", "禁止任何形式夜排"],
        },
        {
            "name": "张靓颖稿：文末抖音关注/星标引导清掉，歌词与事实保留",
            "text": "《心湖》是张靓颖演唱的歌曲。\n远行的孤舟起落\n飘飘在斑斓的湖水中央\n抖音关注 阔景音乐抖音号\n"
                    "微博关注，阔景音乐官微\n小提示\n没有星标的公众号第一时间看不到推文，大家可以把【阔景音乐】设为星标哦，具体操作如下图：\n长按扫码关注",
            "want_absent": ["抖音关注", "微博关注", "星标", "长按扫码"],
            "want_present": ["张靓颖", "心湖"],
        },
        {
            "name": "南京跨年稿：点击蓝字关注/小编提醒清掉，演出信息保留",
            "text": "点击\n蓝字\n关注\n回复\n【\n免费\n】\n获取南京免费信息汇总\n王赫野WINDY巡回演唱会-南京站\n"
                    "时间：2026-12-31\n地点：南京青奥体育公园体育馆\n小编提醒：微信搜索关注【南京本地宝】公众号，"
                    "后台对话框回复【演唱会】即可获取南京演唱会最新消息、购票入口等。",
            "want_absent": ["点击", "蓝字", "小编提醒", "南京本地宝", "免费"],
            "want_present": ["王赫野", "南京青奥体育公园体育馆"],
        },
        {
            "name": "正常新闻不动",
            "text": "Taylor Swift 的新专辑首周卖出 120 万张，刷新今年纪录。",
            "want_absent": [],
            "want_present": ["Taylor Swift", "120 万"],
        },
    ]
    for i, c in enumerate(scrub_cases, 1):
        got = scrub(c["text"])
        bad = [w for w in c["want_absent"] if w in got]
        missing = [w for w in c["want_present"] if w not in got]
        good = not bad and not missing
        ok = ok and good
        print("[%s] scrub%d. %s | 残留:%s 缺失:%s" % (
            "OK" if good else "FAIL", i, c["name"][:24], bad or "无", missing or "无"))
    # ── 简讯优先用例（2026-11-19 用户要求：重点一句话讲透，热点才多内容）──
    brief_cases = [
        {
            "name": "非热点长文压成一句话",
            "kw": {"title": "某歌手出席品牌活动", "category": "gossip", "featured": False,
                   "body": "某歌手昨日出席某品牌活动，现场演唱了两首歌曲。活动持续约两小时，吸引了数百名粉丝到场。品牌方表示未来将继续合作。", "summary": ""},
            "want_changed": True,
        },
        {
            "name": "热点（官宣/巡演）保留长文",
            "kw": {"title": "周杰伦官宣2027世界巡演", "category": "cpop", "featured": False,
                   "body": "长文" * 200, "summary": ""},
            "want_changed": False,
        },
        {
            "name": "报告保留长文与数字",
            "kw": {"title": "2026中国音乐产业发展总报告发布", "category": "report", "featured": False,
                   "body": "长文" * 200, "summary": ""},
            "want_changed": False,
        },
        {
            "name": "featured 热点保留",
            "kw": {"title": "某音乐节阵容公布", "category": "cpop", "featured": True,
                   "body": "长文" * 200, "summary": ""},
            "want_changed": False,
        },
        {
            "name": "已是简讯不动",
            "kw": {"title": "某歌手发布新单曲", "category": "cpop", "featured": False,
                   "body": "", "summary": "某歌手发布新单曲。"},
            "want_changed": False,
        },
    ]
    for i, c in enumerate(brief_cases, 1):
        kw = c["kw"]
        got = brief_check(kw["title"], kw["summary"], kw["body"], kw["category"],
                          kw["featured"])
        good = got[0] == c["want_changed"]
        ok = ok and good
        print("[%s] brief%d. %s -> changed=%s | %s" % (
            "OK" if good else "FAIL", i, c["name"][:26], got[0], (got[1] or "")[:30]))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--body-file", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    body = a.body
    if a.body_file:
        with open(a.body_file, "r", encoding="utf-8") as f:
            body = f.read()
    print(analyze(a.title, body, a.source))


if __name__ == "__main__":
    sys.exit(main())
