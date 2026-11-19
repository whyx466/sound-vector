#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声向 SOUND VECTOR —— 外文稿件中文化
==================================
东南亚（泰/越/印尼/菲/马/新）与日韩欧美的源，标题摘要基本都是英文或本地语。
入库前统一翻成简体中文写进 title_cn / summary_cn，读者打开就能读，
不用等小程序端逐条现翻（现翻要耗模型额度，而且每台设备都要重翻一遍）。

实现
----
用 Google 翻译的公开 gtx 端点（无需 key，GitHub Actions 在国外可直连）：
  https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=...
返回是嵌套数组，第一段是译文，按句切分，拼起来即可。

- 中文稿不翻（汉字占比够高就跳过），避免把「TWICE 发新歌」这种半中半英的标题翻坏。
- 单条失败不影响整批：翻不出来就留空，小程序端还有一层兜底翻译。
- 译文落 out/.zh_cache.json，同一条不会重复请求。
- 本机跑需要代理（gtx 在墙内不可达）时：--proxy 7897，或设 https_proxy 环境变量。

用法
----
  python translate_zh.py --batch out/batch_xxx.json      # 就地补中文，另存 *_zh.json
  python translate_zh.py --text "NCT 127 Is Coming"      # 单条试翻
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
CACHE_PATH = os.path.join(OUT_DIR, ".zh_cache.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
OTHER_RE = re.compile(
    r"[A-Za-z\u00c0-\u024f\u0370-\u04ff\u0590-\u05ff\u0600-\u06ff"
    r"\u0e00-\u0e7f\u3040-\u30ff\uac00-\ud7af]")
HAN_MIN_RATIO = 0.2

# 已经是中文的稿子直接跳过；纯英文/本地语才翻。
def han_ratio(text):
    s = str(text or "")
    if not s:
        return 1.0
    han = len(HAN_RE.findall(s))
    counted = len(re.sub(r"\s", "", s))
    return (han / counted) if counted else 1.0


def needs_zh(text):
    s = str(text or "").strip()
    if not s:
        return False
    if not OTHER_RE.search(s):
        return False
    return han_ratio(s) < HAN_MIN_RATIO


def log(msg):
    print("[翻译] " + msg, flush=True)


def _opener(proxy):
    if not proxy:
        return urllib.request.build_opener()
    host = proxy if "://" in proxy else "http://127.0.0.1:%s" % proxy
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": host, "https": host}))


def _cache_key(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        # 只留最近 4000 条，防止文件无限膨胀
        if len(cache) > 4000:
            keys = sorted(cache, key=lambda k: cache[k].get("at", 0))
            for k in keys[:len(keys) - 4000]:
                cache.pop(k, None)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _parse_any(raw):
    """不同后端返回结构不一样，统一抠出译文。
    clients5 / gtx: [["译文","en"], ...]  或  [[["译文","原文",...], ...], null, "en"]
    mymemory:      {"responseData": {"translatedText": "..."}}
    """
    data = json.loads(raw)
    if isinstance(data, dict):
        # mymemory 出错时仍返回 200，正文是一条英文报错，必须校验状态码。
        if str(data.get("responseStatus")) not in ("200", ""):
            return ""
        rd = data.get("responseData") or {}
        return str(rd.get("translatedText") or "").strip()
    if not isinstance(data, list) or not data:
        return ""
    head = data[0]
    # clients5：[["译文","lang"], ["译文2","lang"]]
    if isinstance(head, list) and head and isinstance(head[0], str):
        return "".join(seg[0] for seg in head if isinstance(seg, list) and seg).strip()
    # gtx：[[[译文, 原文, ...], ...], null, ...]
    if isinstance(head, list) and head and isinstance(head[0], list):
        parts = []
        for seg in head:
            if isinstance(seg, list) and seg and isinstance(seg[0], str):
                parts.append(seg[0])
        return "".join(parts).strip()
    return ""


# 端点按可用性从高到低试。
# 境内节点（微信云托管）访问 Google 的 clients5/gtx 必超时（每条各耗 12s，
# 30 条就撞满写库 timeout 被强杀）——这正是此前被迫 --no-translate 的根因。
# 实测 mymemory 境内 1.6s 可达且译文可用，故境内模式把它提到首位并跳过 Google。
# GitHub 境外节点不设 FAST_MODE，仍优先 Google（质量更好），mymemory 兜底。
_FAST = os.environ.get("FAST_MODE", "").strip().lower() in ("1", "true", "yes", "on")
_GOOGLE = [
    ("clients5", "https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl={tl}&q={q}"),
    ("gtx", "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={tl}&dt=t&q={q}"),
]
_MYMEMORY = ("mymemory", "https://api.mymemory.translated.net/get?langpair=Autodetect|{tl}&q={q}")
_ENDPOINTS = ([_MYMEMORY] + _GOOGLE) if _FAST else (_GOOGLE + [_MYMEMORY])


def translate_once(text, opener, target="zh-CN"):
    q = urllib.parse.quote(text)
    errors = []
    for name, tpl in _ENDPOINTS:
        url = tpl.format(tl=target, q=q)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json,text/plain,*/*",
            })
            with opener.open(req, timeout=6 if _FAST else 12) as r:
                zh = _parse_any(r.read().decode("utf-8", "ignore"))
            # 结果必须真含汉字：某些后端会把报错当译文返回，直接用会把英文错误写进库。
            if zh and HAN_RE.search(zh):
                return zh
            errors.append(name + ":无效结果")
        except Exception as e:
            errors.append("%s:%s" % (name, str(e)[:40]))
    raise RuntimeError("; ".join(errors))


def translate_batch(texts, proxy=None, pause=0.15):
    """批量翻译，返回 {原文: 译文}。失败的单条不出现在结果里。"""
    cache = load_cache()
    opener = _opener(proxy)
    out = {}
    miss = []
    for t in texts:
        if not t:
            continue
        hit = cache.get(_cache_key(t))
        if hit and hit.get("zh"):
            out[t] = hit["zh"]
        else:
            miss.append(t)

    ok = fail = 0
    for i, t in enumerate(miss, 1):
        try:
            zh = translate_once(t, opener)
            if zh:
                cache[_cache_key(t)] = {"zh": zh, "at": time.time(), "src": t[:200]}
                out[t] = zh
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                log("单条失败：%s | %s" % (str(e)[:80], t[:40]))
        if pause:
            time.sleep(pause)
        if i % 20 == 0:
            log("进度 %d/%d" % (i, len(miss)))
            save_cache(cache)
    if miss:
        save_cache(cache)
    log("新增翻译 %d 条，失败 %d 条，缓存命中 %d 条" % (ok, fail, len(out) - ok))
    return out


def fill_rows(rows, proxy=None):
    """给 articles 行补 title_cn / summary_cn（原地改）。已有译文的行不再重复翻。"""
    todo = [r for r in rows if not r.get("title_cn") and needs_zh(r.get("title"))]
    if not todo:
        return 0
    titles = []
    for r in todo:
        if r.get("title"):
            titles.append(r["title"])
    tmap = translate_batch(titles, proxy=proxy)

    summaries = [r.get("summary") for r in todo if needs_zh(r.get("summary"))]
    smap = translate_batch(summaries, proxy=proxy) if summaries else {}

    filled = 0
    for r in todo:
        zh = tmap.get(r.get("title"))
        if zh:
            r["title_cn"] = zh[:200]
            filled += 1
        s = r.get("summary") or ""
        if s and needs_zh(s):
            sz = smap.get(s)
            if sz:
                r["summary_cn"] = sz[:400]
    log("共 %d 条外文稿件，%d 条补上中文标题" % (len(todo), filled))
    return filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None, help="批采 JSON，补完另存 *_zh.json")
    ap.add_argument("--text", default=None, help="试翻一条")
    ap.add_argument("--proxy", default=os.environ.get("https_proxy") or os.environ.get("http_proxy"))
    args = ap.parse_args()

    if args.text:
        m = translate_batch([args.text], proxy=args.proxy)
        print(m.get(args.text, ""))
        return 0

    if not args.batch:
        ap.error("需要 --batch 或 --text")
    with open(args.batch, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("articles") or []
    fill_rows(rows, proxy=args.proxy)
    out_path = args.batch.rsplit(".json", 1)[0] + "_zh.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log("已写出 %s" % os.path.basename(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
