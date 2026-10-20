"""把 weibo_hotsearch\\output 里最新一批抓取结果，整理成作品集页面用的数据。

零依赖：只用 Python 标准库（csv / json / glob / collections），不需要 pandas。

用法：
    python analyze.py

产出：
    data/data.js    ← 页面直接引用（写成 JS 变量，绕开 file:// 下 fetch 被拦的问题）
    data/data.json  ← 同样内容，方便自己核对
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import sentiment

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def _find_crawl_output() -> Path:
    """找采集器的输出目录。

    兼容两种摆放方式：
      · analyze.py 和采集器同目录（放在同一个仓库根目录时）
      · analyze.py 在某个子目录里（本机开发时的布局）
    改过目录名也能自动认出来，不至于一搬家就崩。
    """
    candidates = [
        ROOT / "output",                              # 同目录（仓库根目录布局）
        ROOT.parent / "weibo_hotsearch" / "output",   # 本机布局
        ROOT.parent / "01_collector" / "output",
        ROOT.parent / "collector" / "output",
        ROOT.parent.parent / "weibo_hotsearch" / "output",
        ROOT.parent / "output",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


CRAWL_OUT = _find_crawl_output()


def to_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def read_csv(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def find_batch() -> Path | None:
    """找最新的一批「按话题分目录」抓取结果（topN_时间戳）。"""
    if not CRAWL_OUT.exists():
        return None
    dirs = [p for p in CRAWL_OUT.glob("top*_*") if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def ip_of(row) -> str:
    """IP 属地：优先 ip_location 列，老数据回退到 source 里的「来自 XX」。"""
    ip = (row.get("ip_location") or "").strip()
    if ip:
        return ip
    source = (row.get("source") or "").strip()
    if "来自" in source:
        tail = source.split("来自", 1)[1].strip()
        if 0 < len(tail) <= 8:
            return tail
    return ""


def hour_of(created_at: str) -> int | None:
    """'2026-10-20 13:00:46' -> 13"""
    text = (created_at or "").strip()
    if len(text) >= 13 and text[10] == " ":
        try:
            return int(text[11:13])
        except ValueError:
            return None
    return None


def main() -> int:
    batch = find_batch()
    if batch is None:
        print("找不到抓取结果。")
        print(f"  期望目录：{CRAWL_OUT}\\top<N>_<时间戳>\\")
        print("  请先跑一次 weibo_hotsearch\\run_top10.bat 或 run_check.bat。")
        return 1

    index_rows = read_csv(batch / "_index.csv")
    if not index_rows:
        print(f"{batch} 里没有 _index.csv，无法确定话题列表。")
        return 1

    topics = []
    all_posts = []

    for row in index_rows:
        folder_name = row.get("folder") or ""
        # _index.csv 的列名是 topic（不是 word），这里两种都兼容
        word = row.get("topic") or row.get("word") or ""
        folder = batch / folder_name
        if folder_name and not folder.exists():
            print(f"[警告] 缺少话题目录：{folder}（本次可能用了 --no-split）")
        comments = read_csv(folder / "comments.csv")
        posts = read_csv(folder / "posts.csv")

        hours = [0] * 24
        regions = Counter()
        verified = 0
        likes_total = 0
        sent_pos = sent_neu = sent_neg = 0
        score_sum = 0
        for c in comments:
            h = hour_of(c.get("created_at", ""))
            if h is not None:
                hours[h] += 1
            ip = ip_of(c)
            if ip:
                regions[ip] += 1
            if (c.get("user_verified") or "").strip().lower() == "true":
                verified += 1
            likes_total += to_int(c.get("likes"))

            s = sentiment.score_text(c.get("text") or "")
            score_sum += s
            lab = sentiment.label_of(s)
            if lab == "pos":
                sent_pos += 1
            elif lab == "neg":
                sent_neg += 1
            else:
                sent_neu += 1

        top_comments = sorted(comments, key=lambda c: to_int(c.get("likes")), reverse=True)[:5]

        post_rows = []
        for p in posts:
            item = {
                "mid": p.get("mid", ""),
                "text": (p.get("text") or "").strip(),
                "text_len": to_int(p.get("text_len")),
                "truncated": (p.get("truncated") or "").strip().lower() == "true",
                "author": p.get("author_name", "") or "（未补详情）",
                "source": p.get("source", ""),
                "reposts": to_int(p.get("reposts")),
                "comments": to_int(p.get("comments")),
                "attitudes": to_int(p.get("attitudes")),
                "reported": to_int(p.get("comments_reported")),
                "captured": to_int(p.get("comments_captured")),
                "coverage": to_float(p.get("coverage")),
                "url": p.get("url", ""),
            }
            post_rows.append(item)
            all_posts.append({**item, "topic": word, "rank": to_int(row.get("topic_rank"))})

        topics.append({
            "rank": to_int(row.get("topic_rank")),
            "word": word,
            "hot": to_int(row.get("hot")),
            "comment_count": len(comments),
            "post_count": len(posts),
            "hours": hours,
            "regions": [{"name": k, "count": v} for k, v in regions.most_common(8)],
            "verified_count": verified,
            "likes_total": likes_total,
            "sentiment": {
                "pos": sent_pos,
                "neu": sent_neu,
                "neg": sent_neg,
                "avg": round(score_sum / len(comments), 2) if comments else 0,
            },
            "posts": post_rows,
            "top_comments": [
                {
                    "text": (c.get("text") or "").strip(),
                    "likes": to_int(c.get("likes")),
                    "replies": to_int(c.get("reply_count")),
                    "user": c.get("user_name", ""),
                    "followers": to_int(c.get("user_followers")),
                    "ip": ip_of(c),
                    "time": c.get("created_at", ""),
                }
                for c in top_comments
            ],
        })

    # 占比：用来看「榜单热度」和「真实讨论量」的错位
    hot_sum = sum(t["hot"] for t in topics) or 1
    comment_sum = sum(t["comment_count"] for t in topics) or 1
    for t in topics:
        t["hot_share"] = round(t["hot"] / hot_sum, 4)
        t["comment_share"] = round(t["comment_count"] / comment_sum, 4)

    # 全局：按小时汇总
    total_hours = [0] * 24
    for t in topics:
        for i, n in enumerate(t["hours"]):
            total_hours[i] += n

    all_regions = Counter()
    for t in topics:
        for r in t["regions"]:
            all_regions[r["name"]] += r["count"]

    total_comments = sum(t["comment_count"] for t in topics)
    total_reported = sum(p["reported"] for p in all_posts)
    coverage = round(total_comments / total_reported, 4) if total_reported else 0
    sent_total = {
        "pos": sum(t["sentiment"]["pos"] for t in topics),
        "neu": sum(t["sentiment"]["neu"] for t in topics),
        "neg": sum(t["sentiment"]["neg"] for t in topics),
    }

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "batch": batch.name,
        "totals": {
            "topics": len(topics),
            "posts": len(all_posts),
            "comments": total_comments,
            "comments_reported": total_reported,
            "coverage": coverage,
            "likes": sum(t["likes_total"] for t in topics),
            "regions": len(all_regions),
            "sentiment": sent_total,
        },
        "hot_hours": total_hours,
        "top_regions": [{"name": k, "count": v} for k, v in all_regions.most_common(10)],
        "topics": sorted(topics, key=lambda t: t["rank"]),
        "posts": sorted(all_posts, key=lambda p: p["attitudes"], reverse=True)[:10],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    (DATA_DIR / "data.json").write_text(text, encoding="utf-8")
    (DATA_DIR / "data.js").write_text(
        "// 由 analyze.py 自动生成，请勿手改\nwindow.INSIGHT = " + text + ";\n",
        encoding="utf-8",
    )

    print(f"批次：{batch.name}")
    print(f"话题 {payload['totals']['topics']} 个 / 博文 {payload['totals']['posts']} 条 / "
          f"评论 {payload['totals']['comments']} 条")
    print(f"覆盖率：{coverage:.1%}（{total_comments}/{total_reported}）")
    sent_all = sum(sent_total.values()) or 1
    print(f"情绪：正 {sent_total['pos'] / sent_all:.0%} / "
          f"中 {sent_total['neu'] / sent_all:.0%} / "
          f"负 {sent_total['neg'] / sent_all:.0%}")
    print(f"已写出：{DATA_DIR / 'data.js'}")
    print("现在双击 index.html 就能看了。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[分析失败] {exc.__class__.__name__}: {exc}")
        sys.exit(2)
