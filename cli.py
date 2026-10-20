"""命令行入口。

常用示例（在 weibo_hotsearch 目录下执行）：

    # 0) 看看当前热搜榜（不需要 Cookie）
    python cli.py hot --top 30

    # 1) 爬热搜第 1 名的完整评论
    python cli.py comments --rank 1

    # 2) 爬热搜前 3 名，每个话题取前 5 条博文、每条最多 10 页评论，并连带楼中楼
    python cli.py comments --rank 3 --post-limit 5 --max-pages 10 --with-replies

    # 3) 直接按话题词爬
    python cli.py comments --topic "两会" --post-limit 3

    # 4) 手上有某条微博 mid，直接爬它的评论
    python cli.py post --mid 5012345678901234 --max-pages 50

    # 5) 检查 Cookie 是否可用
    python cli.py check
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

try:  # 支持 `python cli.py` 和 `python -m weibo_hotsearch.cli` 两种方式
    from . import config
    from . import scraper
    from .scraper import LoginRequired, RateLimited, WeiboClient, WeiboError
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import scraper  # type: ignore[no-redef]
    from scraper import LoginRequired, RateLimited, WeiboClient, WeiboError  # type: ignore[no-redef]


# ==========================================================================
# 公共参数
# ==========================================================================
def setup_console() -> None:
    """Win7/中文 Windows 控制台默认 GBK，统一改成 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def add_network_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cookie", help="微博 Cookie；不传则读环境变量 WEIBO_COOKIE 或 cookie.txt")
    parser.add_argument("--delay", type=float, help=f"请求间隔秒数（默认 {config.REQUEST_DELAY}）")
    parser.add_argument("--jitter", type=float, help=f"随机抖动上限秒数（默认 {config.REQUEST_JITTER}）")
    parser.add_argument("--retries", type=int, help=f"失败重试次数（默认 {config.MAX_RETRIES}）")
    parser.add_argument("--timeout", type=int, help=f"请求超时秒数（默认 {config.TIMEOUT}）")
    parser.add_argument("--proxy", help="代理地址，例如 http://127.0.0.1:7890")
    parser.add_argument("--outdir", help=f"输出目录（默认 {config.OUTPUT_DIR}）")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["csv", "json", "both"], default="both", help="导出格式")
    parser.add_argument("--tag", help="输出文件名后缀，默认用时间戳")


def build_client(args: argparse.Namespace) -> WeiboClient:
    return WeiboClient(
        cookie=args.cookie,
        delay=args.delay,
        jitter=args.jitter,
        max_retries=args.retries,
        timeout=args.timeout,
        proxy=args.proxy,
    )


