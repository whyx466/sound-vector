#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「用户问过、但站内没有」的提问，转成下一轮采集的检索词。

问答页把命中不足的问题写进 search_queries（status='miss'）；
本模块读脱敏视图 query_gaps，抽出真正的主体词，产出可直接塞给
collect_global.source_bing() 的查询元组，形成闭环：

    用户提问 → 记 miss → 每小时读 gap → 定向检索 → 命中写库 → 用户下次能搜到

单独运行可看当前欠了哪些选题：
    python gap_queries.py --list
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request

BASE = "https://mp-api.app.workbuddy.host/.cloud/database/rest"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
STATE_DIR = os.path.join(OUT_DIR, "state")
STATE_PATH = os.path.join(STATE_DIR, "gaps.json")

# 窗口：只补最近 72 小时问过的。更早的问题用户多半已经不关心了，
# 而且一直挂着会让每小时多跑几十个检索词。
WINDOW_H = 72
# 同一条 gap 至少间隔多久才会重新检索一次（命中之后就不再管）
RESEARCH_GAP_H = 24

# 问句里没有检索价值的词，抽主体词时先剔掉
NOISE = {
    "最近", "近况", "现在", "目前", "什么", "哪些", "怎么", "如何", "为什么",
    "有没有", "是不是", "可以", "介绍", "一下", "整理", "消息", "动态", "新闻",
    "情况", "进展", "最新", "关于", "这个", "那个", "音乐", "歌手", "乐队",
    "请问", "知道", "告诉", "哪些", "怎么样", "怎样", "值得", "推荐",
    "selftest", "sdk", "test", "自检", "通道",
}

# 明显是测试/自检产生的记录，不要浪费检索配额
TEST_PAT = re.compile(r"selftest|自检|测试|(?<![a-z])test(?![a-z])|dummy|asdf", re.I)

# 元问题：问的是「你/本站」本身，不是新闻选题。
# 这类提问应由问答页本地直接答（见 assistant.js 的 META 分支），
# 放进补采队列只会拿「介绍一下你自己 音乐」去搜，纯浪费检索配额。
META_PAT = re.compile(
    r"介绍(一下)?(你|自己)|你(是|叫|基于|用|们|能|会|怎么|如何|有)|"
    r"(什么|哪个|啥)模型|模型是|谁(做|开发|创造)的|开发者|收费|免费|"
    r"怎么用|如何使用|使用说明|有什么功能|你是谁|建议箱|怎么提交|隐私",
    re.I)

# 地名 → 检索后缀。问「某地有什么动态」时，主体是地名而不是整句，
# 而且地名配「演出/音乐会」比配「音乐」更容易搜到本地演出资讯。
PLACES = {
    "澳门": ("澳门 演唱会 音乐会", "zh-CN", "zh"),
    "香港": ("香港 演唱会 音乐会", "zh-CN", "zh"),
    "台北": ("台北 演唱会 音乐祭", "zh-TW", "zh"),
    "高雄": ("高雄 演唱会 音乐祭", "zh-TW", "zh"),
    "台湾": ("台湾 演唱会 音乐祭", "zh-TW", "zh"),
    "新加坡": ("Singapore concert music", "en-SG", "en"),
    "马来西亚": ("Malaysia concert music", "en-MY", "en"),
    "吉隆坡": ("Kuala Lumpur concert", "en-MY", "en"),
    "泰国": ("Thailand concert music", "en-US", "en"),
    "曼谷": ("Bangkok concert music", "en-US", "en"),
    "越南": ("Vietnam concert music", "en-US", "en"),
    "印尼": ("Indonesia concert music", "en-US", "en"),
    "菲律宾": ("Philippines concert music", "en-US", "en"),
    "日本": ("日本 ライブ 音楽", "ja-JP", "ja"),
    "东京": ("東京 ライブ コンサート", "ja-JP", "ja"),
    "韩国": ("한국 콘서트", "ko-KR", "ko"),
    "首尔": ("서울 콘서트", "ko-KR", "ko"),
}


