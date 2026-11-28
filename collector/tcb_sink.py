# -*- coding: utf-8 -*-
"""微信云开发写入通道（2026-11-25）

用途：WorkBuddy 云服务资源点耗尽后，把采集结果直接写进微信云开发数据库。
出口只有两条对外 HTTP 接口，不需要云服务器、不需要备案域名：
    https://api.weixin.qq.com/cgi-bin/token                    换 access_token
    https://api.weixin.qq.com/tcb/databaseadd|databasequery    读写数据库

密钥来源（按优先级）：
    1. 环境变量 TCB_SECRET
    2. 采集器/.tcb_secret 文件（只放本机，别提交）
token 缓存到 采集器/.tcb_token.json（2 小时有效），避免反复换。
"""

from datetime import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# 云开发环境（数据库/云函数都在这），不是 prod-... 那个云托管容器环境。
ENV = os.environ.get("TCB_ENV", "cloud1-d2gbl3adwca0808e3").strip()
APPID = os.environ.get("TCB_APPID", "wx1216d3578956e32f").strip()

TOKEN_CACHE = os.path.join(HERE, ".tcb_token.json")


def log(msg):
    print("[tcb] " + msg, flush=True)


def _secret():
    s = os.environ.get("TCB_SECRET", "").strip()
    if s:
        return s
    for p in (os.path.join(HERE, ".tcb_secret"),
              os.path.join(os.path.dirname(HERE), ".tcb_secret")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            continue
    return ""


def _http(url, payload=None, timeout=30, retries=3):
    """HTTP 调用，带瞬时故障重试。

    CI runner 到微信 API 偶发超时/连接重置（实测约每十几轮挂 1 次），
    旧实现一抖就整步失败、连带跳过心跳提交。这里退避重试 3 次。
    """
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last = None
    for i in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx 是请求本身有问题，重试无意义；5xx 与 429 值得重试
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        if i < retries - 1:
            time.sleep(1.5 * (i + 1))
    raise last


def token(force=False):
    """取 access_token，优先用缓存。有效期 7200 秒，提前 5 分钟换。"""
    if not force:
        try:
            with open(TOKEN_CACHE, "r", encoding="utf-8") as f:
                c = json.load(f)
            if c.get("expires_at", 0) > time.time() + 300 and c.get("env") == ENV:
                return c["access_token"]
        except Exception:
            pass

    secret = _secret()
    if not secret:
        raise RuntimeError("缺少 TCB_SECRET：设置环境变量，或写到 采集器/.tcb_secret")

    url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
           "&appid=%s&secret=%s" % (APPID, urllib.parse.quote(secret)))
    r = _http(url)
    if "access_token" not in r:
        raise RuntimeError("换 access_token 失败：%s" % json.dumps(r, ensure_ascii=False)[:300])

    tok = r["access_token"]
    try:
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            json.dump({"access_token": tok, "expires_at": time.time() + int(r.get("expires_in", 7200)),
                       "env": ENV, "secret_tail": secret[-6:]}, f)
    except Exception:
        pass
    return tok


def _call(path, payload):
    """调用云开发 HTTP API。

    errcode 里 -1/50001/45009/45011 是系统繁忙或频率限制，属于瞬时错误，
    退避重试；40001/42001 是 token 失效，换 token 重试一次。
    """
    url = "https://api.weixin.qq.com/tcb/%s?access_token=%s" % (path, urllib.parse.quote(token()))
    r = _http(url, payload)
    # 40001 = access_token 失效，换一次再试
    if r.get("errcode") in (40001, 42001):
        url = "https://api.weixin.qq.com/tcb/%s?access_token=%s" % (path, urllib.parse.quote(token(True)))
        r = _http(url, payload)
    # 瞬时错误：最多再重试 2 次
    for i in range(2):
        if r.get("errcode") not in (-1, 50001, 45009, 45011):
            break
        time.sleep(2.0 * (i + 1))
        r = _http(url, payload)
        if r.get("errcode") in (40001, 42001):
            url = "https://api.weixin.qq.com/tcb/%s?access_token=%s" % (path, urllib.parse.quote(token(True)))
            r = _http(url, payload)
    return r


