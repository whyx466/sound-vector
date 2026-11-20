#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 云托管「大脑」：服务端直连 WorkBuddy 免密钥大模型
==========================================================================
用户 2026-11-20 要求：给云托管容器接上大模型（"不能给它长个大脑吗"）。

实测确认（2026-11-20）：
  * 端点 POST {endpoint}/.cloud/llm/chat/completions（SSE 流式，非流式不支持）
  * 鉴权只需 x-wb-webapp-access-key: {publishableKey}（前端公开值，匿名可用，无需登录 token）
  * 便宜模型 glm-5.3-flash / minimax-m3.1-flash（x0.08）实测可正常产出中文

设计原则（重要）：
  1. 只做增强，不做依赖：大模型失败/超时/限流一律降级回规则引擎（机器翻译+关键词分类），
     绝不让采集主流程挂掉。云托管是 7x24 无人值守，稳定 > 智能。
  2. 全量但批量：一批新稿一次请求，要大模型返回 JSON 数组（每条含译名/摘要/分类/相关性），
     一次调用处理 N 条，省 token 也省时间。
  3. 便宜优先：模型链 glm-5.3-flash → minimax-m3.1-flash → auto，逐个降级。
  4. publishableKey 不硬编码在源码里，优先读环境变量 WB_LLM_PK，缺失则用内置公开值兜底
     （该值本就在小程序前端源码公开，非密钥）。

用法（被 cloud_writer.py 调用）：
  from llm_brain import enrich_rows
  stats = enrich_rows(kept)   # 就地改写 kept 里的 title_cn/summary_cn/category，剔除非音乐
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# publishableKey 是前端公开值，不是密钥；仍优先走环境变量便于将来轮换。
DEFAULT_PK = "wbpk_4fi278HywUucwhoWLRCk74_gReLViFF5413E2eAME6wy8ACnp7XeXLe"
ENDPOINT = os.environ.get("WB_LLM_ENDPOINT", "https://mp-api.app.workbuddy.host").rstrip("/")
PK = os.environ.get("WB_LLM_PK", DEFAULT_PK).strip()

# 便宜快速优先 → 稳一点 → 兜底自动
MODEL_CHAIN = ["glm-5.3-flash", "minimax-m3.1-flash", "auto"]
CALL_TIMEOUT = 50          # 单模型超时（秒），到点就降级
BATCH_SIZE = 8             # 一次请求最多带几条，防止输出过长被截断

# 必须与 articles 表 RLS 策略 articles_insert_collector 的 category 白名单一致。
# 注意：旧版含 "indie" 不在 RLS 白名单里，大脑判成 indie 的行写库直接撞
# 42501 new row violates row-level security policy（2026-11-21 根治）。
# 独立/另类归到 rock（RLS 认可），不要新增 RLS 没有的类目。
VALID_CATS = ("west", "gossip", "greaterchina", "kpop", "jpop", "cpop", "rock",
              "classical", "musical", "folk", "guofeng", "heritage", "game",
              "report", "history", "industry", "globalpop",
              "blues", "funk", "electro", "hiphop", "vocal", "ost")

# 2026-11-21 用户点名严重错误：NPR 爵士稿被判成 folk，小程序显示「中国民族音乐+欧美」。
# 分类名对大模型是模糊的（folk 既有中国民族音乐也有欧美民谣），必须写清语义边界，
# 并禁止按来源地硬套中国类目（jazz/blues 是美国本土音乐，但归类看流派不看地理位置）。
CATEGORY_GUIDE = (
    "分类语义（务必按音乐类型判断，不要按国家/地区硬套）："
    "jazz/爵士/蓝调/blues/R&B/soul/funk → classical 或 blues/funk；"
    "民谣/folk/folk-prog/民族民间音乐（含欧美、非洲）→ folk；"
    "中国非遗/昆曲/古琴/古筝/民乐 → heritage 或 guofeng；"
    "华语流行 → cpop；欧美流行 → west；日韩流行 → jpop/kpop；"
    "电子 → electro；嘻哈 → hiphop；声乐 → vocal；影视原声 → ost。"
    "严禁把欧美/非洲的民谣或爵士判成 guofeng/heritage 这类中国专属类目。"
)

SYSTEM_PROMPT = (
    "你是中文音乐媒体的资深编译编辑。根据提供的外文音乐资讯，逐条产出结构化中文信息。"
    "只输出 JSON 数组，不要任何解释、不要代码块标记。忠实原文，不编造事实。"
)


