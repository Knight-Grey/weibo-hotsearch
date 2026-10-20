"""微博热搜榜 + 话题 + 评论抓取核心模块。

只依赖 requests。所有网络请求都走 WeiboClient，统一处理：
    - 限速与随机抖动（避免触发风控）
    - 失败重试 + 指数退避
    - 登录态失效、风控拦截的明确报错

接口均为微博 Web 端公开接口：
    * 热搜榜     https://weibo.com/ajax/side/hotSearch
    * 话题聚合   https://weibo.com/ajax/statuses/topic_band
    * 话题搜索   https://s.weibo.com/weibo?q=%23话题%23
    * 评论       https://weibo.com/ajax/statuses/buildComments
"""

from __future__ import annotations

import html
import json
import random
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests

try:  # 支持 `python cli.py`（包内直接跑）和 `python -m weibo_hotsearch.cli` 两种方式
    from . import config
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]

CST = timezone(timedelta(hours=8))  # 微博服务端为北京时间

BUILD_COMMENTS = "https://weibo.com/ajax/statuses/buildComments"
HOT_SEARCH = "https://weibo.com/ajax/side/hotSearch"
TOPIC_BAND = "https://weibo.com/ajax/statuses/topic_band"
STATUS_SHOW = "https://weibo.com/ajax/statuses/show"
SEARCH_URL = "https://s.weibo.com/weibo"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\n\u3000]+")
_FROM_RE = re.compile(r"来自\s*([^\s，,。;；]+)")  # source 里的「来自 江西」
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")  # 微博正文里常夹的零宽字符
_EXPAND_TAIL_RE = re.compile(r"\s*(?:展开|展开全文|收起)\s*[cCdD]?\s*$")  # 搜索页按钮文字残留
_VIDEO_TAIL_RE = re.compile(r"\s*L?[^\s]{0,30}的微博视频\s*$")  # 视频卡片的「L昵称的微博视频」


class WeiboError(RuntimeError):
    """微博接口返回的业务错误。"""


class LoginRequired(WeiboError):
    """未登录 / 登录态失效。"""


class RateLimited(WeiboError):
    """被风控限流。"""


# ==========================================================================
# 工具函数
# ==========================================================================
def clean_text(raw: Optional[str]) -> str:
    """去掉微博返回文本里的 <a> 标签、HTML 实体、零宽字符和「展开」工具栏残留。

    2026-10 实测：搜索页的长文正文尾巴上会拖一个「展开c」之类的按钮文字，
    中间还夹着 U+200B —— 不清掉的话，正文拿去做分词/情感分析会被污染。
    """
    if not raw:
        return ""
    text = _TAG_RE.sub("", str(raw))
    text = html.unescape(text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    # 尾部可能叠着好几种 UI 残留（`…（天眼查） L青年说的微博视频 收起d`），反复剥到稳定为止
    for _ in range(4):
        before = text
        text = _EXPAND_TAIL_RE.sub("", text).strip()
        text = _VIDEO_TAIL_RE.sub("", text).strip()
        if text == before:
            break
    return text


def parse_created_at(value: Any) -> str:
    """把 'Wed Mar 06 12:00:00 +0800 2024' 归一化成北京时间 'YYYY-MM-DD HH:MM:SS'。

    带时区偏移的输入会先转换到北京时间；无法识别时原样返回。
    """
    if not value:
        return ""
    raw = str(value).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(CST)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return raw


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pick_like_count(raw: Dict[str, Any]) -> int:
    """取评论的点赞数。

    2026-10 实测：`like_count` 拿到的是 0（字段可能改名，或这个版本不返回）。
    这里按候选键依次看，取「出现过的值里最大的那个」——宁可保守，也别静默丢成 0。
    """
    if not isinstance(raw, dict):
        return 0
    keys = ("like_count", "like_counts", "likes_count", "attitudes_count", "likes")
    values = [to_int(raw.get(key)) for key in keys if raw.get(key) is not None]
    return max(values) if values else 0


def extract_ip_location(raw: Dict[str, Any]) -> str:
    """取评论的 IP 属地。

    2026-10 实测：`ip_location` 字段已经不返回了，微博把属地塞进了 `source`
    （形如「来自 江西」「来自 上海」）。所以字段优先，字段为空再从 source 里抠。
    """
    if not isinstance(raw, dict):
        return ""
    direct = clean_text(raw.get("ip_location") or "")
    if direct:
        return direct
    match = _FROM_RE.search(clean_text(raw.get("source") or ""))
    if match and len(match.group(1)) <= 8:  # 地名都很短，太长的多半是客户端名
        return match.group(1)
    return ""


def derive_xsrf(cookie: str) -> str:
    """从 Cookie 中推导 CSRF token（服务端要求原样放进 `X-XSRF-TOKEN` 请求头）。

    微博现在的 Cookie 里这个键叫 `XSRF-TOKEN`；老版本叫 `_weibo_s`。
    两个都试，否则推不出来就不带这个头 —— 而 weibo.com 的 ajax 写接口
    （buildComments 是 POST）缺少这个头会被直接 403。
    """
    if config.XSRF_TOKEN:
        return config.XSRF_TOKEN
    for name in ("XSRF-TOKEN", "X-CSRF-TOKEN", "_weibo_s"):
        match = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie or "", re.I)
        if match:
            return match.group(1).strip()
    return ""


