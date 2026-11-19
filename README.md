# 声向 SOUND VECTOR — 云端采集器

每小时自动抓取全球音乐资讯与流媒体热歌榜，写入「声向」小程序的云数据库。
放在 GitHub 上跑，**电脑关机也能更新**。

仓库地址：https://github.com/whyx466/sound-vector

---

## ✅ 当前状态：已部署并跑通

| 项目 | 状态 |
|---|---|
| 代码推送 | 已完成，`main` 分支已同步 |
| 定时任务 | 已启用，每小时第 23 分钟（`cron: 23 * * * *`，错峰防拖延）自动运行 |
| Secret `WB_KEY` | 已配置 |
| Secret `GOOGLE_API_KEY` | 已配置（YouTube Data API） |
| Workflow 写权限 | 已设为 Read and write（榜单基线能回写） |
| 首次手动试跑 | 通过。写入 7 条、翻译 7 条，云库 608 → 615 条 |
| 全球扩源试跑 | 通过。11 个新源 + 14 个多语种词生效，写入 13 条 |

查看运行记录：https://github.com/whyx466/sound-vector/actions

### 采集覆盖（2026-11-19 起）

| 类别 | 数量 | 覆盖 |
|---|---|---|
| RSS 出版社源 | 41 个 | 欧美主流媒体、中港台、日韩、东南亚 8 源、**印度 3 / 巴西 1 / 欧洲本地 5 / 非洲 1 / 俄罗斯 1 / 波兰 1** |
| Bing News 关键词 | 49 个 | 英/中/日/韩/港台/东南亚 6 国 + **印地/西/葡/德/法/意/俄/土耳其语** |
| YouTube Data API | 12 组检索 | 音乐产业、新专辑、演出 |
| GDELT / Google News | 兜底 | 限流时自动降级 |

地区判定：**标题地理词 > 域名语区 > 流派兜底**。
20 个地区值：中国大陆 / 中国香港 / 中国澳门 / 中国台湾 / 日本 / 韩国 / 新加坡 / 马来西亚 / 泰国 / 印度尼西亚 / 菲律宾 / 越南 / 东南亚 / 欧美 / 印度 / 拉丁美洲 / 非洲 / 俄罗斯 / 中东 / 全球。
欧洲本地（德法意西波兰）归「欧美」；印度/巴西/墨西哥等归「印度」「拉丁美洲」。

内容过滤：非音乐拦截（含舞台剧/文学/游戏）+ 多语种音乐相关性（强词单命中即收、弱词凑两个）+ 标题质量（栏目名如「Latest Hits!」丢弃）+ publish_date 不得为未来日期 + 逐条翻译成中文。

---

## 以后改了代码，怎么上传

三种方式，任选其一。

### 方式一：让我传（最省事）

跟我说「把采集器改动传到 GitHub」，我在本机执行
`git add -A && git commit && git push` 即可。本机已记住 GitHub 凭据，不用你登录。

### 方式二：本机命令行

```bash
cd "C:\Users\47407\WorkBuddy\2026-11-18-18-56-46\github-deploy"
git add -A
git commit -m "说明这次改了什么"
git push
```

### 方式三：网页拖拽（不动命令行）

打开 https://github.com/whyx466/sound-vector/upload/main
把改过的文件拖进去，填一句说明，点 **Commit changes**。

> 注意：拖拽只适合改**已有文件**。新增文件、删除文件、改 `.github/workflows/` 这类，
> 用方式一或方式二更稳（网页拖拽不会删掉旧文件，容易留下两份）。

### 传完之后要做什么

不用做别的。下一个整点它会自己跑。想立刻验证就去 Actions 页点 **Run workflow** 手动触发一次。

---

## 首次部署步骤（已完成，留作以后换账号/重建时参考）

### 第 1 步：新建仓库

打开 https://github.com/new

- **Repository name**：`soundvector-collector`（名字随意）
- **Public** 必须选公开（私有仓库每月只有 2000 分钟，公开仓库免费不限时）
- 不要勾选 "Add a README file"
- 点 **Create repository**

### 第 2 步：上传文件

在这个页面上点 **uploading an existing file** 链接（或直接访问
`https://github.com/你的用户名/soundvector-collector/upload/main`），

把这个文件夹里的**全部内容**拖进去：

