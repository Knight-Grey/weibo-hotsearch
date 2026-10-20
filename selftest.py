"""离线自检：不联网，用假数据把解析、分页、去重、导出跑一遍。

用途：装好环境后先跑这个，确认代码本身没问题（不需要 Cookie，也不会碰微博服务器）：

    python selftest.py

覆盖：
    1. 热搜榜单解析
    2. 搜索页 HTML -> 博文 mid 提取
    3. 主评论分页：游标推进、结束判定、重复数据去重
    4. 楼中楼：内嵌回复 + 分页补齐，且分页 id 必须是主评论 id
    5. 时间 / 点赞 / IP 属地字段
    6. CSV / JSON 导出

假客户端按「请求内容」而不是「调用次序」分发响应，避免测试与实现顺序耦合导致假通过。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import config
import scraper
from scraper import WeiboClient

POST_MID = "1111111111111111"
PASSED = 0
FAILED: List[str] = []

# Windows 控制台默认 GBK，中文会乱码，这里统一切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


class FakeResponse:
    """最小化的 requests.Response 替身。"""

    def __init__(self, payload: Any, text: str = "") -> None:
        self._payload = payload
        self.text = text if text else json.dumps(payload, ensure_ascii=False)
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


HOT_PAYLOAD: Dict[str, Any] = {
    "ok": 1,
    "data": {
        # 按 2024 年实测的真实结构：rank 是 0 基、realpos 是榜上名次（广告位为 None）、
        # num 是热度、word_scheme 才是该拿去搜索的表达式
        "realtime": [
            {"rank": 0, "realpos": 1, "word": "测试话题一", "num": 1234567,
             "label_name": "沸", "word_scheme": "#测试话题一#"},
            {"rank": 1, "realpos": 2, "word": "测试话题二", "num": 999999,
             "label_name": "新", "word_scheme": "测试话题二"},
            {"rank": 2, "realpos": None, "word": "广告位词条", "num": 888888,
             "label_name": "商", "word_scheme": "#广告位词条#"},
            {"rank": 3, "word": "旧字段词条", "raw_hot": 777777,
             "category": "社会", "word_scheme": "#旧字段词条#"},
        ]
    },
}

SEARCH_HTML = """
<div class="card-wrap" action-type="feed_list_item" mid="1111111111111111">
  <p class="txt" node-type="feed_list_content">第一条微博内容</p>
</div>
<div class="card-wrap" action-type="feed_list_item" mid="2222222222222222">
  <p class="txt" node-type="feed_list_content_full">第二条微博内容（长文）</p>
  <p class="txt">转发理由，不应被当成正文</p>
