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
        # 正则会贪婪吞掉前面的介词，如「即将在海珠大戏院」→「海珠大戏院」
        return re.sub(r"^(?:即将|将于|将在|在|于|到|前往|登陆|亮相|走进)+", "", v).strip()

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