def _build_user_prompt(items):
    lines = ["请逐条处理下面的音乐资讯，输出等长的 JSON 数组。每条一个对象，字段：",
             "  n: 序号（整数）",
             "  title_cn: 通顺的简体中文标题（≤40字，艺人名/组合名/专辑名保留原文写法不音译）",
             "  summary_cn: 一句话中文摘要（≤90字，讲清谁+做了什么+关键细节，不写客套开场白）",
             "  category: 从这些类里选最贴切的一个：%s" % "/".join(VALID_CATS),
             "  " + CATEGORY_GUIDE,
             "  is_music: true 或 false（是否音乐相关；书榜/漫画/话剧/体育/时政等填 false）",
             "",
             "资讯清单："]
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()[:300]
        summary = (it.get("summary") or "").strip()[:300]
        src = (it.get("source") or "").strip()[:40]
        lines.append("%d. 标题：%s" % (i, title))
        if summary:
            lines.append("   摘要：%s" % summary)
        if src:
            lines.append("   来源：%s" % src)
    lines.append("")
    lines.append("只输出 JSON 数组本身，例如 [{\"n\":1,...}]。")
    return "\n".join(lines)


def _chat_stream(model, messages, timeout=CALL_TIMEOUT):
    """调用一次 SSE 流式对话，返回拼好的完整文本；失败抛异常。"""
    body = {"model": model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": False}}
    headers = {"x-wb-webapp-access-key": PK,
               "Content-Type": "application/json",
               "Accept": "text/event-stream"}
    req = urllib.request.Request(
        ENDPOINT + "/.cloud/llm/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    text = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
            except (ValueError, IndexError, KeyError, TypeError):
                continue
    return text


def _parse_json_array(text):
    """从大模型输出里稳健地抠出 JSON 数组（容忍 ```json 包裹、前后废话）。"""
    if not text:
        return []
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # 截取第一个 [ 到最后一个 ]，避免前后解释文字干扰
    a, b = s.find("["), s.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        data = json.loads(s[a:b + 1])
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _looks_chinese(s):
    """中文占比，防止模型没按要求输出中文。"""
    s = s or ""
    if not s:
        return False
    han = len(re.findall(r"[\u4e00-\u9fff]", s))
    return han / max(1, len(s)) >= 0.15


def enrich_batch(items, log=print):
    """对一小批（≤BATCH_SIZE）条目调用大模型，就地补写 title_cn/summary_cn/category；
    返回 (成功数, 剔除非音乐数)。任何异常都向上抛，由 enrich_rows 统一降级。"""
    prompt = _build_user_prompt(items)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    last_err = None
    data = []
    for model in MODEL_CHAIN:
        try:
            text = _chat_stream(model, messages)
            data = _parse_json_array(text)
            if data:
                break
            last_err = "%s 返回空/无法解析" % model
        except Exception as e:
            last_err = "%s %s" % (model, str(e)[:80])
    if not data:
        raise RuntimeError(last_err or "无可用模型")

    by_n = {}
    for d in data:
        try:
            n = int(d.get("n"))
        except (ValueError, TypeError, AttributeError):
            continue
        by_n[n] = d

    enriched = rejected = 0
    for i, it in enumerate(items, 1):
        d = by_n.get(i)
        if not d:
            continue  # 该条模型没给结果，保持原样（走规则引擎兜底）
        # 非音乐判定：只有模型明确说 false 才剔除，避免模型抽风误杀
        if d.get("is_music") is False:
            it["_llm_nonmusic"] = True
            rejected += 1
            continue
        tc = (d.get("title_cn") or "").strip()[:200]
        sc = (d.get("summary_cn") or "").strip()[:400]
        if _looks_chinese(tc):
            it["title_cn"] = tc
        if _looks_chinese(sc):
            it["summary_cn"] = sc
        cat = (d.get("category") or "").strip().lower()
        if cat in VALID_CATS:
            it["category"] = cat
        it["_llm_done"] = True
        enriched += 1
    return enriched, rejected


def enrich_rows(rows, log=print):
    """全量智能入口：就地增强 rows。失败整批降级，绝不中断写库。

    返回统计 dict：{enriched, nonmusic, batches, model, error}。
    """
    stats = {"enriched": 0, "nonmusic": 0, "batches": 0, "model": MODEL_CHAIN[0], "error": ""}
    if not rows:
        return stats
    try:
        for start in range(0, len(rows), BATCH_SIZE):
            chunk = rows[start:start + BATCH_SIZE]
            e, r = enrich_batch(chunk, log=log)
            stats["enriched"] += e
            stats["nonmusic"] += r
            stats["batches"] += 1
        log("  [大脑] 智能处理 %d 条（译名/摘要/分类），剔除非音乐 %d 条"
            % (stats["enriched"], stats["nonmusic"]))
    except Exception as e:
        stats["error"] = str(e)[:120]
        log("  [大脑] 大模型不可用，降级规则引擎：%s" % stats["error"])
        # 清掉可能写了一半的标记，保持数据一致
        for r in rows:
            r.pop("_llm_done", None)
            r.pop("_llm_nonmusic", None)
    return stats


if __name__ == "__main__":
    # 自测：两条样例
    demo = [
        {"title": "Billboard: Grammy 2026 nominations revealed", "summary": "The Recording Academy announced this year's nominees.", "category": "west"},
        {"title": "SPY x FAMILY Vol.18 tops book chart", "summary": "Manga sales ranking for this week.", "category": "west"},
    ]
    st = enrich_rows(demo)
    print("stats:", st)
    for d in demo:
        print(d)
