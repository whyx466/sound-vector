#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量稿件补中文（title_cn / summary_cn）—— 2026-11-21 用户报「没翻译」后补的批量补译器。

背景：近 4 天 969 篇里 726 篇 title_cn 为空（11-18 整批 403 篇连 summary_cn 都没），
列表里就变成英文标题 + 空摘要。采集链路的 enrich 只在入库当时跑，漏了就永远是空的，
所以要有一次能反复跑、可断点续跑的存量补齐。

翻译通道（按可靠性降级，全部免费/极便宜）：
  1. 云托管大脑 glm-5.3-flash（批量 12 条一次请求，一起翻标题+摘要，质量最好）；
  2. translate_zh 的 Google 免费端点（逐条，只做兜底）。
  Google 直连/代理实测会被 RST 限流（Tunnel connection failed），所以大脑优先。
  已中英混排的标题（汉字占比 ≥8%）不送翻，直接原文落库，避免把艺人名译坏。

产出：out/backfill_cn/batch_NN_<column>.sql（60 条一条 UPDATE），
由线上 agent 用 db_exec_sql(mode=write) 执行——WB_KEY 只有 INSERT 权限，REST PATCH 会 401。

用法：
  python backfill_cn.py --limit 900
  python backfill_cn.py --limit 900 --no-llm   # 只抄中文原文，不走任何翻译