```
.github/            ← 必须一起拖，里面是定时任务配置
collector/          ← 四个 Python 脚本 + 榜单基线
.gitignore
README.md
```

> 注意：`.github` 是隐藏文件夹，Windows 资源管理器默认看不到。
> 在地址栏直接输入 `%USERPROFILE%\Desktop` 之类进入后，把「查看 → 隐藏的项目」勾上就能看到。
> 如果拖拽失败，用 GitHub Desktop 或 `git push` 也可以。

拖完在下面填一句说明，点 **Commit changes**。

### 第 3 步：添加密钥

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**

| Name | Secret |
|---|---|
| `WB_KEY` | 写入密钥（`wbpk_` 开头，别写进这里，只填进 GitHub 的 Secret 框） |

密钥从哪拿：本地文件 `采集器/.wb_key` 里的那一行，或小程序源码 `utils/cloud.js` 里的
`accessKey`。它是「只读 + 受限插入」的公开密钥，但**不要提交到仓库**——这个仓库是公开的。

可选（不配也能跑，只是少了 YouTube 视频源）：

| Name | Secret |
|---|---|
| `GOOGLE_API_KEY` | 你自己的 YouTube Data API key |

### 第 4 步：启用并试跑

1. 仓库顶部点 **Actions**
2. 如果提示 "Workflows aren't being run on this repository"，点 **I understand my workflows, go ahead and enable them**
3. 左侧选 **声向音乐资讯采集** → 右侧 **Run workflow** → 绿色按钮
4. 等 3–5 分钟，刷新页面看是否出现绿色对勾

### 第 5 步：给 Actions 写权限（很多人漏这步）

**Settings → Actions → General → 拉到最下面 Workflow permissions**

- 选 **Read and write permissions**
- 点 **Save**

不设也能跑，但「回写榜单基线」那一步会 push 失败，下一小时就没法对比榜单变化了。

跑成功后，小程序里就会看到新内容（展示层实时读云数据库，不用重新上传小程序）。

---

## GitHub 和小程序是什么关系？

**它们不互相连接，中间只有一个云数据库。**

```
GitHub Actions（每小时跑脚本）──写──▶ 云数据库 ◀──读── 小程序（用户手机）
```

- GitHub 只是「一台免费的云电脑」，负责跑 Python 脚本抓新闻。
- 脚本用访问密钥直接写云数据库的 REST 接口，**不经过 WorkBuddy 应用，也不需要你的电脑开机**。
- 小程序每次打开都从云数据库拉最新数据，**不认识 GitHub，也不用重新上传版本**。

所以「GitHub 能不能连回 WorkBuddy」这个问题不存在——两边都只跟数据库说话。
GitHub 停了，小程序照常能看，只是不再有新内容进来。

## 怎么查数据

### 1. 小程序里查（给用户用的）

顶部搜索框支持中英文关键词（会同时匹配原文标题和中文译文）、地区下拉、热度/最新排序、板块分类。

### 2. 命令行查（给你自己核账用的）

`collector/query.py`，只用标准库，不用装依赖：

```bash
python collector/query.py                      # 最近 15 条
python collector/query.py -n 50                # 最近 50 条
python collector/query.py -r 新加坡             # 某地区
python collector/query.py -s 格莱美             # 关键词（中英文都行）
python collector/query.py --since 2026-11-18   # 某天之后
python collector/query.py --stats              # 总量 / 各地区 / 各分类 / 今日新增
```

`--stats` 的输出示例：

```
总条数：578
今日（北京 2026-11-19）新增：55

按地区：
  欧美          185
  中国大陆        169
  ...
```

### 3. GitHub 里查（看每轮跑了什么）

Actions → 点某次运行 → 底部 **Summary** 会写本次抓了多少候选、产出几条榜单事件；
展开每一步还能看日志（哪条被过滤、为什么跳过）。

## 停机和排错

| 现象 | 原因 | 处理 |
|---|---|---|
| Actions 列表是空的 | `.github/workflows/collect.yml` 没传上去 | 补传该文件，注意 `.github` 是个文件夹 |
| 运行变红，日志写 `401` | Secret 名字写错或值不对 | 确认叫 `WB_KEY`，值以 `wbpk_` 开头 |
| 跑成功但小程序没新内容 | 全被判重复或非音乐 | 看日志里「跳过 N 条」，属于正常去重 |
| 突然不按时跑了 | 仓库 60 天无人工活动，GitHub 自动停了定时任务 | Actions 页面点 **Enable workflow**，之后每月手动 Run 一次最稳 |

