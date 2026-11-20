#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容纠错消费清单生成器（每小时自动化第零步-D 调用）。

小程序纠错页把更正写进 suggestions（category='内容纠错'）。注意 suggestions
表 RLS 不开放直读（anon 401），所以本脚本不自己读 suggestions，而是：
  1. 由线上 agent 先用 db_exec_sql 导出 pending 纠错为 JSON 文件（--suggestions）；
  2. 本脚本解析 content 里的「文章#N · 字段：X / 原文 / 建议更正」；
  3. 用 WB_KEY（可读 articles）按 id 读回现有内容，判断是否仍含乱码/是否已更正；
  4. 输出 out/correction_queue.json，供 agent 逐条重新抓 source_url 核实 →
     db_exec_sql(mode=write) 改 articles → UPDATE suggestions SET status。

本脚本刻意不改库（WB_KEY 无 UPDATE 权限），改库统一由线上 agent 完成。

用法：
  python correction_queue.py --suggestions out/suggestions_pending.json
  （suggestions_pending.json 是 agent 用 db_exec_sql 读出的 JSON 数组原样落盘）
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REST = "https://mp-api.app.workbuddy.host/.cloud/database/rest"
REST = os.environ.get("WB_REST_URL", DEFAULT_REST).rstrip("/")

FIELD_MAP = {"title": "title_cn", "summary": "summary_cn", "body": "body",
             "category": "category", "region": "region"}
FIELD_LABEL = {"title": "标题", "summary": "摘要", "body": "正文",
               "category": "分类", "region": "地区"}
# 分类/地区是结构化枚举，用户从白名单选值提交，不需要抓原文核对措辞；
# 线上 agent 只需确认值在合法枚举内即可 UPDATE（2026-11-21 用户点名「欧美显示中国民族音乐」后补）。
META_FIELDS = {"category", "region"}
VALID_CATEGORY = {"game", "kpop", "jpop", "cpop", "vocal", "west", "globalpop",
                  "folk", "heritage", "guofeng", "electro", "hiphop", "funk",
                  "blues", "rock", "classical", "musical", "ost", "industry",
                  "report", "history", "gossip", "greaterchina"}
VALID_REGION = {"中国大陆", "中国香港", "中国澳门", "中国台湾", "日本", "韩国", "东南亚",
                "新加坡", "马来西亚", "泰国", "印度尼西亚", "菲律宾", "越南", "欧美",
                "印度", "拉丁美洲", "非洲", "俄罗斯", "中东", "全球"}
# 明显乱码标记：命中即认为"确实需要清理"，agent 可不必等原文即可修。
GARBLE = re.compile(r"<\/?[A-Za-z][^>]*>|appeared first on|^The post\b|帖子\s*T?h?e?\s*$", re.I)