"""
import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "github-deploy", "collector"))

from translate_zh import translate_batch, log  # 免费兜底通道

OUT_DIR = os.path.join(HERE, "out", "backfill_cn")
SELECT = "id,title,title_cn,summary,summary_cn"
CHUNK = 60
REST = (os.environ.get("WB_REST_URL")
        or "https://mp-api.app.workbuddy.host/.cloud/database/rest").rstrip("/")
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ZH_MIN_RATIO = 0.08          # 汉字占比到这个程度就算「已是中文」，不再送翻


def load_key():
    for path in (os.path.join(HERE, ".wb_key"),
                 os.path.join(os.path.dirname(HERE), ".workbuddy", ".wb_key")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except Exception:
            pass
    return os.environ.get("WB_KEY", "").strip()


KEY = load_key()


def han_ratio(s):
    s = re.sub(r"\s", "", str(s or ""))
    if not s:
        return 1.0
    return len(HAN_RE.findall(s)) / len(s)


def is_zh(s):
    return han_ratio(s) >= ZH_MIN_RATIO


def fetch(filter_expr, limit, page=500):
    rows, offset = [], 0
    while len(rows) < limit:
        n = min(page, limit - len(rows))
        url = "%s/articles?select=%s&%s&order=id.asc&limit=%d&offset=%d" % (
            REST, SELECT, filter_expr, n, offset)
        req = urllib.request.Request(url, headers={
            "x-wb-webapp-access-key": KEY, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
        if len(data) < n:
            break
        offset += n
    return rows


def collect(limit):
    by_id = {}
    for expr in ("title_cn=eq.", "summary_cn=eq."):
        got = fetch(expr, limit)
        log("%s 命中 %d 条" % (expr, len(got)))
        for r in got:
            by_id.setdefault(r["id"], r)
    return list(by_id.values())


# ---- 云托管大脑批量翻译 ------------------------------------------------------
def _brain():
    try:
        import llm_brain
        return llm_brain
    except Exception as e:
        log("大脑不可用（%s），只用免费通道" % str(e)[:60])
        return None


def llm_translate(texts, kind, batch=12):
    """texts: 去重后的原文列表 → {原文: 中文}。kind 决定长度约束（标题要短）。"""
    brain = _brain()
    if not brain or not texts:
        return {}
    limit = "标题控制在 40 字内" if kind == "title" else "摘要控制在 120 字内"
    out = {}
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        lines = ["把下面每条音乐资讯的%s译成通顺简体中文，%s。" % (
                     "标题" if kind == "title" else "摘要", limit),
                 "艺人名/组合名/专辑名/榜单名保留原文写法，不要音译；已经是中文的保持原样；",
                 "输出 JSON 数组，字段 n（序号）与 zh（译文），不要任何解释。", "", "清单："]
        for j, t in enumerate(chunk, 1):
            lines.append("%d. %s" % (j, t))
        text = ""
        for model in brain.MODEL_CHAIN:
            try:
                text = brain._chat_stream(model, [
                    {"role": "system", "content": "你是中文音乐媒体的资深编译，只输出 JSON 数组。"},
                    {"role": "user", "content": "\n".join(lines)},
                ])
                if text.strip():
                    break
            except Exception as e:
                log("翻译请求失败 %s：%s" % (model, str(e)[:60]))
                text = ""
        arr = brain._parse_json_array(text) if text else []
        for item in arr:
            try:
                n = int(item.get("n"))
                zh = str(item.get("zh") or "").strip()
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(chunk) and zh and HAN_RE.search(zh):
                out[chunk[n - 1]] = zh
        log("大脑翻译 %d/%d 条" % (len(out), len(texts)))
    return out


def google_translate(texts):
    """免费通道兜底：逐条，失败就放弃（留空，不影响其他行）。"""
    if not texts:
        return {}
    try:
        return translate_batch(texts, proxy=None, pause=0.05)
    except Exception as e:
        log("免费通道失败：%s" % str(e)[:80])
        return {}


def sql_str(s):
    return "'" + str(s or "").replace("'", "''") + "'"


def build_sql(rows, column):
    if not rows:
        return ""
    values = ", ".join("(%d, %s)" % (i, sql_str(v)) for i, v in rows)
    return ("UPDATE public.articles AS a SET %s = v.val "
            "FROM (VALUES %s) AS v(id, val) WHERE a.id = v.id;" % (column, values))


def plan(rows, field):
    """返回 (可直接抄的中文, 待翻的外文原文列表)。"""
    copy, todo = [], []
    for r in rows:
        if str(r.get(field + "_cn") or "").strip():
            continue
        src = str(r.get(field) or "").strip()
        if not src:
            continue
        if is_zh(src):
            copy.append((r["id"], src[:400 if field == "summary" else 200]))
        else:
            todo.append(src)
    return copy, sorted(set(todo))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=900)
    ap.add_argument("--no-llm", action="store_true", help="不走大脑，只抄中文原文 + Google 兜底")
    ap.add_argument("--chunk", type=int, default=150, help="每个 SQL 文件更新多少条")
    ap.add_argument("--from-plan", action="store_true", help="复用上次的 plan.json 重出 SQL，不重新翻译")
    args = ap.parse_args()

    if args.from_plan:
        path = os.path.join(OUT_DIR, "plan.json")
        with open(path, "r", encoding="utf-8") as f:
            plan_data = json.load(f)
        for f2 in os.listdir(OUT_DIR):
            if f2.endswith(".sql"):
                os.remove(os.path.join(OUT_DIR, f2))
        write_sql([tuple(x) for x in plan_data["title_cn"]],
                  [tuple(x) for x in plan_data["summary_cn"]], args.chunk)
        return 0

    if not KEY:
        print("!! 未找到 WB_KEY")
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    for f in os.listdir(OUT_DIR):
        os.remove(os.path.join(OUT_DIR, f))

    rows = collect(args.limit)
    log("合计待补 %d 条" % len(rows))

    t_copy, t_todo = plan(rows, "title")
    s_copy, s_todo = plan(rows, "summary")
    log("标题：中文直抄 %d / 待翻 %d；摘要：中文直抄 %d / 待翻 %d"
        % (len(t_copy), len(t_todo), len(s_copy), len(s_todo)))

    tmap = {} if args.no_llm else llm_translate(t_todo, "title")
    smap = {} if args.no_llm else llm_translate(s_todo, "summary")
    # 大脑漏掉的再走免费通道
    for kind, todo, got in (("title", t_todo, tmap), ("summary", s_todo, smap)):
        left = [t for t in todo if t not in got]
        if left:
            log("%s 还剩 %d 条走免费通道" % (kind, len(left)))
            got.update(google_translate(left))

    title_updates = list(t_copy) + [
        (r["id"], tmap[str(r.get("title") or "").strip()][:200])
        for r in rows
        if not str(r.get("title_cn") or "").strip()
        and str(r.get("title") or "").strip() in tmap]
    sum_updates = list(s_copy) + [
        (r["id"], smap[str(r.get("summary") or "").strip()][:400])
        for r in rows
        if not str(r.get("summary_cn") or "").strip()
        and str(r.get("summary") or "").strip() in smap]
    title_updates = sorted(set(title_updates))
    sum_updates = sorted(set(sum_updates))
    log("可写回：标题 %d 条，摘要 %d 条（仍空：标题 %d / 摘要 %d）"
        % (len(title_updates), len(sum_updates),
           len(rows) - len(title_updates), len(rows) - len(sum_updates)))

    # 计划落盘：翻译很贵，改批次大小不该重翻一遍（--from-plan 直接复用）
    with open(os.path.join(OUT_DIR, "plan.json"), "w", encoding="utf-8") as f:
        json.dump({"title_cn": title_updates, "summary_cn": sum_updates},
                  f, ensure_ascii=False)
    write_sql(title_updates, sum_updates, args.chunk)
    return 0


def write_sql(title_updates, sum_updates, chunk):
    n = 0
    for column, items in (("title_cn", title_updates), ("summary_cn", sum_updates)):
        for i in range(0, len(items), chunk):
            with open(os.path.join(OUT_DIR, "batch_%02d_%s.sql" % (n, column)),
                      "w", encoding="utf-8") as f:
                f.write(build_sql(items[i:i + chunk], column) + "\n")
            n += 1
    log("已生成 %d 个 SQL 文件到 %s（每批 %d 条）" % (n, OUT_DIR, chunk))


if __name__ == "__main__":
    sys.exit(main())
