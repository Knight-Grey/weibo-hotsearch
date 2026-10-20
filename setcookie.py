"""Cookie 填入助手：把浏览器里复制到的东西粘进来，自动提取并写入 cookie.txt。

用法：

    python setcookie.py

然后按提示粘贴以下任意一种形式（都能识别）：
    * F12 -> Network -> Request Headers 里 Cookie: 后面的整串
    * document.cookie 的整串输出
    * 只粘贴 SUB=xxxx 这一段
    * 只粘贴 SUB 的值本身（一串 base64）
    * 整条 curl 命令 / fetch 代码（会从里面把 Cookie 抠出来）

粘贴后按回车结束输入，脚本会：
    1. 提取出 Cookie 串
    2. 写入 cookie.txt
    3. 立刻联网验证热搜榜与评论接口是否可用
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import config
import scraper

sys.stdout.reconfigure(encoding="utf-8")

SUB_RE = re.compile(r"(SUB=[^;\s\"'\\]+)")
SUB_VALUE_RE = re.compile(r"\b(SUB)\s*[=:]\s*([A-Za-z0-9=_\-]{10,})")
COOKIE_HEADER_RE = re.compile(r"(?:cookie|Cookie)\s*:\s*([^\n\r]+)")
RAW_SUB_RE = re.compile(r"^[A-Za-z0-9=_\-]{15,}$")


def extract_cookie(raw: str) -> str:
    """从用户粘贴的任意文本里尽力提取出可用的 Cookie 串。"""
    text = raw.strip().strip('"').strip("'")
    if not text:
        return ""

    # 1) 标准的 Cookie: 头（curl / fetch / devtools 复制）
    header = COOKIE_HEADER_RE.search(text)
    if header:
        text = header.group(1).strip()

    # 2) 文本里出现 SUB=... 就直接用那一段（优先 SUBP 之后的内容）
    pairs = re.findall(r"([A-Za-z0-9_\-]+)=([^;\s\"']*)", text)
    if pairs:
        joined = "; ".join(f"{k}={v}" for k, v in pairs if k and v != "")
        if "SUB=" in joined:
            return joined

    # 3) 只粘了 SUB 的值本身
    if RAW_SUB_RE.match(text):
        return f"SUB={text}"

    return text


def main() -> int:
    print("=" * 60)
    print("微博 Cookie 填入助手")
    print("=" * 60)
    print("请粘贴 Cookie（整串 / 只有 SUB 也行 / curl 命令也行），")
    print("粘贴完成后按回车，再按 Ctrl+Z 回车（Windows）结束输入：")
    print("-" * 60)

    try:
        raw = sys.stdin.read()
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130

    if not raw.strip():
        print("没有读到任何内容，已取消。")
        return 1

    cookie = extract_cookie(raw)
    if not cookie or "SUB" not in cookie:
        print("\n[失败] 没能从粘贴内容里识别出 SUB。")
        print("  请回到浏览器 F12 -> Network -> 任意请求 -> Request Headers，")
        print("  复制 Cookie: 后面那一整行再试一次。")
        return 1

    preview = cookie if len(cookie) <= 90 else cookie[:90] + f"...（共 {len(cookie)} 字符）"
    print(f"\n识别到 Cookie：{preview}")
    for key in ("SUB", "SUBP", "SSOLoginState", "XSRF-TOKEN", "_weibo_s"):
        if f"{key}=" in cookie:
            print(f"  ✓ 含 {key}")

    config.COOKIE_FILE.write_text(cookie, encoding="utf-8")
    print(f"\n已写入：{config.COOKIE_FILE}")

    print("\n开始联网验证 ...")
    client = scraper.WeiboClient(cookie=cookie, delay=0.5, jitter=0.3)

    print("\n[1/2] 热搜榜接口 ...")
    try:
        items = client.fetch_hot_search()
        print(f"  OK，共 {len(items)} 条；第 1 名：{items[0].word if items else '—'}")
    except scraper.WeiboError as exc:
        print(f"  失败：{exc}")

    print("\n[2/2] 话题搜索 + 评论接口 ...")
    print("  用当前热搜第 1 名做端到端测试：")
    if not items:
        print("  跳过（榜单没取到）。")
        return 1

    topic = items[0].word
    print(f"  话题：{topic}")
    try:
        posts = client.fetch_topic_posts(items[0].query or topic, max_pages=1, debug=True)
        print(f"  搜索到博文 {len(posts)} 条")
        if not posts:
            print("  [注意] 搜索页没解析出博文。若上面 final= 指向 passport.weibo.com，说明 Cookie 仍是访客态。")
            return 1
        comments = client.fetch_post_comments(
            post_id=posts[0]["mid"], topic=topic, topic_rank=items[0].rank, max_pages=1
        )
        print(f"  OK，抓到 {len(comments)} 条评论。Cookie 完全可用 ✓")
        print(f"\n现在就可以跑：python cli.py comments --rank 1")
    except scraper.LoginRequired as exc:
        print(f"  [登录态问题] {exc}")
        return 1
    except scraper.WeiboError as exc:
        print(f"  失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n[异常] {exc.__class__.__name__}: {exc}")
        sys.exit(2)