def query(q):
    """执行一段 db.collection(...)... 查询语句，返回解析后的数组。

    坑（2026-11-25 实测）：HTTP API 返回的 data 字段已经是 Python list，
    且每个元素是「JSON 字符串」（不是 dict）。早期版本对 list 再跑
    json.loads 会抛异常被吞掉 → 恒返回 [] → 去重集为空 → 每轮重复入库。
    必须逐元素解析。
    """
    r = _call("databasequery", {"env": ENV, "query": q})
    if r.get("errcode"):
        raise RuntimeError("databasequery 失败：%s" % json.dumps(r, ensure_ascii=False)[:300])
    raw = r.get("data", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            try:
                out.append(json.loads(item))
            except Exception:
                continue
        elif isinstance(item, dict):
            out.append(item)
    return out


def add_batch(collection, rows):
    """批量插入。返回 (成功条数, 错误信息)。云开发单批上限 1000，这里由调用方控制。"""
    q = 'db.collection("%s").add(%s)' % (
        collection, json.dumps({"data": rows}, ensure_ascii=False))
    r = _call("databaseadd", payload={"env": ENV, "query": q})
    if r.get("errcode"):
        return 0, json.dumps(r, ensure_ascii=False)[:400]
    return len(rows), ""


def max_sort_and_id(collection="articles"):
    """取云端最大的 sort_order 与 id，新稿从这里往上接。"""
    smax = 0
    imax = 0
    try:
        rows = query('db.collection("%s").orderBy("sort_order","desc").limit(1).get()' % collection)
        if rows:
            smax = int(rows[0].get("sort_order") or 0)
    except Exception as e:
        log("读 max sort_order 失败：%s" % str(e)[:120])
    try:
        rows = query('db.collection("%s").orderBy("id","desc").limit(1).get()' % collection)
        if rows:
            imax = int(rows[0].get("id") or 0)
    except Exception as e:
        log("读 max id 失败：%s" % str(e)[:120])
    return smax, imax


def existing(limit=2000):
    """云端现有 source_url / title，用于去重。返回 (urls, titles) 两个 set。

    HTTP API 单次 limit 上限 1000（实测 limit(2000) 报 -501007），必须分页。
    """
    import re

    def norm_url(u):
        # 公众号身份在查询串（__biz/mid/idx/sn），不能剥查询串，只去 fragment。
        return (u or "").split("#")[0].strip().lower()

    def norm_title(t):
        return re.sub(r"[\s\W_]+", "", (t or "")).lower()

    PAGE = 1000
    rows = []
    skip = 0
    while skip < int(limit):
        size = min(PAGE, int(limit) - skip)
        page = query('db.collection("articles").orderBy("id","desc").skip(%d).limit(%d).get()'
                     % (skip, size))
        rows.extend(page)
        if len(page) < size:
            break
        skip += size

    urls = {norm_url(r.get("source_url")) for r in rows if r.get("source_url")}
    titles = {norm_title(r.get("title")) for r in rows if r.get("title")}
    log("云端现有 %d 条，source_url %d 个" % (len(rows), len(urls)))
    return urls, titles


def write_rows(rows, dry_run=False, batch_size=100):
    """把已清洗的行写进云开发 articles。返回 (成功, 失败)。"""
    if not rows:
        return 0, 0

    base_sort, base_id = max_sort_and_id()
    log("云端最大 sort_order=%d, id=%d，本轮待写 %d 条" % (base_sort, base_id, len(rows)))

    payloads = []
    for i, r in enumerate(rows, 1):
        p = {k: v for k, v in r.items() if not k.startswith("_")}
        new_id = base_id + i
        p["id"] = new_id
        p["sort_order"] = base_sort + i
        p["_id"] = "art_%d" % new_id
        p.setdefault("tag", "")
        p.setdefault("featured", False)
        p["title"] = (p.get("title") or "").strip()[:200]
        p["source_url"] = (p.get("source_url") or "").strip()
        # created_at = 真实入库时刻（详情页「更新于 X月X日 HH:MM」读它）。
        # 别再拿 publish_date 推：报道日当天 12:00 在上午就是「未来时间」，
        # 用户 10:31 看到「更新于 12:00」会直接判定数据出错（2026-11-28 反馈）。
        # 规则：缺省/零点/纯日期 → 当前时刻；任何未来时间 → 拉回当前时刻。
        _now = datetime.now()
        _cd = str(p.get("created_at") or "").strip()
        if len(_cd) == 10 and _cd[4] == "-" and _cd[7] == "-":
            _cd += "T12:00:00"
        if not _cd or _cd.endswith("T00:00:00"):
            _cd = _now.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            if datetime.strptime(_cd[:19], "%Y-%m-%dT%H:%M:%S") > _now:
                _cd = _now.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            _cd = _now.strftime("%Y-%m-%dT%H:%M:%S")
        p["created_at"] = _cd
        payloads.append(p)

    if dry_run:
        for p in payloads[:50]:
            log("  [dry] id=%s %s | %s" % (p["id"], p.get("category"), p["title"][:60]))
        return len(payloads), 0

    ok = fail = 0
    for i in range(0, len(payloads), batch_size):
        chunk = payloads[i:i + batch_size]
        n, err = add_batch("articles", chunk)
        if n:
            ok += n
            log("  批次 %d 写入 %d 条（累计 %d）" % (i // batch_size + 1, n, ok))
        else:
            fail += len(chunk)
            log("  批次 %d 失败：%s" % (i // batch_size + 1, err))
        time.sleep(0.2)
    return ok, fail
