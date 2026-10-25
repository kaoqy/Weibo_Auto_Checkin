"""Local integration server used only by browser smoke tests."""
import json
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

os.environ["WCM_BROWSER_TEST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import database
from app.main import app

upstream = FastAPI()


@upstream.get("/api/config")
def config():
    return {"data": {"login": True, "st": "test-st"}}


@upstream.get("/api/container/getIndex")
def topics(containerid: str = ""):
    if containerid == "100803_-_followsuper":
        return {"ok": 1, "data": {"cards": [{"card_group": [{"card_type": 8, "title_sub": "测试超话", "scheme": "sinaweibo://page/?containerid=100808test", "buttons": [{"name": "签到", "scheme": "/checkin"}], "desc": "集成测试"}]}], "cardlistInfo": {"since_id": ""}}}
    return {"ok": 1, "data": {"cards": [{"mblog": {"id": "1001", "text": "今天讨论新舞台 #巡演#", "created_at": "2026-10-25 12:00:00", "attitudes_count": 88, "comments_count": 12, "user": {"id": 1, "screen_name": "测试作者"}}}], "cardlistInfo": {"since_id": ""}}}


@upstream.get("/checkin")
def checkin(st: str = ""):
    return {"ok": 1, "msg": "签到成功", "data": {"rank": 1}}


@upstream.post("/v1/chat/completions")
def completion(payload: dict):
    if payload.get("stream"):
        def stream():
            for text in ("## 内容概览\n", "本期讨论集中在新舞台。", "\n\n## 热门话题\n- 巡演舞台获得关注"):
                yield f"data: {json.dumps({'choices':[{'delta':{'content':text}}]}, ensure_ascii=False)}\n\n"
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"model":"browser-test"}\n\n'
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")
    return {"model": "browser-test", "choices": [{"message": {"content": "测试连接成功"}}]}


def run_upstream():
    uvicorn.run(upstream, host="127.0.0.1", port=8877, log_level="warning")


if __name__ == "__main__":
    import threading
    from app import weibo_client
    threading.Thread(target=run_upstream, daemon=True).start()
    database.DB_PATH = Path(os.environ.get("TEMP", ".")) / "weibo-browser-smoke.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(database.DB_PATH) + suffix).unlink(missing_ok=True)
    database.init_db()
    database.set_settings({"ai_base_url": "http://127.0.0.1:8877/v1", "ai_api_key": "browser-test", "schedule_enabled": "0", "cookie_auto_refresh": "0"})
    database.upsert_all_topic("100808test", name="测试超话", description="集成测试")
    weibo_client.BASE = "http://127.0.0.1:8877"
    weibo_client.CONFIG_URL = weibo_client.BASE + "/api/config"
    weibo_client.TOPICS_URL = weibo_client.BASE + "/api/container/getIndex"
    uvicorn.run(app, host="127.0.0.1", port=8876, log_level="warning")
