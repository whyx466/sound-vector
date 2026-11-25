# -*- coding: utf-8 -*-
"""逐条翻译器（2026-11-26）：内置术语词典 + 便宜大模型 + 译后纠察。

为什么不用 translate_zh.fill_rows：它走 Google gtx，本机被限流、19 条跑 400s 未完成，
GitHub 只能 --no-translate 全跳过，结果新稿 title_cn/summary_cn 全空（2026-11-26 截图那批）。
本模块改走 WorkBuddy 大模型端点（实测 2026-11-26 仍可用，glm-5.3-flash 约 1.9s/条），
逐条翻、逐条纠、带缓存，失败宁可留空也不把英文写进 *_cn。

词典两层：
  1) term_fixes：从 learning_rules.json 读，用户纠错持续增长的规则（权威，最高优先级）。
  2) KEEP_ORIGINAL：内置音乐术语/艺人名白名单，翻译时必须保留原文写法不音译
     （来自 summary_playbook「艺人名/组合名/品牌/场馆/榜单名保留原文写法」）。

译后纠察：模型可能把 King Crimson 翻成「克里姆森国王」——词典回查发现白名单词被意译，
就把中文常见误译替换回原文，避免"词典放在内部让大模型自己纠察"落空。
"""
import os
import re
import json
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD_JS = os.path.join(HERE, '..', '音乐资讯小程序', 'utils', 'cloud.js')
RULES_JSON = os.path.join(HERE, 'learning_rules.json')
CACHE_JSON = os.path.join(HERE, 'out', 'translate_cache.json')

ENDPOINT = os.environ.get('WB_LLM_ENDPOINT', 'https://mp-api.app.workbuddy.host').rstrip('/')
CHAT_URL = ENDPOINT + '/.cloud/llm/chat/completions'
MODEL_CHAIN = ['glm-5.3-flash', 'minimax-m3.1-flash', 'auto']
TIMEOUT = 30
HAN_RE = re.compile(r'[一-龥]')

# 内置"词典"：这些词在中文稿里必须保留原文写法。用户可通过 term_fixes 继续追加。
KEEP_ORIGINAL = [
    'King Crimson', 'Cardi B', 'Offset', 'Noah Kahan', 'Billboard', 'Hot 100',
    'Rolling Stone', 'NME', 'Pitchfork', 'Grammy', 'Spotify', 'Apple Music',
    'YouTube Music', 'TikTok', 'RIAA', 'Billboard Hot Latin Songs', 'Tianyue Huayin',
    'Peter Berg', 'Rihanna', 'Carly Rae Jepsen', 'Run Away With Me', 'Luke Bryan',
    'Luke Combs', 'Rose Chamber', 'My Silhouette', 'Modern Rot', 'Stereogum',
    'K-pop', 'J-pop', 'R&B', 'Hip-Hop', 'EDM', 'Afrobeats',
]
# 常见误译 → 原文（译后纠察用）。可按需增补。
MISTRANSLATION = {
    '克里姆森国王': 'King Crimson',
    '深红国王': 'King Crimson',
    '国王深红': 'King Crimson',
}

# publishableKey 是前端公开值（写在 cloud.js 里、匿名可用），不是密钥。
# 内置兜底是为了让 GitHub Actions 部署包（没有 cloud.js 副本）也能翻译。
FALLBACK_PK = 'wbpk_4fi278HywUucwhoWLRCk74_gReLViFF5413E2eAME6wy8ACnp7XeXLe'

_pub_key = None
_rules = None
_cache = None


def log(msg):
    print('[translator] %s' % msg, flush=True)


def pub_key():
    global _pub_key
    if _pub_key:
        return _pub_key
    env = os.environ.get('WB_LLM_PK')
    if env:
        _pub_key = env
        return _pub_key
    try:
        src = open(CLOUD_JS, encoding='utf-8').read()
        m = re.search(r"publishableKey\s*:\s*['\"]([^'\"]+)['\"]", src)
        _pub_key = m.group(1) if m else FALLBACK_PK
    except Exception:
        _pub_key = FALLBACK_PK
    return _pub_key


def rules():
    global _rules
    if _rules is None:
        try:
            _rules = json.load(open(RULES_JSON, encoding='utf-8'))
        except Exception:
            _rules = {}
    return _rules


def load_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE_JSON, encoding='utf-8'))
        except Exception:
            _cache = {}
    return _cache