def safe_dirname(text: str, limit: int = 40) -> str:
    """把话题词变成合法的 Windows 目录名（去掉 \\ / : * ? " < > | 等）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t#]', "_", str(text or "")).strip(" ._")
    return (cleaned or "topic")[:limit]


def dump(rows, name: str, args: argparse.Namespace, meta: dict | None = None,
         outdir=None, slug: str | None = None):
    if not rows:
        print("没有数据可导出。")
        return []
    target = Path(outdir) if outdir else (
        Path(args.outdir) if getattr(args, "outdir", None) else config.OUTPUT_DIR
    )
    if slug is None:
        tag = args.tag or scraper.timestamp_slug()
    else:
        tag = slug
    stem = f"{name}_{tag}" if tag else name
    written = []
    if args.format in ("csv", "both"):
        path = target / f"{stem}.csv"
        scraper.export_csv(rows, path)
        written.append(path)
    if args.format in ("json", "both"):
        path = target / f"{stem}.json"
        scraper.export_json(rows, path, meta)
        written.append(path)
    for path in written:
        print(f"已导出：{path}（{len(rows)} 条）")
    return written


# ==========================================================================
# 子命令
# ==========================================================================
def cmd_hot(args: argparse.Namespace) -> int:
    client = build_client(args)
    items = client.fetch_hot_search()
    if not items:
        print("没有抓到热搜数据（接口结构可能变了，或被风控）。")
        return 1

    top = max(1, args.top)
    rows = [
        {
            "rank": it.rank,
            "word": it.word,
            "hot": it.raw_hot,
            "label": it.label,
            "search": it.topic_url,
        }
        for it in items[:top]
    ]
    print(f"\n微博实时热搜榜（共 {len(items)} 条真实词条，已剔除广告位；展示前 {len(rows)} 条）")
    print("-" * 68)
    for row in rows:
        print(f"{row['rank']:>2}. {row['word']:<28} {row['hot']:>12,}  {row['label']}")
    print("-" * 68)
    if rows:
        print(f"搜索链接示例：{rows[0]['search']}")

    if args.export:
        dump(rows, "hot_search", args, {"generated_at": scraper.timestamp_slug()})
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    started = time.monotonic()
    client = build_client(args)
    if not client.cookie:
        print("[缺少 Cookie] 评论接口需要登录态。")
        print("  请复制 cookie.txt.example 为 cookie.txt 并填入微博 Cookie（见 README「二、准备 Cookie」），")
        print("  或用 --cookie \"SUB=...\" 临时传入。热搜榜本身不需要 Cookie。")
        return 2

    if getattr(args, "dump_raw", None):
        client.dump_raw_dir = args.dump_raw
    if getattr(args, "dump_search_html", None):
        client.dump_search_dir = args.dump_search_html
    if getattr(args, "flow", None) is not None:
        client.comment_flow = str(args.flow)

    # 话题统一成 (话题词, 榜位, 搜索表达式, 热度)
    if args.topic:
        topics = [(args.topic, 0, args.topic, 0)]
    else:
        print("正在获取热搜榜 ...")
        hot_items = client.fetch_hot_search()
        if not hot_items:
            print("热搜榜为空，无法确定话题，请改用 --topic。")
            return 1
        selected = hot_items[: max(1, args.rank)]
        topics = [(it.word, it.rank, it.query, it.raw_hot) for it in selected]
        print(f"目标话题（共 {len(topics)} 个）：")
        for word, rank, _query, hot in topics:
            print(f"  #{rank} {word}    热度 {hot:,}")

        # 榜单快照本身也是数据：以后要回溯「那天前十是什么」就靠它
        if args.export_hot:
            hot_rows = [
                {"topic_rank": it.rank, "word": it.word, "hot": it.raw_hot,
                 "label": it.label, "realpos": it.realpos, "search_query": it.query}
                for it in selected
            ]
            dump(hot_rows, "hot_topics", args, {"generated_at": scraper.timestamp_slug()})

    all_comments = []
    all_posts: list = []
    per_topic_count = {}
    per_topic_comments: dict = {}
    for word, rank, query, _hot in topics:
        print(f"\n=== 抓取话题：{word}（榜位 {rank or '手动指定'}）===")
        try:
            comments = client.collect_topic_comments(
                topic=word,
                topic_rank=rank,
                post_limit=args.post_limit,
                max_pages=args.max_pages,
                fetch_replies=args.with_replies,
                max_reply_pages=args.max_reply_pages,
                post_pages=args.post_pages,
                query=query,
                debug=args.debug,
                posts_out=all_posts,
            )
        except (LoginRequired, RateLimited) as exc:
            # 登录态 / 风控会连累后面所有话题，早点停，别把时间浪费完
            print(f"  [终止] {exc}")
            print(f"  已抓到 {sum(per_topic_count.values())} 条，先停在这里。")
            return 2 if isinstance(exc, LoginRequired) else 3
        except WeiboError as exc:
            print(f"  [跳过] {exc}")
            comments = []
        per_topic_count[word] = len(comments)
        per_topic_comments[word] = comments
        all_comments.extend(comments)

    elapsed = time.monotonic() - started
    # 正文全文 + 作者 + 互动数：搜索页只给摘要，这里按需补详情
    if getattr(args, "full_text", True) and all_posts:
        print(f"\n补抓 {len(all_posts)} 条博文详情（全文 / 作者 / 转评赞）...")
        for idx, post in enumerate(all_posts, start=1):
            try:
                detail = client.fetch_post_detail(post["mid"])
            except WeiboError as exc:
                print(f"  [{idx}/{len(all_posts)}] 失败 {post['mid']}：{exc}")
                continue
            post["detail"] = detail
            print(f"  [{idx}/{len(all_posts)}] {post['mid']}  正文 {detail['text_len']} 字  "
                  f"@{detail['author_name']}  转{detail['reposts']} 评{detail['comments']} "
                  f"赞{detail['attitudes']}")
        elapsed = time.monotonic() - started

    # 微博正文：搜索页里本来就解析出来了，以前只用来打日志，现在一并落盘
    post_rows = []
    for p in all_posts:
        detail = p.get("detail") or {}
        # 正文取「更长的那一份」：
        #   搜索页的 feed_list_content_full 是全文（实测 317 字）；
        #   statuses/show 返回的 text 反而可能仍是截断版（147 字）。
        # 之前直接拿 detail 覆盖，把全文又盖回摘要了，这里按长度择优。
        detail_text = detail.get("text") or ""
        search_text = p.get("text", "")
        if len(detail_text) > len(search_text):
            text = detail_text
            truncated = bool(detail.get("truncated", False))
        else:
            text = search_text
            truncated = bool(p.get("truncated"))
        post_rows.append({
            "topic_rank": p.get("topic_rank", 0),
            "topic": p.get("topic", ""),
            "mid": p.get("mid", ""),
            "text": text,
            "text_len": len(text),
            "truncated": truncated,
            "created_at": detail.get("created_at", ""),
            "author_name": detail.get("author_name", ""),
            "author_id": detail.get("author_id", ""),
            "author_followers": detail.get("author_followers", ""),
            "reposts": detail.get("reposts", ""),
            "comments": detail.get("comments", ""),
            "attitudes": detail.get("attitudes", ""),
            "source": detail.get("source", ""),
            "comments_reported": p.get("comments_reported", ""),
            "comments_captured": p.get("comments_captured", ""),
            "coverage": (
                round(p.get("comments_captured", 0) / p["comments_reported"], 2)
                if p.get("comments_reported")
                else ""
            ),
            "url": p.get("url", ""),
        })

    if not all_comments:
        print(f"\n没有抓到任何评论（耗时 {elapsed:.0f}s）。"
              "可能原因：未配置 Cookie、话题下无内容、或已被风控。")
        if post_rows:
            dump(post_rows, "posts", args, {"generated_at": scraper.timestamp_slug()})
            print(f"不过抓到了 {len(post_rows)} 条博文正文，已单独导出。")
        return 1

    rows = [c.as_row() for c in all_comments]
    rows.sort(key=lambda r: (r["topic_rank"], scraper.to_int(r["comment_id"])))
    meta = {
        "generated_at": scraper.timestamp_slug(),
        "elapsed_seconds": round(elapsed, 1),
        "topics": [
            {"word": w, "rank": r, "hot": h, "comments": per_topic_count.get(w, 0)}
            for w, r, _q, h in topics
        ],
        "settings": config.settings_snapshot(),
    }
    print(f"\n共抓到 {len(rows)} 条评论（含回复：{args.with_replies}），耗时 {elapsed:.0f}s")
    dump(rows, "weibo_comments", args, meta)

    if post_rows:
        cut = sum(1 for r in post_rows if r.get("truncated"))
        print(f"共 {len(post_rows)} 条博文正文（其中 {cut} 条仍是摘要，其余为全文）")
        dump(post_rows, "posts", args, meta)

    # 话题级汇总：每个话题各抓到多少，一张表看完
    summary = [
        {"topic_rank": r, "word": w, "hot": h, "comments": per_topic_count.get(w, 0)}
        for w, r, _q, h in topics
    ]
    dump(summary, "topics_summary", args, meta)

    # 按话题分目录：一条热搜 = 一个文件夹，博文 / 评论 / 小结都在里面
    if getattr(args, "split_topics", True):
        base = Path(args.outdir) if getattr(args, "outdir", None) else config.OUTPUT_DIR
        root = base / f"top{len(topics)}_{scraper.timestamp_slug()}"
        index_rows = []
        for idx, (word, rank, _q, hot) in enumerate(topics, start=1):
            sub = root / f"{idx:02d}_{safe_dirname(word)}"
            sub.mkdir(parents=True, exist_ok=True)
            t_comments = [c.as_row() for c in per_topic_comments.get(word, [])]
            t_posts = [r for r in post_rows if r.get("topic") == word]
            t_meta = {
                "generated_at": scraper.timestamp_slug(),
                "topic_rank": rank,
                "topic": word,
                "hot": hot,
                "posts": len(t_posts),
                "comments": len(t_comments),
            }
            if t_posts:
                dump(t_posts, "posts", args, t_meta, outdir=sub, slug="")
            if t_comments:
                dump(t_comments, "comments", args, t_meta, outdir=sub, slug="")
            index_rows.append({
                "topic_rank": rank,
                "topic": word,
                "hot": hot,
                "posts": len(t_posts),
                "comments": len(t_comments),
                "folder": sub.name,
            })
        if index_rows:
            dump(index_rows, "_index", args,
                 {"generated_at": scraper.timestamp_slug()}, outdir=root, slug="")
            print(f"\n按话题分组导出完成：{root}")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    client = build_client(args)
    if not client.cookie:
        print("[缺少 Cookie] 评论接口需要登录态。")
        print("  请复制 cookie.txt.example 为 cookie.txt 并填入微博 Cookie（见 README「二、准备 Cookie」），")
        print("  或用 --cookie \"SUB=...\" 临时传入。")
        return 2

    print(f"抓取微博 {args.mid} 的评论 ...")
    comments = client.fetch_post_comments(
        post_id=args.mid,
        topic=args.topic or "",
        topic_rank=0,
        max_pages=args.max_pages,
        fetch_replies=args.with_replies,
        max_reply_pages=args.max_reply_pages,
    )
    if not comments:
        print("没有抓到评论。")
        return 1
    rows = [c.as_row() for c in comments]
    print(f"\n共抓到 {len(rows)} 条评论")
    dump(rows, f"post_{args.mid}", args, {"generated_at": scraper.timestamp_slug(), "mid": args.mid})
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    client = build_client(args)
    print(f"Cookie: {'已提供' if client.cookie else '缺失'}（长度 {len(client.cookie)}）")
    xsrf = scraper.derive_xsrf(client.cookie)
    print(f"XSRF-TOKEN: {'已推导（' + str(len(xsrf)) + ' 字符）' if xsrf else '未找到（评论接口可能被 403）'}")
    if not client.cookie:
        print("提示：没有 Cookie 时热搜榜仍可抓，但评论接口会返回未登录错误。")
        print("      获取方式见 README.md「二、准备 Cookie」。")

    print("\n[1/2] 测试热搜榜接口 ...")
    try:
        items = client.fetch_hot_search()
        print(f"  OK，共 {len(items)} 条；第 1 名：{items[0].word if items else '—'}")
    except WeiboError as exc:
        print(f"  失败：{exc}")
        return 1

    if not client.cookie:
        return 0

    if not args.mid:
        print("\n[2/2] 跳过评论接口测试。")
        print("      热搜榜单里只有话题 id，没有微博 mid，无法用榜单数据验证评论接口。")
        print("      请找一条真实微博，用 `python cli.py check --mid <mid>` 验证。")
        return 0

    print(f"\n[2/2] 测试评论接口（mid={args.mid}）...")
    try:
        payload = client._fetch_comment_page(args.mid)
        print(f"  OK，返回 {len(client._extract_batch(payload))} 条评论")
    except LoginRequired as exc:
        print(f"  登录态问题：{exc}")
        return 1
    except WeiboError as exc:
        print(f"  失败（Cookie 可能仍可用，只是这条微博取不到评论）：{exc}")
    return 0


# ==========================================================================
# 参数解析
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weibo-hotsearch",
        description="爬取微博热搜榜话题的相关评论",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_hot = sub.add_parser("hot", help="查看实时热搜榜")
    p_hot.add_argument("--top", type=int, default=50, help="展示前 N 条（默认 50）")
    p_hot.add_argument("--export", action="store_true", help="同时导出文件")
    add_network_args(p_hot)
    add_output_args(p_hot)
    p_hot.set_defaults(func=cmd_hot)

    p_com = sub.add_parser("comments", help="爬热搜话题的评论（主流程）")
    p_com.add_argument("--rank", type=int, default=1, help="取热搜前 N 名，默认 1（即第 1 名）")
    p_com.add_argument("--topic", help="直接指定话题词，指定后忽略 --rank")
    p_com.add_argument("--post-limit", type=int, default=3, help="每个话题最多抓几条博文的评论（默认 3）")
    p_com.add_argument("--post-pages", type=int, default=1, help="话题搜索页翻几页来挑博文（默认 1，调大可扩大样本）")
    p_com.add_argument("--no-full-text", dest="full_text", action="store_false", default=True,
                       help="不补抓博文详情（作者/转评赞会留空；正文全文仍可从搜索页拿到）")
    p_com.add_argument("--flow", choices=["0", "1"], default="0",
                       help="评论排序：0=按热度（默认），1=按时间（换条路可能抓到更多）")
    p_com.add_argument("--no-split", dest="split_topics", action="store_false", default=True,
                       help="不按话题分文件夹，只导出合并的大表")
    p_com.add_argument("--max-pages", type=int, default=config.MAX_COMMENT_PAGES, help="每条博文最多翻几页评论，0 表示不限")
    p_com.add_argument("--with-replies", action="store_true", help="同时抓取楼中楼回复")
    p_com.add_argument("--max-reply-pages", type=int, default=config.MAX_REPLY_PAGES, help="每条主评论的楼中楼翻页上限")
    p_com.add_argument("--debug", action="store_true", help="打印每次请求的最终 URL 与解析条数")
    p_com.add_argument("--no-hot-export", dest="export_hot", action="store_false", default=True,
                       help="不把本次热搜榜快照一起导出")
    p_com.add_argument("--dump-raw", metavar="DIR", help="把第一页评论的原始 JSON 存到 DIR，用于核对字段名")
    p_com.add_argument("--dump-search-html", dest="dump_search_html", metavar="DIR",
                       help="把话题搜索页的原始 HTML 存到 DIR，用于补作者等字段")
    add_network_args(p_com)
    add_output_args(p_com)
    p_com.set_defaults(func=cmd_comments)

    p_post = sub.add_parser("post", help="直接爬指定微博 mid 的评论")
    p_post.add_argument("--mid", required=True, help="微博 mid，例如 5012345678901234")
    p_post.add_argument("--topic", help="给数据打个话题标签")
    p_post.add_argument("--max-pages", type=int, default=config.MAX_COMMENT_PAGES, help="最多翻几页，0 表示不限")
    p_post.add_argument("--with-replies", action="store_true", help="同时抓取楼中楼回复")
    p_post.add_argument("--max-reply-pages", type=int, default=config.MAX_REPLY_PAGES, help="楼中楼翻页上限")
    add_network_args(p_post)
    add_output_args(p_post)
    p_post.set_defaults(func=cmd_post)

    p_check = sub.add_parser("check", help="检查 Cookie 与接口连通性")
    p_check.add_argument("--mid", help="可选：给一条真实微博 mid，用于验证评论接口是否可读")
    add_network_args(p_check)
    p_check.set_defaults(func=cmd_check)

    return parser


def main(argv=None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LoginRequired as exc:
        print(f"\n[登录态错误] {exc}")
        return 2
    except RateLimited as exc:
        print(f"\n[被限流] {exc}")
        return 3
    except WeiboError as exc:
        print(f"\n[接口错误] {exc}")
        return 4
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # 接口结构变化时不吐 traceback，给出可读提示
        print(f"\n[未预期的错误] {exc.__class__.__name__}: {exc}")
        print("  如果怀疑是微博接口改版，请把上面的报错连同 `python cli.py hot` 的输出一起反馈。")
        return 5


if __name__ == "__main__":
    sys.exit(main())