</div>
"""


def fake_comment(cid: str, text: str, likes: int = 1, replies: int = 0, with_cursor: bool = False) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "idstr": cid,
        "text_raw": text,
        "created_at": "Wed Mar 06 12:00:00 +0800 2024",
        "like_count": likes,
        "total_number": replies,
        "ip_location": "北京",
        "user": {"idstr": "u" + cid, "screen_name": "用户" + cid, "followers_count": 10},
    }
    if with_cursor:  # 楼中楼还有下一页时，主评论对象上会带 max_id / max_id_type
        item["max_id"] = "9500"
        item["max_id_type"] = "0"
    return item


class FakeClient(WeiboClient):
    """替换网络层，按请求内容返回预设数据。"""

    def __init__(self) -> None:
        super().__init__(cookie="SUB=fake", delay=0, jitter=0, max_retries=1, timeout=1)
        self.post_calls: List[Dict[str, Any]] = []
        self.get_calls: List[Dict[str, Any]] = []

    def _comment_payload(self, target: str, max_id: str) -> Dict[str, Any]:
        """按 (id, max_id) 返回假评论数据；GET / POST 两条路共用同一份。"""
        # 楼中楼分页：id 必须是主评论 id，且携带游标
        if target == "1002":
            if max_id == "9500":
                return {
                    "ok": 1,
                    "data": [fake_comment("2002", "楼中楼回复B2", with_cursor=True)],
                    "max_id": "0",
                    "max_id_type": "0",
                }
            return {"ok": 1, "data": [], "max_id": "0", "max_id_type": "0"}

        # 主评论分页：id 是微博 mid
        if target == POST_MID:
            if max_id == "0":  # 第一页
                return {
                    "ok": 1,
                    "data": [
                        # 1001 的楼中楼已经内嵌在接口返回里（fetch_level=1 的效果）
                        {**fake_comment("1001", "主评论A", likes=5, replies=1),
                         "comments": [fake_comment("2001", "楼中楼回复A1")]},
                        fake_comment("1002", "主评论B", likes=2, replies=1, with_cursor=True),
                    ],
                    "total_number": 2,
                    "max_id": "8001",
                    "max_id_type": "0",
                }
            if max_id == "8001":  # 第二页：故意混入重复的 1001
                return {
                    "ok": 1,
                    "data": [fake_comment("1001", "主评论A（重复数据）"), fake_comment("1003", "主评论C")],
                    "total_number": 3,
                    "max_id": "0",
                    "max_id_type": "0",
                }

        return {"ok": 1, "data": [], "max_id": "0", "max_id_type": "0"}

    # --- 覆盖网络入口（只覆盖这两个，其余走真实实现）---------------------
    def _request(self, method, url, **kwargs):  # type: ignore[override]
        params = kwargs.get("params") or {}
        self.get_calls.append({"method": method, "url": url, "params": dict(params),
                               "headers": dict(kwargs.get("headers") or {})})
        if url == scraper.HOT_SEARCH:
            return FakeResponse(HOT_PAYLOAD)
        if url == scraper.BUILD_COMMENTS:
            # 评论接口现在走 GET：POST 会被新网关 404 掉
            return FakeResponse(self._comment_payload(str(params.get("id") or ""),
                                                     str(params.get("max_id") or "0")))
        # 话题搜索页：返回服务端渲染的 HTML
        return FakeResponse({}, text=SEARCH_HTML)

    def _post_json(self, url, data, referer):  # type: ignore[override]
        self.post_calls.append({"data": dict(data), "referer": referer})
        return self._comment_payload(str(data.get("id") or ""), str(data.get("max_id") or "0"))


def main() -> int:
    print("微博爬虫离线自检\n" + "=" * 50)
    client = FakeClient()

    # ---------------------------------------------------------------- 1
    print("\n[1] 热搜榜解析（真实字段结构）")
    items = client.fetch_hot_search()
    check("自动剔除广告位", len(items) == 3, f"实际 {len(items)}: {[i.word for i in items]}")
    if len(items) == 3:
        check("名次按非广告条目顺序编号（1 起）", items[0].rank == 1 and items[1].rank == 2,
              f"{items[0].rank}/{items[1].rank}")
        check("第 1 名话题正确", items[0].word == "测试话题一", items[0].word)
        check("num 解析成热度", items[0].raw_hot == 1234567, str(items[0].raw_hot))
        check("word_scheme 用于搜索", items[0].query == "#测试话题一#", items[0].query)
        check("搜索链接带话题井号", unquote(items[0].topic_url).endswith("q=#测试话题一#"),
              unquote(items[0].topic_url))
        check("旧字段 raw_hot / rank 仍兼容", items[2].raw_hot == 777777 and items[2].rank == 3,
              f"{items[2].raw_hot}/{items[2].rank}")
    with_ads = client.fetch_hot_search(include_ads=True)
    check("include_ads=True 保留广告位", len(with_ads) == 4, f"实际 {len(with_ads)}")
    hot_calls = [c for c in client.get_calls if c["url"] == scraper.HOT_SEARCH]
    check("热搜请求已发出", len(hot_calls) == 2, f"实际 {len(hot_calls)} 次")
    check("热搜走匿名请求（不带 Cookie）", all("Cookie" not in c["headers"] for c in hot_calls),
          str([sorted(c["headers"].keys()) for c in hot_calls]))

    # ---------------------------------------------------------------- 2
    print("\n[2] 搜索页 HTML 解析")
    posts = client.fetch_topic_posts("测试话题一", max_pages=1)
    check("经 fetch_topic_posts 拿到博文", len(posts) == 2, f"实际 {len(posts)}")
    check("搜索词被包成话题形式", any(c["params"].get("q") == "#测试话题一#" for c in client.get_calls),
          str([c["params"].get("q") for c in client.get_calls]))
    posts = client._parse_search_page(SEARCH_HTML, "测试话题一")
    check("博文条数正确", len(posts) == 2, f"实际 {len(posts)}")
    if len(posts) == 2:
        check("普通正文解析正确", posts[0]["text"] == "第一条微博内容", posts[0]["text"])
        check("mid 解析正确", posts[0]["mid"] == POST_MID, posts[0]["mid"])
        check("长文正文解析正确", posts[1]["text"] == "第二条微博内容（长文）", posts[1]["text"])
        check("未混入转发理由", "转发理由" not in posts[1]["text"], posts[1]["text"])

    # 匿名访问时微博会 302 到 passport 访客页（200 但无内容）——必须报明确错误而不是静默 0 条
    class VisitorRedirectClient(FakeClient):
        def _request(self, method, url, **kwargs):  # type: ignore[override]
            resp = FakeResponse({}, text="<html>visitor</html>")
            resp.url = "https://passport.weibo.com/visitor/visitor?a=enter"
            return resp

    try:
        VisitorRedirectClient().fetch_topic_posts("测试话题一", max_pages=1)
        check("访客重定向被识别为未登录", False, "没有抛出 LoginRequired")
    except scraper.LoginRequired:
        check("访客重定向被识别为未登录", True)
    except Exception as exc:  # noqa: BLE001
        check("访客重定向被识别为未登录", False, f"抛出了 {exc.__class__.__name__}")

    # ---------------------------------------------------------------- 3
    print("\n[3] 评论分页 + 去重")
    comments = client.fetch_post_comments(
        post_id=POST_MID,
        topic="测试话题一",
        topic_rank=1,
        max_pages=0,          # 不限页数，靠游标结束
        fetch_replies=True,
        max_reply_pages=2,
    )
    roots = [c for c in comments if not c.is_reply]
    replies = [c for c in comments if c.is_reply]
    root_ids = [c.comment_id for c in roots]
    reply_ids = [c.comment_id for c in replies]
    all_ids = [c.comment_id for c in comments]

    check("主评论去重后 3 条", root_ids == ["1001", "1002", "1003"], str(root_ids))
    check("重复的 1001 只保留一条", all_ids.count("1001") == 1, str(all_ids))
    check("楼中楼 2 条", reply_ids == ["2001", "2002"], str(reply_ids))
    check("全部 comment_id 唯一", len(all_ids) == len(set(all_ids)), str(all_ids))
    comment_gets = [c for c in client.get_calls if c["url"] == scraper.BUILD_COMMENTS]
    check("主评论翻页 2 次即结束", sum(1 for c in comment_gets if c["params"].get("id") == POST_MID) == 2,
          str([c["params"].get("id") for c in comment_gets]))
    check("评论接口走 GET（POST 会被网关 404）", len(comment_gets) >= 2 and not client.post_calls,
          f"GET {len(comment_gets)} 次 / POST {len(client.post_calls)} 次")

    # ---------------------------------------------------------------- 4
    print("\n[4] 楼中楼分页参数")
    reply_calls = [c for c in client.get_calls
                   if c["url"] == scraper.BUILD_COMMENTS and c["params"].get("id") == "1002"]
    check("楼中楼用主评论 id 发起请求", len(reply_calls) == 1, f"实际 {len(reply_calls)} 次")
    if reply_calls:
        check("楼中楼分页带正确游标", reply_calls[0]["params"].get("max_id") == "9500",
              str(reply_calls[0]["params"].get("max_id")))
        check("楼中楼 referer 带 type=comment",
              "type=comment" in str(reply_calls[0]["headers"].get("Referer", "")),
              str(reply_calls[0]["headers"]))
    check("回复行都标了 is_reply", all(c.is_reply for c in replies) and len(replies) == 2, str(len(replies)))
    check("回复行各自挂到正确的主评论", [c.root_comment_id for c in replies] == ["1001", "1002"],
          str([c.root_comment_id for c in replies]))

    # ---------------------------------------------------------------- 5
    print("\n[5] 字段解析")
    first: Optional[scraper.Comment] = roots[0] if roots else None
    check("时间转成北京时间", bool(first) and first.created_at == "2024-03-06 12:00:00",
          first.created_at if first else "无数据")
    check("点赞数正确", bool(first) and first.likes == 5, str(first.likes) if first else "无数据")
    check("IP 属地正确", bool(first) and first.ip_location == "北京", first.ip_location if first else "无数据")
    fallback_ip = scraper.extract_ip_location({"source": "来自 江西"})
    check("ip_location 缺失时从 source 兜底", fallback_ip == "江西", fallback_ip or "没抠出来")
    check("话题与榜位回填正确", bool(first) and first.topic == "测试话题一" and first.topic_rank == 1,
          f"{first.topic}/{first.topic_rank}" if first else "无数据")
    check("非回复行 root_comment_id 为空", all(c.root_comment_id == "" for c in roots),
          str([c.root_comment_id for c in roots]))

    # ---------------------------------------------------------------- 6
    print("\n[6] 导出")
    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp)
        rows = [c.as_row() for c in comments]
        csv_path = outdir / "t.csv"
        json_path = outdir / "t.json"
        scraper.export_csv(rows, csv_path)
        scraper.export_json(rows, json_path, {"n": len(rows)})

        csv_text = csv_path.read_text(encoding=config.OUTPUT_ENCODING) if csv_path.exists() else ""
        csv_rows = [line for line in csv_text.splitlines() if line.strip()]
        check("CSV 行数 = 表头 + 数据行", len(csv_rows) == len(rows) + 1, f"实际 {len(csv_rows)} 行")
        check("CSV 含表头 comment_id", csv_rows and csv_rows[0].startswith("comment_id"), csv_rows[0][:40] if csv_rows else "空")
        check("CSV 含楼中楼文本", "楼中楼回复B2" in csv_text, "未找到")

        data = json.loads(json_path.read_text(encoding="utf-8"))
        check("JSON 条数正确", data.get("count") == len(rows), str(data.get("count")))
        check("JSON items 完整", len(data.get("items") or []) == len(rows), str(len(data.get("items") or [])))
        check("JSON 保留 meta", (data.get("meta") or {}).get("n") == len(rows), str(data.get("meta")))

    print("\n" + "=" * 50)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("全部通过，代码本身可用；联网抓取还需要有效的 Cookie。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 自检脚本自身出错也要给出可读结论
        print(f"\n[自检异常] {exc.__class__.__name__}: {exc}")
        sys.exit(2)
