"""OpenAI-compatible completions with resilient streaming and JSON fallback."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import requests
from fastapi.responses import StreamingResponse

log = logging.getLogger("weibo.ai")


def extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return str(payload.get("output_text") or "")
    message = choices[0].get("message") or choices[0].get("delta") or {}
    content = message.get("content") or message.get("reasoning_content") or choices[0].get("text") or ""
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def iter_sse_data(response) -> Iterator[str]:
    """Parse complete SSE data records across arbitrary chunks and line endings."""
    buffer = ""
    lines: list[str] = []

    def flush():
        nonlocal lines
        if lines:
            data = "\n".join(lines)
            lines = []
            return data
        return ""

    for chunk in response.iter_content(chunk_size=1024):
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if not line.strip():
                data = flush()
                if data:
                    yield data
            elif line.startswith("data:"):
                # Standard SSE may have multiple data lines. Flush before a new
                # record when the previous one is complete JSON (line-SSE too).
                candidate = line[5:].lstrip()
                if candidate == "[DONE]":
                    data = flush()
                    if data:
                        yield data
                    yield candidate
                    continue
                if lines and candidate.startswith("{"):
                    try:
                        json.loads("\n".join(lines))
                        data = flush()
                        if data:
                            yield data
                    except json.JSONDecodeError:
                        pass
                lines.append(candidate)
            elif line.strip() == "event:meta":
                pass
    if buffer.strip():
        line = buffer.strip()
        if line.startswith("data:"):
            lines.append(line[5:].lstrip())
    data = flush()
    if data:
        yield data


def stream_events(response) -> Iterator[dict]:
    content_type = response.headers.get("Content-Type", "").lower() if getattr(response, "headers", None) else ""
    try:
        if "application/json" in content_type:
            payload = response.json()
            text = extract_content(payload)
            if text:
                yield {"text": text}
            yield {"finish": True, "model": payload.get("model", "")}
            return
        data_iter = iter_sse_data(response)
        for data in data_iter:
            if data.strip() == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                log.warning("忽略无法解析的 AI SSE 数据：%.200s", data)
                continue
            if payload.get("error"):
                yield {"error": str(payload["error"].get("message") if isinstance(payload["error"], dict) else payload["error"])}
                return
            text = extract_content(payload)
            if text:
                yield {"text": text}
            choices = payload.get("choices") or []
            if choices and choices[0].get("finish_reason"):
                yield {"finish": True, "model": payload.get("model", ""), "usage": payload.get("usage")}
        yield {"finish": True}
    except (requests.RequestException, ValueError) as exc:
        yield {"error": f"读取 AI 响应失败：{exc}"}
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


def post_completion(base_url: str, api_key: str, payload: dict):
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "text/event-stream" if payload.get("stream") else "application/json"},
        json=payload, timeout=(15, 120), stream=bool(payload.get("stream")),
    )
    response.raise_for_status()
    return response


def summarize(text: str, topic_name: str, prompt: str, base_url: str,
              api_key: str, model: str, question: str = "", stream: bool = False):
    """Return a result dict, or a StreamingResponse with one event per SSE record."""
    if not base_url or not api_key:
        return {"ok": False, "summary": "", "error": "未配置 AI 总结功能。请在设置中填写 API Base URL 和 API Key。"}
    question = question.strip()
    if question:
        system = f"你是超话内容问答助手。只能依据下面帖子回答，不知道就明确说明。\n\n帖子样本：\n{text[:24000]}"
        user = question
    else:
        system = prompt.replace("{topic_name}", topic_name)
        user = f"以下是「{topic_name}」超话的最新帖子内容：\n\n{text[:24000]}"
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "max_tokens": 1600, "temperature": 0.4, "stream": stream}
    try:
        response = post_completion(base_url, api_key, payload)
        if stream:
            def generate():
                emitted_text = False
                emitted_error = False
                for event in stream_events(response):
                    emitted_text = emitted_text or bool(event.get("text"))
                    emitted_error = emitted_error or bool(event.get("error"))
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not emitted_text and not emitted_error:
                    log.warning("AI 流式响应没有正文 topic=%s model=%s", topic_name, model)
                    yield f"data: {json.dumps({'error': 'AI 服务没有返回正文，请稍后重试'}, ensure_ascii=False)}\n\n"
            return StreamingResponse(generate(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        result = response.json()
        summary = extract_content(result).strip()
        if not summary:
            return {"ok": False, "summary": "", "error": "AI 服务未返回有效内容，请稍后重试"}
        return {"ok": True, "summary": summary, "model": result.get("model", model), "qa_mode": bool(question)}
    except requests.Timeout:
        return {"ok": False, "summary": "", "error": "AI 请求超时，请稍后重试"}
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "未知"
        return {"ok": False, "summary": "", "error": f"AI 服务返回 HTTP {status}，请检查接口和模型配置"}
    except requests.RequestException as exc:
        return {"ok": False, "summary": "", "error": f"AI 网络请求失败：{exc}"}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "summary": "", "error": f"AI 响应格式异常：{exc}"}
