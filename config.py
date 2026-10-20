"""全局配置。

说明：
    - 所有项都可以被命令行参数覆盖，也可以全部通过环境变量提供（适合 CI / 临时使用）。
    - COOKIE 是唯一必须自己填的东西：微博的评论接口不再是匿名可读，
      未登录请求会返回 ok=0 / code=100001，必须带上浏览器里的登录 Cookie。
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("WEIBO_OUTPUT_DIR") or (BASE_DIR / "output"))
COOKIE_FILE = Path(os.getenv("WEIBO_COOKIE_FILE") or (BASE_DIR / "cookie.txt"))

# --------------------------------------------------------------------------
# 身份 / 请求头
# --------------------------------------------------------------------------
# 从浏览器 F12 -> Network -> 任意 weibo.com 请求 -> Request Headers 里整行复制 Cookie
COOKIE = os.getenv("WEIBO_COOKIE", "")

# 与登录态无关，但建议换成你自己浏览器的 UA，降低被风控的概率
USER_AGENT = os.getenv(
    "WEIBO_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

# 扩展参数：在 weibo.com 页面执行
#   copy(document.cookie.match(/_weibo_s\s*=\s*([^;]+)/)[1])
# 拿不到就留空，脚本会自动从 Cookie 里推导。
XSRF_TOKEN = os.getenv("WEIBO_XSRF_TOKEN", "")

# --------------------------------------------------------------------------
# 抓取节奏（调大更安全，调小更容易触发风控）
# --------------------------------------------------------------------------
REQUEST_DELAY = float(os.getenv("WEIBO_REQUEST_DELAY", "1.5"))   # 每次请求之间的基础间隔（秒）
REQUEST_JITTER = float(os.getenv("WEIBO_REQUEST_JITTER", "0.8"))  # 随机抖动上限（秒）
MAX_RETRIES = int(os.getenv("WEIBO_MAX_RETRIES", "4"))            # 单次请求失败重试次数
TIMEOUT = int(os.getenv("WEIBO_TIMEOUT", "15"))                   # 请求超时（秒）

# 触发风控（429 / ok=-100）后的冷却基数，按重试次数指数增长
BACKOFF_BASE = float(os.getenv("WEIBO_BACKOFF_BASE", "5.0"))

# --------------------------------------------------------------------------
# 抓取范围
# --------------------------------------------------------------------------
MAX_COMMENT_PAGES = int(os.getenv("WEIBO_MAX_COMMENT_PAGES", "20"))   # 0 = 不限
MAX_REPLY_PAGES = int(os.getenv("WEIBO_MAX_REPLY_PAGES", "3"))        # 每条主评论的楼中楼页数上限

# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
OUTPUT_ENCODING = "utf-8-sig"  # 带 BOM，Excel 直接双击打开不乱码


COOKIE_PLACEHOLDER = "PASTE_YOUR_COOKIE_HERE"


def load_cookie() -> str:
    """按 环境变量 -> cookie.txt 的顺序取 Cookie。"""
    if COOKIE.strip() and COOKIE.strip() != COOKIE_PLACEHOLDER:
        return COOKIE.strip()
    if COOKIE_FILE.exists():
        value = COOKIE_FILE.read_text(encoding="utf-8").strip()
        # 忽略模板里的占位符，避免用户没改就得到一个费解的接口报错
        if value and value != COOKIE_PLACEHOLDER:
            return value
    return ""


def settings_snapshot() -> dict:
    """给日志/元数据用的配置快照（不含 Cookie 明文）。"""
    return {
        "request_delay": REQUEST_DELAY,
        "request_jitter": REQUEST_JITTER,
        "max_retries": MAX_RETRIES,
        "timeout": TIMEOUT,
        "max_comment_pages": MAX_COMMENT_PAGES,
        "max_reply_pages": MAX_REPLY_PAGES,
        "output_dir": str(OUTPUT_DIR),
        "cookie_present": bool(load_cookie()),
    }