def save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_JSON), exist_ok=True)
        json.dump(_cache, open(CACHE_JSON, 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception as e:
        log('缓存写入失败：%s' % str(e)[:60])


def need_zh(text):
    s = str(text or '').strip()
    return bool(s) and not HAN_RE.search(s)


def _chat_stream(model, messages):
    body = json.dumps({
        'model': model,
        'messages': messages,
        'stream': True,
        'temperature': 0.2,
        'max_tokens': 400,
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(CHAT_URL, data=body, headers={
        'x-wb-webapp-access-key': pub_key(),
        'Content-Type': 'application/json',
    })
    parts = []
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read().decode('utf-8', 'ignore')
    for line in raw.splitlines():
        if not line.startswith('data:'):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == '[DONE]':
            continue
        try:
            d = json.loads(chunk)
            delta = (d.get('choices') or [{}])[0].get('delta', {})
            c = delta.get('content')
            if c:
                parts.append(c)
        except Exception:
            continue
    return ''.join(parts).strip()


def _build_prompt(kind):
    keep = '、'.join(KEEP_ORIGINAL)
    fixes = [f for f in (rules().get('term_fixes') or [])
             if f.get('wrong') and f.get('right')]
    fix_note = ''
    if fixes:
        fix_note = '\n必须遵守的术语修正：' + '；'.join(
            '%s→%s' % (f['wrong'], f['right']) for f in fixes[:20])
    if kind == 'title':
        task = '把下面的英文新闻标题翻成简体中文，只输出译文本身，不要引号、解释或前缀。'
    else:
        task = '把下面的英文新闻摘要翻成简体中文一句话，只输出译文本身，不要引号、解释或前缀。'
    return [
        {'role': 'system', 'content': (
            '你是音乐资讯翻译助手。%s'
            '\n硬规则：艺人名、组合名、品牌、榜单、场馆、作品名保留英文原文写法，不要音译或意译。'
            '\n例如 King Crimson、Billboard Hot 100、Rolling Stone 原样保留。'
            '\n保留名单：%s。%s'
            '\n只输出译文，不加任何说明。' % (task, keep, fix_note))},
        {'role': 'user', 'content': '{{TEXT}}'},
    ]


def correct_zh(zh):
    """译后纠察：词典里的词被意译时换回原文；term_fixes 强制替换。"""
    out = str(zh or '').strip()
    for wrong, right in MISTRANSLATION.items():
        if wrong in out and right not in out:
            out = out.replace(wrong, right)
    for f in (rules().get('term_fixes') or []):
        wrong = str(f.get('wrong') or '').strip()
        right = str(f.get('right') or '').strip()
        if wrong and right and wrong in out:
            out = out.replace(wrong, right)
    # 去掉模型爱加的引号包裹
    out = out.strip().strip('"“”「」\'')
    return out.strip()


def translate_text(text, kind='title'):
    """逐条翻译，返回中文；失败返回空串（绝不返回英文原文）。"""
    text = str(text or '').strip()
    if not text:
        return ''
    if not need_zh(text):
        return text[:400]
    cache = load_cache()
    ck = kind + '|' + text
    hit = cache.get(ck)
    if hit and hit.get('zh') and HAN_RE.search(hit['zh']):
        return hit['zh']
    msgs = _build_prompt(kind)
    msgs[-1]['content'] = text
    last = ''
    for model in MODEL_CHAIN:
        try:
            raw = _chat_stream(model, msgs)
            zh = correct_zh(raw)
            if zh and HAN_RE.search(zh):
                cache[ck] = {'zh': zh, 'at': time.time(), 'model': model}
                save_cache()
                return zh
            last = '%s:无中文结果' % model
        except Exception as e:
            last = '%s:%s' % (model, str(e)[:80])
    log('翻译失败[%s] %s | %s' % (kind, last, text[:50]))
    return ''


def translate_row(row, force=False):
    """原地补 row 的 title_cn / summary_cn，返回补了几条。"""
    n = 0
    for src_key, cn_key, kind, cap in (
        ('title', 'title_cn', 'title', 200),
        ('summary', 'summary_cn', 'summary', 400),
    ):
        if (not force) and str(row.get(cn_key) or '').strip():
            continue
        src = str(row.get(src_key) or '').strip()
        if not src:
            continue
        zh = translate_text(src, kind)
        if zh:
            row[cn_key] = zh[:cap]
            n += 1
    return n


def main():
    import argparse
    ap = argparse.ArgumentParser(description='逐条翻译器（词典+大模型+纠察）')
    ap.add_argument('--text', help='试翻一条（命令行为 title 类型）')
    ap.add_argument('--kind', default='title', choices=['title', 'summary'])
    ap.add_argument('--batch', help='JSON 文件（含 articles 数组或数组本身），补完另存 *_zh.json')
    args = ap.parse_args()
    if args.text:
        print(translate_text(args.text, args.kind))
        return
    if args.batch:
        data = json.load(open(args.batch, encoding='utf-8'))
        rows = data if isinstance(data, list) else data.get('articles', [])
        total = 0
        for i, r in enumerate(rows, 1):
            total += translate_row(r)
            if i % 10 == 0:
                log('进度 %d/%d' % (i, len(rows)))
        out = args.batch.replace('.json', '_zh.json')
        json.dump(data if isinstance(data, dict) else rows,
                  open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        log('补中文 %d 条，已存 %s' % (total, out))
        return
    ap.print_help()


if __name__ == '__main__':
    main()