---

## 它每小时做什么

| 步骤 | 脚本 | 作用 |
|---|---|---|
| 榜单监测 | `collect_charts.py` | 抓 Spotify（全球/美/英/日/韩/港/台）、QQ音乐、网易云、酷狗共 16 个榜单，**只在榜首易主/新歌空降/大幅跃升时**生成一条简讯，无变化不写 |
| 资讯采集 | `collect_global.py` | 20+ 音乐媒体 RSS + 25 组 Bing 关键词（演出场所/音乐剧/古典/民族/游戏音乐/全球大奖等）+ YouTube + GDELT |
| 补用户提问 | `gap_queries.py` | 读小程序里「问了但站内没有」的提问，抽出主体词（如 BIGBANG）做定向检索，每轮最多补 6 条 |
| 写库 | `cloud_writer.py` | 先去云端拉已有 source_url/标题做去重，再做「必须含音乐关键词或来自音乐垂媒」的过滤，最后逐条写入 |

## 安全边界

数据库只给了这条通道**受限插入**权限：

- ✅ 只能新增，**不能修改、不能删除**已有内容
- ✅ 不能设「编辑推荐」（`featured` 必须为 false，无法置顶）
- ✅ 发布日期不能是未来
- ✅ 分类必须在白名单内
- ✅ 抓不到音乐相关证据的稿子会被脚本拦掉

即使密钥泄露，最坏情况也只是被塞进一些普通稿件，不会破坏已有数据；随时可以在数据库里删掉策略一键关闭。

## 需要注意

- **GitHub 定时任务不保证精确**：高峰期可能延迟 5–20 分钟，这是平台特性。
- **公开仓库的 Actions 免费不限时**，但单次任务上限 6 小时，本任务通常 3–5 分钟。
- 仓库连续 60 天没有**人工活动**时，GitHub 会自动停用定时任务（机器人自己的提交通常不算数）。最稳的做法是每月进 Actions 页面手动 Run 一次。
- 想停：Actions 页面点 **Disable workflow** 即可。

## 本地调试

```bash
# 只抓榜单
python collector/collect_charts.py

# 只抓资讯
python collector/collect_global.py --limit 60 --days 2

# 只看会写什么，不真写
python collector/cloud_writer.py --dry-run
```

本地运行时密钥从 `collector/.wb_key` 读取（该文件已被 .gitignore 排除，不会上传）。

## 用户问过但没收录的内容，谁来补

小程序问答页每次判「本站暂未收录」时，会把问题写进云端的 `search_queries` 表（`status=miss`）。
每小时这一轮采集开始前，`gap_queries.py` 会读这些记录：

1. 从问句里抽主体词（「BIGBANG 最近有什么新动向」→ `BIGBANG`），去掉「最近/近况/什么」这类噪音；
2. 组成定向检索词（`BIGBANG music` / `周杰伦 音乐`），**排在所有固定关键词之前**先跑，避免被条数上限截掉；
3. 命中的稿件标记为 `gap` 来源，写库时豁免「必须含音乐关键词」那一道（否则「BIGBANG 近况」这种标题会被误杀），但仍要过非音乐拦截；
4. 检索状态记在 `collector/out/state/gaps.json`，同一条 24 小时内不重复搜。

看当前欠了哪些选题：

```bash
python collector/gap_queries.py --list
```

## 外文稿件自动译成中文

东南亚、日韩、欧美的源基本是英文或泰语/越南语/印尼语。写库前会把标题与摘要翻成简体中文，
存进 `title_cn` / `summary_cn` 两列，小程序优先显示中文、原文附在下面一行。

- 翻译在 `cloud_writer.py` 里自动执行，不需要额外配置密钥。
- 想跳过翻译（比如国外网络访问翻译接口不稳）：`python collector/cloud_writer.py --no-translate`。
- 单独补某个批采文件：`python collector/translate_zh.py --batch collector/out/batch_xxx.json`。
- 国内本机跑需要代理：`--proxy 7897`。GitHub Actions 直连即可。