# ==========================================================================
# 数据结构
# ==========================================================================
AD_LABELS = ("商", "荐", "推广", "大卖")  # 热搜榜里的广告位标识


@dataclass
class HotItem:
    """热搜榜条目。

    query 是**搜索表达式**：话题类词条形如 `#词#`，普通词条就是词本身，
    直接拿去搜 s.weibo.com 命中率最高。
    """

    rank: int
    word: str
    raw_hot: int
    category: str
    label: str
    query: str = ""
    is_ad: bool = False
    realpos: int = 0  # 接口给的官方榜位，可能缺失或重复，仅供参考

    @property
    def topic_url(self) -> str:
        return f"https://s.weibo.com/weibo?q={quote(self.query or self.word)}"


@dataclass
class Comment:
    """一条评论（主评论或楼中楼回复）。"""

    comment_id: str
    text: str
    created_at: str
    user_id: str
    user_name: str
    user_verified: bool
    user_followers: int
    likes: int
    reply_count: int
    source: str
    ip_location: str
    post_id: str
    topic: str
    topic_rank: int
    is_reply: bool = False
    root_comment_id: str = ""
    reply_to_user: str = ""

    def as_row(self) -> Dict[str, Any]:
        return asdict(self)


# ==========================================================================
# 客户端
# ==========================================================================
class WeiboClient:
    def __init__(
        self,
        cookie: Optional[str] = None,
        delay: Optional[float] = None,
        jitter: Optional[float] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[int] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self.cookie = (cookie if cookie is not None else config.load_cookie()).strip()
        self.delay = config.REQUEST_DELAY if delay is None else delay
        self.jitter = config.REQUEST_JITTER if jitter is None else jitter
        self.max_retries = config.MAX_RETRIES if max_retries is None else max_retries
        self.timeout = config.TIMEOUT if timeout is None else timeout

        self.session = requests.Session()
        self.session.headers.update(self._build_headers())
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        self._last_request_at = 0.0
        self.request_count = 0

    # ---------------------------------------------------------------- 底层
    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://weibo.com/",
            "X-Requested-With": "XMLHttpRequest",
            "Client-Version": "v2.46.36",
            "Connection": "keep-alive",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        xsrf = derive_xsrf(self.cookie)
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
        return headers

    def _throttle(self) -> None:
        """限速：保证两次请求间隔 >= delay，并附加随机抖动。"""
        wait = self.delay - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        if self.jitter > 0:
            time.sleep(random.uniform(0, self.jitter))

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """带重试的请求。

        kwargs 里的 ``retry_body`` 是一个「构造请求体的工厂函数」，只在重试时调用一次，
        用来避免流式/已消费的 body 在第二次尝试时失效；普通 dict body 不需要它。
        """
        body_factory = kwargs.pop("retry_body", None)
        kwargs.setdefault("timeout", self.timeout)
        max_attempts = max(1, int(self.max_retries))

        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            self._throttle()
            try:
                if attempt > 1 and body_factory is not None:
                    kwargs["data"] = body_factory()
                resp = self.session.request(method, url, **kwargs)
                self._last_request_at = time.monotonic()
                self.request_count += 1
            except requests.RequestException as exc:
                last_error = exc
                self._sleep_backoff(attempt, f"网络异常 {exc.__class__.__name__}")
                continue

            # 403 是凭证/权限层面的拒绝，退避重试没有意义。
            # 直接把服务端原话带出来，否则用户只看到「重试 4 次仍失败」，无从判断。
            if resp.status_code == 403:
                snippet = " ".join((resp.text or "").split())[:200]
                raise WeiboError(
                    f"HTTP 403 被拒绝：{url}｜服务端返回：{snippet or '<空响应体>'}"
                    "｜请确认 cookie 里含 XSRF-TOKEN、且仍是有效登录态。"
                )

            # 418 / 429 / 5xx：退避后重试（这些通常是瞬时的风控/抖动）
            if resp.status_code in (418, 429) or resp.status_code >= 500:
                last_error = RateLimited(f"HTTP {resp.status_code}")
                self._sleep_backoff(attempt, f"HTTP {resp.status_code}")
                continue

            if resp.status_code != 200:
                raise WeiboError(f"HTTP {resp.status_code} - {url}")

            return resp

        raise WeiboError(f"重试 {max_attempts} 次仍失败：{last_error} ({url})")

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        sleep_for = config.BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
        print(f"  [重试 {attempt}] {reason}，等待 {sleep_for:.1f}s")
        time.sleep(sleep_for)

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None,
                  headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        resp = self._request("GET", url, params=params, headers=headers)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WeiboError(f"响应不是合法 JSON（可能被重定向到登录页）：{resp.text[:200]}") from exc
        self._check_payload(payload)
        return payload

    def _post_json(self, url: str, data: Dict[str, Any], referer: str) -> Dict[str, Any]:
        body = dict(data)
        body.setdefault("_t", int(time.time() * 1000))
        resp = self._request(
            "POST",
            url,
            data=body,
            retry_body=lambda: dict(body),  # 每次重试都用一份新的 body
            headers={"Referer": referer, "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WeiboError(f"响应不是合法 JSON：{resp.text[:200]}") from exc
        self._check_payload(payload)
        return payload

    @staticmethod
    def _check_payload(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        ok = payload.get("ok")
        code = to_int(payload.get("code") or payload.get("error_code"), 0)
        msg = str(payload.get("msg") or payload.get("message") or "")
        lowered = msg.lower()

        if code in (100001, 21332, 21327) or (ok == 0 and "login" in lowered):
            raise LoginRequired(
                "登录态失效或未提供 Cookie。请在 config.py / cookie.txt / --cookie 中填入有效的微博 Cookie。"
            )
        if ok == -100 or code in (100002, -100) or "频繁" in msg or "frequent" in lowered:
            raise RateLimited(f"触发风控：{msg or code}。请调大 REQUEST_DELAY 或稍后再试。")
        if ok == 0 and code not in (0,):
            raise WeiboError(f"接口返回错误：code={code} msg={msg}")

    # ------------------------------------------------------------ 热搜榜
    def fetch_hot_search(self, include_ads: bool = False) -> List[HotItem]:
        """抓取实时热搜榜（对应 https://s.weibo.com/top/summary 页面）。

        真实接口字段（2024 年实测）：
            rank=0 基序号、realpos=榜上名次（广告位为 None）、num=热度、
            label_name/icon_desc=标签（"商" 等表示广告）、word_scheme=搜索表达式、
            **没有** topic_id / mid 字段。
        默认过滤掉广告位，因为它们不是真正的话题。
        """
        # 热搜榜是公开接口，不需要登录；带上 Cookie 反而会走个性化分支，这里统一匿名请求
        session_headers = dict(self.session.headers)
        for key in ("Cookie", "X-XSRF-TOKEN"):
            session_headers.pop(key, None)

        resp = self._request(
            "GET", HOT_SEARCH, params={"_t": int(time.time() * 1000)}, headers=session_headers
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WeiboError(f"热搜接口返回的不是 JSON：{resp.text[:200]}") from exc
        self._check_payload(payload)
        data = payload.get("data") or {}

        if isinstance(data, list):  # 少数情况下 data 直接是列表
            band = data
        elif isinstance(data, dict):
            band = data.get("realtime") or data.get("band_list") or []
        else:
            band = []
        if not isinstance(band, list):
            band = []

        items: List[HotItem] = []
        seq = 0
        for idx, raw in enumerate(band, start=1):
            if not isinstance(raw, dict):
                continue
            word = clean_text(raw.get("word") or raw.get("note") or "")
            if not word:
                continue

            label = clean_text(raw.get("label_name") or raw.get("icon_desc") or "")
            has_realpos = "realpos" in raw
            realpos = to_int(raw.get("realpos"), 0)
            # 新版接口用 realpos 缺失/为 0 标记广告位；老结构没有该字段，只按标签判定
            is_ad = (has_realpos and realpos <= 0) or any(mark in label for mark in AD_LABELS)
            if is_ad and not include_ads:
                continue

            # 名次统一用「非广告条目的序号」而不是 realpos：
            # 2026-10 实测 realpos 会缺失并造成重复（出现两个 3、两个 6），
            # 拿它当名次会把「前十」排乱。realpos 仍保留在字段里供对照。
            seq += 1
            scheme = clean_text(raw.get("word_scheme") or "")
            items.append(
                HotItem(
                    rank=seq,
                    word=word,
                    raw_hot=to_int(raw.get("num") or raw.get("raw_hot")),
                    category=clean_text(raw.get("category") or ""),
                    label=label,
                    query=scheme or word,
                    is_ad=is_ad,
                    realpos=realpos,
                )
            )
        return items

    def resolve_topic_id(self, word: str) -> str:
        """尝试把话题词解析成话题聚合页 id（失败返回空字符串）。"""
        try:
            payload = self._get_json(
                TOPIC_BAND,
                {"word": word, "sid": "", "_t": int(time.time() * 1000)},
            )
        except WeiboError:
            return ""
        data = payload.get("data") or {}
        for key in ("id", "topic_id", "page_id"):
            if isinstance(data, dict) and data.get(key):
                return str(data[key])
        return ""

    # ------------------------------------------------------------ 话题博文
    def fetch_topic_posts(
        self,
        topic: str,
        max_pages: int = 1,
        search_type: str = "all",
        debug: bool = False,
    ) -> List[Dict[str, Any]]:
        """抓取话题下的博文（数据源是 s.weibo.com 搜索页，服务端渲染的 HTML）。

        注意：s.weibo.com 对匿名请求会 302 到 passport.weibo.com 的访客页（返回 200 但无内容），
        所以**话题搜索需要登录态 Cookie**。检测到这种重定向会直接抛 LoginRequired，
        而不是让你对着「0 条博文」猜原因。

        debug=True 时打印每次请求的最终 URL 和解析到的条数，便于排查改版问题。
        """
        posts: List[Dict[str, Any]] = []
        query = topic if topic.startswith("#") else f"#{topic}#"
        seen_mids = set()

        for page in range(1, max_pages + 1):
            params = {
                "q": query,
                "type": search_type,
                "page": page,
                "Refer": "weibo_comprehensive",
                "tw": "hot",
            }
            resp = self._request("GET", SEARCH_URL, params=params, headers={"Referer": SEARCH_URL})
            self._dump_search_html(page, resp.text)

            final_url = str(getattr(resp, "url", "") or "")
            if "passport.weibo.com" in final_url:
                raise LoginRequired(
                    "微博把匿名请求重定向到了访客/登录页，话题搜索需要有效 Cookie。"
                    "请配置 cookie.txt 或用 --cookie 传入（见 README「二、准备 Cookie」）。"
                )

            page_posts = self._parse_search_page(resp.text, topic)
            if debug:
                print(f"    [debug] 第 {page} 页 status={resp.status_code} "
                      f"final={final_url[:80]} 解析={len(page_posts)} 条 html={len(resp.text)} 字节")
            if not page_posts:
                break

            added = 0
            for post in page_posts:
                if post["mid"] in seen_mids:
                    continue
                seen_mids.add(post["mid"])
                posts.append(post)
                added += 1
            if added == 0:
                break  # 整页都是重复内容，继续翻页没有意义
        return posts

    @staticmethod
    def _parse_search_page(html_text: str, topic: str) -> List[Dict[str, Any]]:
        """搜索页是服务端渲染的 HTML，用正则把每条微博抠出来（刻意不依赖 bs4/lxml）。

        关键点：每条微博要按「卡片自身的范围」切片，不能用一个固定长度窗口，
        否则会把下一张卡片的正文算到当前 mid 上。
        """
        card_re = re.compile(r'<div\s+class="card-wrap"[^>]*?mid="(\d+)"')
        starts = [(m.group(1), m.start()) for m in card_re.finditer(html_text)]

        # 顺序很重要：先找 `feed_list_content_full` —— 它写在 HTML 里但被 style="display:none"
        # 藏起来，内容**就是正文全文**（2026-10 实测确认）。找不到才退回截断版
        # `feed_list_content`（尾巴上挂着「展开」按钮）。这样拿全文不用多发一次请求。
        text_res = (
            re.compile(r'<p[^>]*node-type="feed_list_content_full"[^>]*>(.*?)</p>', re.S),
            re.compile(r'<p[^>]*class="txt"[^>]*node-type="feed_list_content"[^>]*>(.*?)</p>', re.S),
            re.compile(r'<p[^>]*node-type="feed_list_content"[^>]*class="txt"[^>]*>(.*?)</p>', re.S),
            re.compile(r'<p[^>]*node-type="feed_list_content"[^>]*>(.*?)</p>', re.S),
        )

        posts: List[Dict[str, Any]] = []
        seen = set()
        for idx, (mid, start) in enumerate(starts):
            if mid in seen:
                continue
            end = starts[idx + 1][1] if idx + 1 < len(starts) else len(html_text)
            block = html_text[start:end]

            text = ""
            truncated = False
            for pat_idx, pattern in enumerate(text_res):
                match = pattern.search(block)
                if match:
                    text = clean_text(match.group(1))
                    # 第 0 个 pattern 才是全文版；命中后面的说明只有摘要
                    truncated = pat_idx > 0
                    break
            if not text:
                continue

            seen.add(mid)
            posts.append(
                {
                    "mid": mid,
                    "topic": topic,
                    "text": text,
                    "truncated": truncated,
                    "url": f"https://weibo.com/{mid}",
                }
            )
        return posts

    # ------------------------------------------------------------ 单条微博详情
    def _dump_detail(self, payload: Dict[str, Any]) -> None:
        """存一份微博详情的原始 JSON（仅 --dump-raw 时生效，只存第一个）。"""
        target = getattr(self, "dump_raw_dir", None)
        if not target or getattr(self, "_detail_dumped", False):
            return
        self._detail_dumped = True
        try:
            path = Path(target) / "_raw_detail_sample.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2)[:300000],
                            encoding="utf-8")
            print(f"    [debug] 微博详情原始 JSON 已保存：{path}")
        except OSError as exc:
            print(f"    [debug] 详情保存失败：{exc}")

    def fetch_post_detail(self, mid: str) -> Dict[str, Any]:
        """取单条微博的详情：正文全文 + 作者 + 转评赞 + 发布时间。

        搜索页给的长文只有摘要（尾巴带「展开」），这里打 `statuses/show` 拿全文。
        每条微博多一次请求，只有 --full-text 时才走这一段。
        """
        payload = self._get_json(
            STATUS_SHOW,
            {"id": mid, "locale": "zh-CN"},
            headers={"Referer": f"https://weibo.com/{mid}"},
        )
        self._dump_detail(payload)
        return self._status_row(payload, mid)

    @staticmethod
    def _status_row(payload: Dict[str, Any], mid: str) -> Dict[str, Any]:
        raw = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        long_text = raw.get("longText") if isinstance(raw.get("longText"), dict) else {}
        text = clean_text(
            long_text.get("longTextContent") or raw.get("text_raw") or raw.get("text") or ""
        )
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        return {
            "mid": str(raw.get("idstr") or raw.get("id") or mid),
            "text": text,
            "text_len": len(text),
            "truncated": bool(raw.get("isLongText")) and not bool(long_text),
            "created_at": parse_created_at(raw.get("created_at")),
            "author_id": str(user.get("idstr") or user.get("id") or ""),
            "author_name": clean_text(user.get("screen_name") or ""),
            "author_followers": to_int(user.get("followers_count")),
            "author_verified": bool(user.get("verified")),
            "reposts": to_int(raw.get("reposts_count")),
            "comments": to_int(raw.get("comments_count")),
            "attitudes": to_int(raw.get("attitudes_count")),
            "source": clean_text(raw.get("source") or ""),
            "url": f"https://weibo.com/{mid}",
        }

    def _dump_search_html(self, page: int, html_text: str) -> None:
        """把话题搜索页的原始 HTML 存一份，用来补作者/转评赞等字段（仅 --dump-search-html 时生效）。"""
        target = getattr(self, "dump_search_dir", None)
        if not target:
            return
        self._search_dump_seq = getattr(self, "_search_dump_seq", 0) + 1
        try:
            path = Path(target) / f"_raw_search_{self._search_dump_seq:03d}_p{page}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html_text, encoding="utf-8")
            print(f"    [debug] 搜索页 HTML 已保存：{path}（{len(html_text)} 字节）")
        except OSError as exc:
            print(f"    [debug] 搜索页保存失败：{exc}")

    # ------------------------------------------------------------ 评论
    def _dump_raw(self, payload: Dict[str, Any]) -> None:
        """把第一页评论的**原始 JSON** 存一份，用来核对字段名（仅 --dump-raw 时生效）。"""
        target = getattr(self, "dump_raw_dir", None)
        if not target or getattr(self, "_raw_dumped", False):
            return
        self._raw_dumped = True
        try:
            path = Path(target) / "_raw_comments_sample.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2)[:300000],
                            encoding="utf-8")
            print(f"    [debug] 原始响应已保存：{path}")
        except OSError as exc:
            print(f"    [debug] 原始响应保存失败：{exc}")

    def _comment_page(self, target_id: str, params: Dict[str, Any], referer: str) -> Dict[str, Any]:
        """取一页评论数据（主评论与楼中楼共用）。

        2026-10 实测：
          * 匿名 **GET** 该路径 → HTTP 403 `{"error":"Forbidden"}`：路径有效，只是缺登录态。
          * 用 **POST** 打同一路径 → HTTP 404：网关不认这个方法，说明评论接口只吃 GET。
        所以这里默认走 GET；GET 拿不到数据时才退回 POST（兼容更老的接口版本）。
        """
        payload: Dict[str, Any] = {}
        try:
            resp = self._request("GET", BUILD_COMMENTS, params=params, headers={"Referer": referer})
            payload = resp.json()
        except ValueError:
            print("    [警告] 评论接口返回的不是 JSON（可能被重定向到登录/验证页）")
        # HTTP 错误（403 / 404 / 限流）一律向上抛，不要吞掉 ——
        # 吞掉的话用户只会看到「没有抓到评论」，分不清是凭证、方法还是参数问题。

        if isinstance(payload, dict):
            self._dump_raw(payload)
            self._check_payload(payload)  # 登录态 / 风控要抛出明确异常
            if self._extract_batch(payload):
                return payload

        # 兜底：极老版本接口只认 POST（新网关会 404 掉，失败就安静返回）
        try:
            return self._post_json(BUILD_COMMENTS, params, referer)
        except WeiboError:
            return payload if isinstance(payload, dict) else {}

    def _fetch_comment_page(
        self,
        post_id: str,
        max_id: str = "0",
        max_id_type: str = "0",
        fetch_reply: bool = False,
    ) -> Dict[str, Any]:
        """抓某条微博的主评论页（id 必须是微博 mid）。"""
        referer = f"https://weibo.com/{post_id}"
        params: Dict[str, Any] = {
            "is_reload": "1",
            "id": post_id,
            "is_show_bulletin": "2",
            "is_mix": "0",
            "count": "20",
            "flow": str(getattr(self, "comment_flow", "0")),
            "locale": "zh-CN",
        }
        if fetch_reply:
            params["fetch_level"] = "1"  # 1 = 同时带回楼中楼
        if max_id and max_id != "0":
            params["max_id"] = max_id
            params["max_id_type"] = max_id_type
        return self._comment_page(post_id, params, referer)

    def _fetch_reply_page(
        self,
        post_id: str,
        root_id: str,
        max_id: str = "0",
        max_id_type: str = "0",
    ) -> Dict[str, Any]:
        """抓某条主评论下的楼中楼页。

        注意：楼中楼分页的 id 必须是**主评论 id**，不是微博 mid，
        否则会把主评论当成回复再返回一遍。
        """
        referer = f"https://weibo.com/{post_id}?type=comment"
        params: Dict[str, Any] = {
            "is_reload": "1",
            "id": root_id,
            "is_show_bulletin": "2",
            "is_mix": "0",
            "count": "20",
            "flow": "0",
            "locale": "zh-CN",
        }
        if max_id and max_id != "0":
            params["max_id"] = max_id
            params["max_id_type"] = max_id_type
        return self._comment_page(root_id, params, referer)

    @staticmethod
    def _row(
        raw: Dict[str, Any],
        post_id: str,
        topic: str,
        topic_rank: int,
        is_reply: bool,
        root_id: str = "",
    ) -> Comment:
        user = raw.get("user") or {}
        reply_comment = raw.get("reply_comment") or {}
        reply_user = (reply_comment.get("user") or {}).get("screen_name", "")
        return Comment(
            comment_id=str(raw.get("idstr") or raw.get("id") or ""),
            text=clean_text(raw.get("text_raw") or raw.get("text") or ""),
            created_at=parse_created_at(raw.get("created_at")),
            user_id=str(user.get("idstr") or user.get("id") or ""),
            user_name=clean_text(user.get("screen_name") or ""),
            user_verified=bool(user.get("verified") or user.get("verified_type") not in (None, -1)),
            user_followers=to_int(user.get("followers_count")),
            likes=pick_like_count(raw),
            reply_count=to_int(raw.get("total_number") or raw.get("reply_count")),
            source=clean_text(raw.get("source") or ""),
            ip_location=extract_ip_location(raw),
            post_id=post_id,
            topic=topic,
            topic_rank=topic_rank,
            is_reply=is_reply,
            root_comment_id=root_id,
            reply_to_user=clean_text(reply_user),
        )

    @staticmethod
    def _extract_batch(payload: Any) -> List[Dict[str, Any]]:
        """把接口响应里的数据列表安全地取出来（结构异常时返回空列表而不是崩掉）。"""
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def fetch_post_comments(
        self,
        post_id: str,
        topic: str = "",
        topic_rank: int = 0,
        max_pages: int = 20,
        fetch_replies: bool = False,
        max_reply_pages: int = 3,
    ) -> List[Comment]:
        """抓取一条微博下的全部主评论（可选连带楼中楼回复）。

        max_pages=0 表示不限页数，但仍会做「游标不前进就停」的保护，不会死循环。
        """
        comments: List[Comment] = []
        seen_ids = set()
        max_id, max_id_type = "0", "0"
        prev_max_id: Optional[str] = None
        page = 0
        self.last_total_number = 0

        if not self.cookie:
            raise LoginRequired(
                "评论接口需要登录态，但当前没有 Cookie。"
                "请复制 cookie.txt.example 为 cookie.txt 并填入微博 Cookie（见 README「二、准备 Cookie」）。"
            )

        while True:
            page += 1
            if max_pages and page > max_pages:
                break

            try:
                payload = self._fetch_comment_page(post_id, max_id, max_id_type, fetch_reply=fetch_replies)
            except WeiboError as exc:
                # 已经抓到的部分保留，不要因为中途失败把整条博文的数据丢掉
                print(f"    [中断] 第 {page} 页失败：{exc}")
                break
            batch = self._extract_batch(payload)
            if not batch:
                break

            for raw in batch:
                root = self._row(raw, post_id, topic, topic_rank, is_reply=False)
                if root.comment_id and root.comment_id in seen_ids:
                    continue
                if root.comment_id:
                    seen_ids.add(root.comment_id)
                comments.append(root)
                if fetch_replies:
                    replies = self._collect_replies(
                        raw, post_id, topic, topic_rank, root.comment_id, max_reply_pages
                    )
                    for reply in replies:
                        if reply.comment_id and reply.comment_id in seen_ids:
                            continue
                        if reply.comment_id:
                            seen_ids.add(reply.comment_id)
                        comments.append(reply)

            max_id = str(payload.get("max_id") or "0")
            max_id_type = str(payload.get("max_id_type") or "0")
            total = to_int(payload.get("total_number"))
            self.last_total_number = total
            print(f"    第 {page} 页：+{len(batch)} 条主评论（累计 {len(comments)} 行，接口报告总数 {total or '?'}）")

            if max_id == "0":
                break
            if max_id == prev_max_id:  # 游标没前进，再翻就是重复数据
                print("    [停止] 分页游标未前进，避免重复抓取")
                break
            prev_max_id = max_id

        return comments

    def _collect_replies(
        self,
        raw: Dict[str, Any],
        post_id: str,
        topic: str,
        topic_rank: int,
        root_id: str,
        max_reply_pages: int,
    ) -> List[Comment]:
        """楼中楼：优先用接口内嵌数据，不够再翻页。"""
        replies: List[Comment] = []

        def push(items: Iterable[Dict[str, Any]]) -> int:
            added = 0
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                if not (item.get("idstr") or item.get("id")):
                    continue
                replies.append(self._row(item, post_id, topic, topic_rank, is_reply=True, root_id=root_id))
                added += 1
            return added

        push(raw.get("comments") or [])

        # 内嵌数据取不干净时，再按 max_id 游标翻页补齐。
        # 注意：这里不用 total_number 做上限判断 —— 主评论接口里该字段语义不稳定，
        # 会导致无限翻页，因此只服从 max_id 游标和 max_reply_pages 上限。
        if max_reply_pages <= 0:
            return replies

        max_id = str(raw.get("max_id") or "0")
        max_id_type = str(raw.get("max_id_type") or "0")
        prev_max_id: Optional[str] = None
        page = 0
        while max_id and max_id != "0" and page < max_reply_pages:
            page += 1
            try:
                payload = self._fetch_reply_page(post_id, root_id, max_id, max_id_type)
            except WeiboError:
                break
            batch = self._extract_batch(payload)
            if not batch:
                break
            if push(batch) == 0:
                break
            max_id = str(payload.get("max_id") or "0")
            max_id_type = str(payload.get("max_id_type") or "0")
            if max_id == prev_max_id:
                break
            prev_max_id = max_id

        return replies

    # ------------------------------------------------------------ 组合流程
    def collect_topic_comments(
        self,
        topic: str,
        topic_rank: int = 0,
        post_limit: int = 3,
        max_pages: int = 20,
        fetch_replies: bool = False,
        max_reply_pages: int = 3,
        post_pages: int = 1,
        query: str = "",
        debug: bool = False,
        posts_out: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Comment]:
        """话题 -> 博文 -> 评论 的完整链路。

        query 用热搜条目自带的 word_scheme（形如 `#词#`）时搜索命中率更高。
        话题搜索页拿不到内容时会抛 LoginRequired（Cookie 缺失/失效）。
        """
        posts = self.fetch_topic_posts(query or topic, max_pages=max(1, post_pages), debug=debug)
        posts = posts[:post_limit]
        for post in posts:  # 原地补元数据，抓完评论后回填覆盖率
            post["topic_rank"] = topic_rank
            # 搜索时用的是 `#词#` 表达式，但归属要记成干净的词，方便按话题分组
            post["topic"] = topic
        if posts_out is not None:
            posts_out.extend(posts)
        if not posts:
            print(
                f"  [警告] 话题「{topic}」搜索页里没解析出博文。"
                "常见原因：Cookie 失效/为访客态、该词条在 s.weibo.com 无结果、或搜索页 HTML 改版。"
                "可加 --debug 查看每次请求的最终 URL 与解析条数。"
            )
            return []

        results: List[Comment] = []
        for idx, post in enumerate(posts, start=1):
            print(f"  [{idx}/{len(posts)}] 抓评论 {post['mid']}  {post['text'][:30]}...")
            try:
                got = self.fetch_post_comments(
                    post_id=post["mid"],
                    topic=topic,
                    topic_rank=topic_rank,
                    max_pages=max_pages,
                    fetch_replies=fetch_replies,
                    max_reply_pages=max_reply_pages,
                )
            except WeiboError as exc:
                print(f"    [跳过] {exc}")
                got = []
            results.extend(got)
            # 覆盖率：接口报的评论总数 vs 我们真正抓到多少
            post["comments_captured"] = len(got)
            post["comments_reported"] = getattr(self, "last_total_number", 0)
        return results


# ==========================================================================
# 导出
# ==========================================================================
def export_json(rows: List[Dict[str, Any]], path, meta: Optional[Dict[str, Any]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta or {}, "count": len(rows), "items": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def export_csv(rows: List[Dict[str, Any]], path) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding=config.OUTPUT_ENCODING)
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding=config.OUTPUT_ENCODING, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def timestamp_slug() -> str:
    return datetime.now(CST).strftime("%Y%m%d_%H%M%S")