def load_key():
    for path in (
        os.path.join(HERE, ".wb_key"),
        os.path.join(os.path.dirname(HERE), ".workbuddy", ".wb_key"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except Exception:
            pass
    return os.environ.get("WB_KEY", "").strip()


KEY = load_key()


def req(method, path, body=None):
    import urllib.request
    from urllib.parse import quote
    # 查询串里可能含中文（如 category=eq.内容纠错），必须对 URL 整体做百分号编码，
    # 否则 urllib 会按 ascii 编码直接抛 "'ascii' codec can't encode characters"。
    url = quote(REST + path, safe=":/?&=%+,")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hd = {
        "x-wb-webapp-access-key": KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }
    r = urllib.request.Request(url, data=data, headers=hd, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return resp.status, json.loads(raw) if raw else None
    except Exception:
        return resp.status, raw


def parse_content(text):
    """从多行 content 解析出 article_id / field / original / suggested / note。"""
    out = {"article_id": 0, "field": "", "original": "", "suggested": "", "user_note": "", "ai_model": ""}
    head = re.search(r"文章#(\d+)", text or "")
    fld = re.search(r"字段：\s*(title|summary|body|category|region)", text or "")
    if head:
        out["article_id"] = int(head.group(1))
    if fld:
        out["field"] = fld.group(1)
    for line in (text or "").splitlines():
        if line.startswith("原文："):
            out["original"] = line.split("：", 1)[1].strip()
        elif line.startswith("建议更正："):
            out["suggested"] = line.split("：", 1)[1].strip()
        elif line.startswith("读者说明："):
            out["user_note"] = line.split("：", 1)[1].strip()
        elif line.startswith("AI 模型："):
            out["ai_model"] = line.split("：", 1)[1].strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "out", "correction_queue.json"))
    ap.add_argument("--suggestions", default=os.path.join(HERE, "out", "suggestions_pending.json"),
                    help="agent 用 db_exec_sql 导出的 pending 纠错 JSON 数组文件")
    args = ap.parse_args()

    if not KEY:
        print("!! 未找到 WB_KEY，无法读 articles")
        return 2

    # suggestions 由 agent 导出（RLS 不开放直读）；只认 category=内容纠错 且 status=pending。
    try:
        with open(args.suggestions, "r", encoding="utf-8") as f:
            raw_rows = json.load(f)
        rows = [r for r in (raw_rows or [])
                if r.get("category") == "内容纠错" and r.get("status") == "pending"]
    except FileNotFoundError:
        rows = []   # 没有导出文件 = 暂无纠错，正常跳过
    except Exception as e:
        print("!! 读 suggestions 导出文件失败：", e)
        return 3

    queue = []
    for row in rows:
        info = parse_content(row.get("content") or "")
        if not info["article_id"] or not info["field"]:
            continue  # 格式不符，留给 agent 人工看
        # 读回该文章现值，判断是否仍需处理
        try:
            _, arts = req("GET", "/articles?select=id,title,summary,title_cn,summary_cn,body,source_url,category,region"
                                "&id=eq.%d&limit=1" % info["article_id"])
        except Exception:
            arts = None
        art = (arts or [{}])[0] if isinstance(arts, list) and arts else {}
        display_col = FIELD_MAP[info["field"]]
        current = art.get(display_col) or ""
        still_garbled = bool(GARBLE.search(str(current)))
        already_fixed = bool(info["suggested"]) and str(info["suggested"]).strip() == str(current).strip()
        is_meta = info["field"] in META_FIELDS
        valid_meta = (info["suggested"] in VALID_CATEGORY) if info["field"] == "category" \
            else ((info["suggested"] in VALID_REGION) if info["field"] == "region" else True)
        queue.append({
            "suggestion_id": row.get("id"),
            "article_id": info["article_id"],
            "field": info["field"],
            "field_label": FIELD_LABEL.get(info["field"], info["field"]),
            "display_column": display_col,
            "source_url": art.get("source_url", ""),
            "current_value": str(current)[:800],
            "suggested_value": info["suggested"],
            "user_note": info["user_note"],
            "ai_model": info["ai_model"],
            "still_garbled": still_garbled,
            "already_fixed": already_fixed,
            "is_meta": is_meta,
            "valid_meta": valid_meta,
            "action": ("skip_already_fixed" if already_fixed else
                       ("reject_invalid_value" if is_meta and not valid_meta else
                        ("apply_meta" if is_meta else
                         ("quick_clean" if still_garbled else "verify_then_apply")))),
            "created_at": row.get("created_at", ""),
        })

    out_dir = os.path.dirname(args.out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "_说明": "pending 内容纠错队列。每条由线上 agent 重新抓 source_url 核实后，"
                     "用 db_exec_sql(mode=write) 改 articles 对应列，"
                     "再 UPDATE suggestions SET status='done'/'rejected'。",
            "count": len(queue),
            "items": queue,
        }, f, ensure_ascii=False, indent=2)

    # 学习样本：供线上大模型提炼 term_fixes / category_hints / summary_playbook，
    # 写进 learning_rules.json 反哺后续采集（每小时自动化第零步-H）。
    samples = [{
        "suggestion_id": q["suggestion_id"],
        "field": q["field_label"],
        "original": q["current_value"][:300],
        "corrected": q["suggested_value"][:300],
        "user_note": q["user_note"],
        "source_url": q["source_url"],
    } for q in queue if q["suggested_value"]]
    samples_path = os.path.join(out_dir, "learning_samples.json")
    with open(samples_path, "w", encoding="utf-8") as f:
        json.dump({
            "_说明": "客户纠错样本。线上 agent 每轮把这些样本连同摘要/归类实践喂给大模型，"
                     "提炼可复用规则写入 learning_rules.json（term_fixes/category_hints/summary_playbook）。"
                     "提炼原则：只提通用规则（人名译名、专有名词、分类线索、摘要范式），不要记个案整句。",
            "count": len(samples),
            "samples": samples,
        }, f, ensure_ascii=False, indent=2)

    print("纠错队列：%d 条（apply_meta=%d, quick_clean=%d, verify_then_apply=%d, skip=%d）" % (
        len(queue),
        sum(1 for q in queue if q["action"] == "apply_meta"),
        sum(1 for q in queue if q["action"] == "quick_clean"),
        sum(1 for q in queue if q["action"] == "verify_then_apply"),
        sum(1 for q in queue if q["action"] == "skip_already_fixed"),
    ))
    print("→", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
