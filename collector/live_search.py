#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 提问当场联网搜索（境内节点可用）
======================================================
用户诉求：小程序提问时能"当场联网抓最新数据"，而不是只靠大模型训练知识或站内缓存。

实测结论（2026-11-20）：
  * 云托管容器（境内节点）抓不到 Google News / GDELT / Google Translate（网络不可达/429）。
  * 但 cn.bing.com 普通网页搜索在境内可达，返回 200，能拿到 10 条带时间戳的真实结果。
  * 免密钥大模型端点支持 tool_call，但自己不执行搜索 —— 所以搜索由本模块做，大模型只做汇总。

设计：
  1. search(query)  →  用 urllib + 正则抓 cn.bing.com，提取 [{title, summary, source, date}]，零新增依赖。
  2. summarize(query, results) → 调 llm_brain 的 glm-5.3-flash，把搜索结果汇总成中文答案。
  3. answer(query) → 串联两者，失败时降级为"搜索结果直接拼接"，绝不给空。

用法（被 app.py 的 /live-search 端点调用）：
  from live_search import answer
  payload = answer("Taylor Swift 2026 演唱会最新动态")
"""
import html as _html
import json
import re
import time
import urllib.parse
import urllib.request

try:
    import llm_brain as brain
except ImportError:  # 容错：单独测试时把 collector 目录加入 path 即可
    brain = None

BING_URL = "https://cn.bing.com/search?q={q}&ensearch=0&count=10"
TOUTIAO_URL = "https://so.toutiao.com/search?keyword={q}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
FETCH_TIMEOUT = 12
MAX_RESULTS = 8


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# 口语停用词：搜索时去掉，避免"现在/最近/啊/呢"这类稀释查询
_STOP = ("现在", "目前", "最近", "请问", "帮我", "我想知道", "怎么样", "什么样",
         "一下", "告诉", "给我", "这个", "那个", "呢", "吗", "啊", "吧", "？", "?")
# 音乐兜底限定词：纯泛词查询时拼上，提高音乐相关召回
_MUSIC_HINT = "音乐 歌手 演唱会"


def boost_query(query):
    """规则层查询改写（零成本）：去口语停用词 + 泛词补音乐限定。"""
    q = (query or "").strip()
    for w in _STOP:
        q = q.replace(w, " ")
    q = re.sub(r"\s+", " ", q).strip()
    # 已经有明确音乐词就不再补，避免稀释"Taylor Swift 演唱会"这类精准查询
    music_words = ("音乐", "歌手", "歌", "演唱会", "专辑", "乐队", "组合", "song",
                   "album", "concert", "music", "Billboard", "格莱美", "打歌", "单曲")
    if not any(w in q for w in music_words):
        q = q + " " + _MUSIC_HINT
    return q or (query or "").strip()


def _fetch(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(1200000).decode("utf-8", "ignore")


def _search_toutiao(final_q, max_results, log):
    """头条搜索：中文娱乐/音乐动态质量最高，作为首选源。结果嵌在 HTML 的 JSON title 字段。"""
    try:
        body = _fetch(TOUTIAO_URL.format(q=urllib.parse.quote(final_q)))
    except Exception as e:
        if log:
            log("[联网] 头条失败：%s" % str(e)[:100])
        return []
    out = []
    seen = set()
    # 头条把结果包装成 {"title":"<em>命中词</em>...",...}（em 是 \u003c 转义）。
    # 只有带 <em> 高亮的 title 才是真正命中的搜索结果；页面导航/广告的 title 没有高亮，过滤掉。
    for raw in re.findall(r'"title":"((?:[^"\\]|\\.)*)"', body):
        try:
            decoded = json.loads('"%s"' % raw)  # \u003cem\u003e → <em>，\uXXXX → 中文
        except Exception:
            decoded = raw
        if "<em>" not in decoded:
            continue  # 无高亮 = 导航/广告/无关项
        title = _clean(decoded)
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "summary": "", "source": "头条搜索", "date": ""})
        if len(out) >= max_results:
            break
    return out


def _search_bing(final_q, max_results, log):
    """cn.bing 普通搜索：带摘要时间戳，作为兜底/补充源。"""
    try:
        body = _fetch(BING_URL.format(q=urllib.parse.quote(final_q)))
    except Exception as e:
        if log:
            log("[联网] bing 失败：%s" % str(e)[:100])
        return []
    blocks = re.split(r'<li class="b_algo"', body)[1:]
    out = []
    for b in blocks:
        b = b[:3500]
        mh = re.search(r"<h2[^>]*>(.*?)</h2>", b, re.S)
        title = _clean(mh.group(1)) if mh else ""
        if not title:
            continue
        mp = (re.search(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', b, re.S)
              or re.search(r"<p[^>]*>(.*?)</p>", b, re.S))
        summary = _clean(mp.group(1))[:240] if mp else ""
        mc = re.search(r"<cite[^>]*>(.*?)</cite>", b, re.S)
        source = _clean(mc.group(1))[:60] if mc else ""
        date = ""
        dm = re.match(r"^(\d+\s*(?:天前|小时前|分钟前)|\d{4}年\d{1,2}月\d{1,2}日|\d+\s+days? ago)", summary)
        if dm:
            date = dm.group(1)
            summary = summary[dm.end():].lstrip("· ").strip()
        out.append({"title": title, "summary": summary,
                    "source": source, "date": date})
        if len(out) >= max_results:
            break
    return out


def _query_terms(query):
    """从原始问题里抽实体词：英文/数字串 + 2字以上中文词块，用于相关性过滤。"""
    q = boost_query(query)
    terms = set(re.findall(r"[A-Za-z][A-Za-z0-9]{1,}|\d{3,}", q))
    # 中文按 2-4 字切块
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        terms.add(seg)
        for i in range(len(seg) - 1):
            terms.add(seg[i:i + 2])
    # 去掉过泛的音乐限定词本身，避免"音乐/歌手"命中一堆无关项
    for w in ("音乐", "歌手", "演唱", "专辑", "什么", "怎么", "最新", "现在",
              "全球", "世界", "最近", "动态", "险", "查询"):
        terms.discard(w)
    return {t.lower() for t in terms if t}


def search(query, max_results=MAX_RESULTS, log=print, boost=True):
    """多源联网搜索：头条优先 + bing 补充，相关性过滤 + 标题去重。返回 (results, err)。"""
    if not query or not query.strip():
        return [], "空查询"
    final_q = boost_query(query) if boost else query.strip()
    terms = _query_terms(query)
    # 两个源都抓：头条(title 质量高) + bing(带摘要和时间戳)
    toutiao = _search_toutiao(final_q, max_results * 2, log)
    bing = _search_bing(final_q, max_results * 2, log)

    def relevant(item):
        text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
        return any(t in text for t in terms) if terms else True

    merged, seen = [], set()
    for item in toutiao + bing:
        if not relevant(item):
            continue  # 剔除与查询无关的结果（备案查询/百科泛词等）
        key = re.sub(r"[\s<em>/]", "", item["title"]).lower()[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= max_results:
            break
    if not merged:
        # 相关性过滤后为空，退回未过滤结果（总比空好），仍去重
        for item in toutiao + bing:
            key = re.sub(r"[\s<em>/]", "", item["title"]).lower()[:40]
            if key and key not in seen:
                seen.add(key)
                merged.append(item)
                if len(merged) >= max_results:
                    break
    if not merged:
        return [], "所有搜索源均无有效结果（可能被拦截或改版）"
    return merged, ""


def _fallback_text(query, results):
    """大模型不可用时的降级：把搜索结果直接拼成可读文本，不给空。"""
    lines = ["以下是联网搜索到的公开资料（未能生成汇总，直接列出）："]
    for i, r in enumerate(results[:6], 1):
        date = ("（%s）" % r["date"]) if r.get("date") else ""
        lines.append("%d. %s%s" % (i, r["title"], date))
        if r.get("summary"):
            lines.append("   %s" % r["summary"][:160])
        if r.get("source"):
            lines.append("   来源：%s" % r["source"])
    return "\n".join(lines)


def summarize(query, results, log=print):
    """把搜索结果喂给 glm-5.3-flash，汇总成一段切题的中文答案。失败返回空串由降级兜底。"""
    if not brain:
        return ""
    lines = ["你是一个音乐资讯助手。请仅依据下面的【实时联网搜索结果】回答用户问题。",
             "要求：用简体中文、2-5 句、口语化；带上关键信息（时间/数字/人名）；",
             "不要编造搜索结果里没有的事实；如果资料不足以回答，就如实说公开资料有限。",
             "不要写「根据搜索结果」这类开场白，直接给答案。", "",
             "用户问题：%s" % query, "", "实时搜索结果："]
    for i, r in enumerate(results[:8], 1):
        date = ("[%s] " % r["date"]) if r.get("date") else ""
        lines.append("%d. %s%s" % (i, date, r["title"]))
        if r.get("summary"):
            lines.append("   %s" % r["summary"][:200])
        if r.get("source"):
            lines.append("   来源：%s" % r["source"])
    messages = [
        {"role": "system", "content": "你是音乐资讯助手，只输出简体中文答案，不要 Markdown 符号。"},
        {"role": "user", "content": "\n".join(lines)},
    ]
    last = ""
    for model in (brain.MODEL_CHAIN or ["glm-5.3-flash", "minimax-m3.1-flash", "auto"]):
        try:
            text = brain._chat_stream(model, messages, timeout=45)
            if text and text.strip():
                return re.sub(r"[#*`>]", "", text).strip()
            last = "%s 返回空" % model
        except Exception as e:
            last = "%s %s" % (model, str(e)[:100])
    if last and log:
        log("[联网汇总] 大模型不可用：%s，降级直接列结果" % last)
    return ""


def answer(query, log=print):
    """完整链路：联网搜索 → 大模型汇总 → 返回结构化 payload。"""
    t0 = time.time()
    results, err = search(query, log=log)
    if not results:
        return {"ok": False, "query": query, "answer": "",
                "sources": [], "error": err, "elapsed": round(time.time() - t0, 1),
                "mode": "search_failed"}
    summary = summarize(query, results, log=log)
    mode = "live_llm" if summary else "live_results"
    text = summary or _fallback_text(query, results)
    return {"ok": True, "query": query, "answer": text,
            "sources": results[:8], "error": "",
            "elapsed": round(time.time() - t0, 1), "mode": mode,
            "searched": True, "search_count": len(results)}


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "Taylor Swift 2026 演唱会最新动态"
    import json
    print(json.dumps(answer(q), ensure_ascii=False, indent=2))
