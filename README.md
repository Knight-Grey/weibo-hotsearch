# 微博热搜话题评论爬虫

爬取「微博热搜榜第 1 名」等热搜话题下的微博评论，导出 CSV / JSON。

## 项目概览

一个用于**舆情素材采集**的命令行工具：从微博热搜榜出发，定位话题 → 找到相关博文 → 抓取评论区，最终产出结构化的评论语料，供内容分析与可视化使用。

| 项 | 说明 |
|---|---|
| 语言 / 依赖 | Python 3.8+，**仅依赖 `requests`** |
| 数据来源 | 微博热搜榜 / 话题搜索页 / 评论接口（均为公开可见内容） |
| 输出 | CSV + JSON，可按话题分目录 |
| 反爬处理 | 请求间隔 + 随机抖动、指数退避重试、游标防死循环、403 与登录态明确报错 |
| 质量保障 | `selftest.py` 离线自检 41 项断言，不联网、不需要 Cookie |
| 配套作品 | 采集结果的可视化分析页：[weibo-insight](https://knight-grey.github.io/portfolio/weibo-insight/index.html) |

**注意**：仓库不包含 `cookie.txt`（个人登录凭证）与 `output/`（抓取结果含真实用户信息），二者均由 `.gitignore` 排除。

## 使用前必读

这份代码是**给每个人用自己账号跑**的。作者不提供、也不接受任何共享 Cookie。

| 事项 | 说明 |
|---|---|
| **必须自备 Cookie** | 评论接口与话题搜索页都要求登录态；微博的「自动访客 Cookie」流程已失效，无法匿名绕过。 |
| **不要共用 Cookie** | 把 Cookie 发给别人、或多人共用同一个，会让账号迅速触发风控。轻则接口 403，重则封号。**每个人的 Cookie 只在自己机器上本地使用。** |
| **Cookie 只留在本地** | 写入 `cookie.txt`，已被 `.gitignore` 排除。不要用 `git add -f` 强行提交——一旦进了 git 历史就删不干净。 |
| **不要调小请求间隔** | 默认 `--delay 1.5 --jitter 0.8`，串行不并发。调小等于替别人的服务器做压力测试。 |
| **只抓公开内容** | 不抓私密微博、私信、他人隐私信息。抓到的昵称、uid、IP 属地属于个人信息，不要二次公开传播。 |
| **风险自负** | 自动化访问可能违反平台用户协议，账号受限的风险由使用者自行承担。本项目仅供个人学习与研究。 |

> 第一次配 Cookie 卡住了？先看下面「二、准备 Cookie」里的**取不到 Cookie 的三种常见情况**。

结构：

```
weibo_hotsearch/
├── __init__.py          # 让本目录可作为包导入（python -m weibo_hotsearch.cli）
├── config.py            # 配置：Cookie、限速、翻页上限、输出目录
├── scraper.py           # 核心：热搜榜 / 话题博文 / 评论抓取，限速+重试+导出
├── cli.py               # 命令行入口
├── setcookie.py         # Cookie 填入助手：粘贴即写好 cookie.txt 并联网验证
├── selftest.py          # 离线自检（不联网，41 项断言）
├── analyze.py           # 分析：把 output/ 整理成可视化用的 data.js
├── sentiment.py         # 情感打分：情感词典 + 否定词 / 程度副词规则
├── requirements.txt
├── cookie.txt.example   # Cookie 模板（含获取步骤与常见坑，可安全提交）
├── .gitignore           # 排除 cookie.txt / output/ / data/ / __pycache__
├── LICENSE              # MIT
└── output/              # 导出目录（自动创建，不提交）
```

> `analyze.py` 与 `sentiment.py` 是配套的分析脚本，也可以单独使用：
> 采集完成后运行 `python analyze.py`，它会在 `data/` 下生成 `data.js` / `data.json`，
> 供前端页面直接读取。**`data/` 与 `output/` 一样由 `.gitignore` 排除**，
> 因为它们是脚本可从原始数据再生的产物，且内含真实用户信息。

## 一、安装与自检

```powershell
git clone https://github.com/Knight-Grey/weibo-hotsearch.git
cd weibo-hotsearch
python -m pip install -r requirements.txt

python selftest.py        # 离线自检：不联网、不需要 Cookie，验证代码本身没问题
python cli.py hot --top 5 # 联网冒烟测试：热搜榜是公开接口，不需要 Cookie
```

需要 Python 3.8+，只依赖 `requests`。若 `selftest.py` 输出「通过 41 项，失败 0 项」，说明环境与代码都正常。

## 二、准备 Cookie（抓评论/搜话题必须）

微博的评论接口和搜索结果页都要求登录态：

- 匿名请求评论接口 → `ok=0 / code=100001`，或直接 `403 Forbidden`
- 匿名请求 `s.weibo.com` 搜索页 → 302 跳到 `passport.weibo.com/visitor` 访客页（返回 200 但没有内容）

> 实测：微博的「自动换访客 Cookie」流程当前不可用（`genvisitor` 返回空），所以**必须用你自己的浏览器 Cookie**，不能靠匿名绕过。

1. 浏览器登录 <https://weibo.com>
2. `F12` → **Network** → 刷新页面 → 点任意一个请求 → **Request Headers**
3. 复制 `Cookie:` 后面的整串内容
4. 用助手一键写好（**推荐**）：

```powershell
python setcookie.py
# 粘贴刚刚复制的内容，按回车，再按 Ctrl+Z 回车结束
# 助手会自动提取 SUB/SUBP、写入 cookie.txt，并立刻联网验证热搜榜 + 话题搜索 + 评论接口
```

助手能识别这些粘贴格式：完整 `Cookie:` 头、`document.cookie` 输出、只有 `SUB=xxx`、只粘 SUB 值本身、整条 curl 命令。也可以手动填：

- 复制 `cookie.txt.example` 为 `cookie.txt`，把 Cookie 粘进去
- 环境变量：`$env:WEIBO_COOKIE="SUB=..."`（当前 PowerShell 会话有效）
- 命令行：`--cookie "SUB=..."`

然后自检：

```powershell
python cli.py check                                    # 验证热搜榜接口
python cli.py check --mid 5012345678901234             # 再验证评论接口（填一条真实微博 mid）
```

期望输出包含 `OK，共 5x 条`。若提示 `[登录态错误]` 或 `[缺少 Cookie]`（退出码 2），说明 Cookie 缺失/过期/复制不完整（末尾的 `;` 不影响）。

### 取不到 Cookie 的三种常见情况

| 现象 | 原因 | 解法 |
|---|---|---|
| 显示 `Provisional headers are shown. Disable cache to see full headers` | 该请求命中了浏览器缓存，Chrome 拿不到真实请求头 | Network 面板勾上 **Disable cache**，再按 `Ctrl + F5` 强刷 |
| Request Headers 里没有 `cookie:` 这一行 | 点的是静态资源（js / css / 图片）或第三方域名的请求，这类请求不带登录态 | 点第一条 **Type = document**、domain 为 `weibo.com` 的请求 |
| 浏览器里 `document.cookie` 读不到 `SUB` | `SUB` 被设成了 HttpOnly，Console 看不到 | 用 **Application → Storage → Cookies → https://weibo.com**，在表里双击 `SUB` 的 Value 复制 |

> 更省事的做法：右键那条请求 → **Copy → Copy as cURL (bash)**，把整条命令贴给 `setcookie.py`，它会自己把 Cookie 抠出来。这能顺带避开「长字符串复制不全」这个隐形坑。

> 热搜榜是公开接口，脚本**刻意用匿名请求**拉榜单（不带 Cookie），`hot` 子命令因此永远可用。

## 三、跑起来

### 1. 爬热搜第 1 名的评论（你最初的需求）

```powershell
python cli.py comments --rank 1
```

流程：热搜榜 → 取第 1 名话题 → 搜该话题下的博文 → 逐条抓评论 → 导出到 `output/weibo_comments_<时间戳>.csv|.json`。

### 2. 更贴近真实需求的几种用法

```powershell
# 热搜前 3 名，每个话题取 5 条博文，每条博文最多翻 10 页评论，并连带楼中楼回复
python cli.py comments --rank 3 --post-limit 5 --max-pages 10 --with-replies

# 直接指定话题词（忽略榜单）
python cli.py comments --topic "两会" --post-limit 3

# 排障：打印每次搜索请求的最终 URL 与解析到的博文数
python cli.py comments --rank 1 --debug

# 已有微博 mid，只爬这条的评论，翻到底（0 = 不限页数）
python cli.py post --mid 5012345678901234 --max-pages 0 --with-replies

# 只看榜单 + 导出（默认已剔除广告位）
python cli.py hot --top 30 --export

# 只要 JSON
python cli.py comments --rank 1 --format json --tag top1
```

### 3. 全部参数

| 参数 | 作用 |
| --- | --- |
| `--rank N` | 取热搜前 N 名，默认 1（名次用真实榜位 `realpos`，广告位不计入） |
| `--topic 词` | 直接指定话题词，优先级高于 `--rank` |
| `--post-limit N` | 每个话题最多抓 N 条博文的评论，默认 3 |
| `--max-pages N` | 每条博文最多翻 N 页评论，默认 20，`0` = 不限 |
| `--with-replies` | 同时抓楼中楼回复（数据量会明显变大） |
| `--max-reply-pages N` | 每条主评论的楼中楼翻页上限，默认 3 |
| `--debug` | 打印搜索请求的最终 URL / HTTP 状态 / 解析条数（改版排障用） |
| `--delay / --jitter` | 请求间隔与随机抖动（秒），默认 1.5 / 0.8 |
| `--retries / --timeout` | 重试次数 / 超时秒数，默认 4 / 15 |
| `--proxy` | 代理，如 `http://127.0.0.1:7890` |
| `--format` | `csv` / `json` / `both`（默认 both） |
| `--outdir / --tag` | 输出目录 / 文件名后缀 |
| `--cookie` | 临时指定 Cookie；`check` 还支持 `--mid` |

## 四、输出字段

| 字段 | 含义 |
| --- | --- |
| `comment_id` | 评论 id |
| `text` | 评论正文（已去 HTML 标签与实体） |
| `created_at` | 评论时间 `YYYY-MM-DD HH:MM:SS`（北京时间） |
| `user_id` / `user_name` | 评论者 uid / 昵称 |
| `user_verified` / `user_followers` | 是否认证 / 粉丝数 |
| `likes` / `reply_count` | 点赞数 / 该评论的回复数 |
| `source` / `ip_location` | 发布来源 / IP 属地 |
| `post_id` | 所属微博 mid |
| `topic` / `topic_rank` | 话题词 / 该话题在热搜榜的名次 |
| `is_reply` | 是否楼中楼回复 |
| `root_comment_id` / `reply_to_user` | 所属主评论 / 被回复人 |

CSV 用 `utf-8-sig` 编码，Excel 双击直接打开不乱码。

## 五、频率控制与反爬

代码里已经内置以下防护，**不要**为了快而随意调小 `--delay`：

- 每次请求前保证间隔 `delay + random(0, jitter)`
- 403 / 418 / 429 / 5xx 自动指数退避重试（默认最多 4 次）
- 识别风控文案（含「频繁」/`ok=-100`）并抛出明确异常
- 单进程串行抓取，不做并发——评论接口并发极易触发验证码

如果频繁遇到 `[被限流]`：调大到 `--delay 3 --jitter 2`，换个网络出口，或登录后在浏览器里手动过一次验证码再重试。

## 六、接口与稳定性说明

| 用途 | 接口 | 是否需要登录 |
| --- | --- | --- |
| 热搜榜 | `https://weibo.com/ajax/side/hotSearch` | 否（脚本匿名请求） |
| 话题博文 | `https://s.weibo.com/weibo?q=%23话题%23` | **是**（匿名会被 302 到访客页） |
| 评论（含楼中楼） | `https://weibo.com/ajax/statuses/buildComments` | **是** |
| 话题聚合 id | `https://weibo.com/ajax/statuses/topic_band` | 是（预留，默认流程未用） |

这些都是微博 Web 端的**非公开接口**，微博可能随时改动字段或加签。真实字段实测记录（2024）：

- 热搜条目：`rank`（0 基）、`realpos`（真实榜位，**广告位为 null**）、`num`（热度）、`label_name`/`icon_desc`（标签，`商` 表示广告）、`word_scheme`（该拿去搜索的表达式，话题形如 `#词#`）；**没有** `topic_id` / `mid` 字段，所以没法从榜单直接拿到微博 mid。
- 热搜列表里混有广告位（`realpos=null`），脚本默认剔除，`--rank 1` 拿到的就是真实第 1 名。

已知限制与坑：

- 话题博文用正则解析搜索页 HTML（刻意不引入 `bs4`/`lxml`）：按卡片范围切片、兼容 `feed_list_content` 与 `feed_list_content_full`，但微博大改版仍会失效。`--debug` 可以看到实际 URL、状态码和解析条数，便于判断是 Cookie 问题还是解析问题。
- 匿名访问搜索页会被重定向到 `passport.weibo.com/visitor`（HTTP 200 但无内容）。脚本会识别这种重定向并抛「需要 Cookie」，不会静默返回 0 条。
- 评论接口无 Cookie 时直接前置拦截并提示，不会白等 4 次 403 重试（约 80 秒）。
- `buildComments` 的 `max_id` 游标分页在极端情况下可能少少量数据；本工具按 `max_id == "0"` 判定结束，并额外做了「游标不前进就停」与 `comment_id` 去重两重保护，所以 `--max-pages 0` 也不会死循环。
- 楼中楼分页的 `id` 参数必须传**主评论 id**（不是微博 mid），代码里由 `_fetch_reply_page` 处理；如果你自己改这部分，别把两者混用，否则会把主评论当成回复重复抓一遍。
- 长文微博（全文）在搜索页里只有摘要，评论抓取不受影响。

## 七、合规提醒

- 仅抓取**公开可见**内容，不要抓取私密微博、私信或他人隐私信息。
- 遵守微博《用户协议》与 `robots.txt`，控制频率，不要对目标站点造成压力。
- 抓到的用户昵称、uid、IP 属地属于个人信息，请勿公开传播或用于商业用途。
- 仅用于个人学习与研究，风险自负。
