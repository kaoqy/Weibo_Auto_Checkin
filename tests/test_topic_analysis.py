from app.topic_analysis import build_analysis_context, filter_posts, prepare_posts
from app.weibo_client import fetch_topic_posts


def post(mid, text, likes=0, repost=False):
    return {"mid": mid, "text": text, "created_at": "2026-10-25 10:00:00",
            "user": {"id": "1", "screen_name": "测试者"}, "attitudes_count": likes,
            "retweeted": {"text": "原博"} if repost else None}


def test_analysis_deduplicates_and_keeps_engagement():
    prepared = prepare_posts([post("1", "新舞台 #巡演#", 2), post("2", "新舞台 #巡演#", 5), post("3", "新专辑", 9)])
    assert len(prepared) == 2
    assert prepared[0]["mid"] == "3"
    assert prepared[0]["engagement"] == 9


def test_analysis_filter_by_keyword_and_repost():
    posts = [post("1", "讨论新专辑"), post("2", "其他", repost=True)]
    filtered = filter_posts(posts, keyword="专辑", hide_reposts=True)
    assert [p["mid"] for p in filtered] == ["1"]


def test_analysis_context_includes_stats_and_source_ids():
    text, result = build_analysis_context([post("123", "#巡演# 新舞台", 8)], "测试超话")
    assert "ID=123" in text and "巡演" in text
    assert result["stats"]["posts"] == 1
    assert result["stats"]["hashtags"] == [{"name": "巡演", "count": 1}]


def test_fetch_pages_stops_on_repeated_cursor(monkeypatch):
    monkeypatch.setattr("app.weibo_client.time.sleep", lambda _: None)
    def fake_request(session, method, url, **kwargs):
        return {"items": [{"data": {"id": "1", "text": "测试", "user": {}}}],
                "data": {"cardlistInfo": {"since_id": "same"}}}
    monkeypatch.setattr("app.weibo_client.request_json", fake_request)
    result = fetch_topic_posts(__import__("requests").Session(), {}, "100808abc", count=30, max_pages=5)
    assert result["pages"] == 2
    assert len(result["posts"]) == 1
    assert "游标重复" in result["error"]