def load_key():
    key = os.environ.get("WB_KEY", "").strip()
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wb_key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def fetch_gaps(hours=WINDOW_H):
    """读 query_gaps 视图（只有 id/question/keyword/created_at，无任何用户身份信息）。"""
    key = load_key()
    if not key:
        return []
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
    qs = ("/query_gaps?select=id,question,keyword,created_at"
          "&created_at=gte." + since + "&order=id.desc&limit=200")
    req = urllib.request.Request(BASE + qs, headers={
        "x-wb-webapp-access-key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            rows = json.loads(r.read().decode("utf-8", "ignore") or "[]")
            return rows if isinstance(rows, list) else []
    except Exception as e:
        print("[gap] 读取失败：%s" % str(e)[:120])
        return []


def subject_of(question):
    """从问句里抽一个可用于检索的主体词。

    「BIGBANG 近况整理」→ BIGBANG
    「周杰伦最近有什么新歌」→ 周杰伦
    抽不出来就返回空串，宁可少搜一条，也不要拿整句去搜。
    """
    q = str(question or "").strip()
    if not q:
        return ""
    # 地名优先：问「澳门最近有什么动态」时主体是澳门，不能被切成「澳门有什么新」
    for place in PLACES:
        if place in q:
            return place
    # 英文/数字优先：艺人名、厂牌、榜单名多数是拉丁字母
    latin = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9&.'\-]{1,}", q)
             if w.lower() not in NOISE and len(w) >= 3]
    if latin:
        # 取最长的那个，避免 "the" / "of" 之类被留下
        latin.sort(key=len, reverse=True)
        return latin[0]
    # 中文：按标点切段，取第一段里最长且不是噪音的片段
    segs = re.split(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()《》\[\]【】\-—_/\\|]+", q)
    for seg in segs:
        cjk = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", seg)
        if len(cjk) < 2 or cjk in NOISE:
            continue
        # 去掉开头的疑问词，剩下的前 6 个字就是主体
        for w in sorted(NOISE, key=len, reverse=True):
            if cjk.startswith(w) and len(cjk) - len(w) >= 2:
                cjk = cjk[len(w):]
                break
        if len(cjk) > 6:
            cjk = cjk[:6]
        if len(cjk) >= 2:
            return cjk
    return ""


def to_query(subject):
    """主体词 → (bing query, 市场, 语言, 分类提示)。"""
    if subject in PLACES:
        # PLACES 只存三要素，hint 交给下游 guess_category 判，补 None 保持四元组格式
        q, mkt, lang = PLACES[subject]
        return (q, mkt, lang, None)
    if re.search(r"[A-Za-z]", subject):
        return (subject + " music", "en-US", "en", None)
    return (subject + " 音乐", "zh-CN", "zh", None)


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def build_gap_queries(limit=8, log=print):
    """给 collect_global 用：返回 (queries, gaps)。

    queries 是可直接 append 到 BING_QUERIES 的元组列表；
    gaps 是 [{id, question, subject}]，用于写批次文件时交代「这轮补了谁的提问」。
    """
    rows = fetch_gaps()
    if not rows:
        return [], []
    state = _load_state()
    now = time.time()
    queries, gaps = [], []
    seen_subject = {}
    for r in rows:
        q = str(r.get("question") or "")
        if TEST_PAT.search(q) or META_PAT.search(q):
            continue
        subj = subject_of(q)
        if not subj:
            continue
        key = subj.lower()
        if key in seen_subject:
            continue
        st = state.get(str(r.get("id"))) or {}
        last = float(st.get("searched_at") or 0)
        if now - last < RESEARCH_GAP_H * 3600:
            continue
        seen_subject[key] = True
        queries.append(to_query(subj))
        gaps.append({"id": r.get("id"), "question": q[:120], "subject": subj,
                     "created_at": r.get("created_at")})
        state[str(r.get("id"))] = {"searched_at": now, "subject": subj}
        if len(queries) >= limit:
            break
    _save_state(state)
    if gaps:
        log("-- 未收录提问补采 --")
        for g in gaps:
            log("   提问「%s」→ 检索「%s」" % (g["subject"], to_query(g["subject"])[0]))
    return queries, gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出欠的选题，不写状态")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="输出 JSON（供其他脚本消费）")
    args = ap.parse_args()

    if args.list:
        rows = fetch_gaps()
        print("近 %d 小时未收录提问：%d 条" % (WINDOW_H, len(rows)))
        for r in rows:
            q = str(r.get("question") or "")
            if META_PAT.search(q):
                note = "→ 元问题，由问答页本地回答，不补采"
            else:
                note = "→ 主体「%s」" % (subject_of(q) or "(抽不出主体)")
            print("  #%s  %s  %s" % (r.get("id"), q, note))
        return

    queries, gaps = build_gap_queries(args.limit)
    if args.json:
        print(json.dumps({"queries": queries, "gaps": gaps}, ensure_ascii=False))
    else:
        print("产出检索词 %d 个" % len(queries))
        for q in queries:
            print("  ", q[0])


if __name__ == "__main__":
    main()
