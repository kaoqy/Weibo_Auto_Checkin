import json

from app.ai_service import extract_content, iter_sse_data, stream_events, summarize


class FakeResponse:
    def __init__(self, chunks, content_type="text/event-stream", payload=None):
        self.chunks = chunks
        self.headers = {"Content-Type": content_type}
        self.payload = payload
        self.closed = False

    def iter_content(self, chunk_size=1024):
        yield from self.chunks

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


def test_sse_parser_handles_crlf_split_chunks_and_unterminated_tail():
    first = 'data: {"choices":[{"delta":{"content":"摘要"}}]}\r\n\r\n'.encode()
    second = 'data: {"choices":[{"delta":{"content":"正文"}}]}\r\n\r\ndata: [DONE]'.encode()
    chunks = [first[:42], first[42:], second[:38], second[38:]]
    data = list(iter_sse_data(FakeResponse(chunks)))
    assert len(data) == 3
    assert json.loads(data[0])["choices"][0]["delta"]["content"] == "摘要"
    assert json.loads(data[1])["choices"][0]["delta"]["content"] == "正文"


def test_stream_json_fallback_returns_visible_text():
    response = FakeResponse([], "application/json", {"model": "test", "choices": [{"message": {"content": "完整总结"}}]})
    events = list(stream_events(response))
    assert events[0] == {"text": "完整总结"}
    assert events[1]["model"] == "test"
    assert response.closed


def test_stream_supports_content_parts_and_reasoning_fallback():
    assert extract_content({"choices": [{"delta": {"content": [{"text": "甲"}, {"text": "乙"}]}}]}) == "甲乙"
    assert extract_content({"choices": [{"delta": {"reasoning_content": "思考"}}]}) == "思考"


def test_summarize_stream_emits_json_fallback_as_sse(monkeypatch):
    response = FakeResponse([], "application/json", {"model": "test", "choices": [{"message": {"content": "降级总结"}}]})
    monkeypatch.setattr("app.ai_service.post_completion", lambda *args: response)
    result = summarize("帖子", "超话", "总结", "https://ai.example/v1", "key", "model", stream=True)
    import asyncio
    async def read_body():
        chunks = []
        async for chunk in result.body_iterator:
            chunks.append(chunk)
        return b"".join(chunk.encode() if isinstance(chunk, str) else chunk for chunk in chunks).decode()
    body = asyncio.run(read_body())
    assert '"text": "降级总结"' in body
    assert result.headers["x-accel-buffering"] == "no"
