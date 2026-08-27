"""OpenAI-compatible adapter for the local browser-backed Gemini lab."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


UPSTREAM = os.environ.get("GEMINI_LAB_BASE_URL", "http://127.0.0.1:3346").rstrip("/")
PROJECT = os.environ.get("GEMINI_LAB_PROJECT", "lab")
LOCATION = os.environ.get("GEMINI_LAB_LOCATION", "us-central1")

app = FastAPI(title="Gemini Vertex adapter", docs_url=None, redoc_url=None)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return ""


def _vertex_request(body: dict[str, Any]) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, str]] = []
    for message in body.get("messages", []):
        role = message.get("role", "user")
        text = _text(message.get("content"))
        if not text:
            continue
        if role in {"system", "developer"}:
            system_parts.append({"text": text})
        else:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]})

    generation: dict[str, Any] = {}
    if "max_tokens" in body:
        generation["maxOutputTokens"] = body["max_tokens"]
    if "max_completion_tokens" in body:
        generation["maxOutputTokens"] = body["max_completion_tokens"]
    for source, target in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        if source in body:
            generation[target] = body[source]

    request: dict[str, Any] = {"contents": contents, "generationConfig": generation}
    if system_parts:
        request["systemInstruction"] = {"parts": system_parts}
    return request


def _url(model: str, stream: bool) -> str:
    model = model.removeprefix("models/")
    endpoint = "streamGenerateContent" if stream else "generateContent"
    suffix = "?alt=sse" if stream else ""
    return f"{UPSTREAM}/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{model}:{endpoint}{suffix}"


def _openai_response(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    candidate = (payload.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    content = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    usage = payload.get("usageMetadata", {})
    return {
        "id": f"chatcmpl-gemini-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model.removeprefix("models/"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
            "total_tokens": usage.get("totalTokenCount", 0),
        },
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="model is required")
    stream = bool(body.get("stream"))
    payload = _vertex_request(body)

    # Intentionally send only Content-Type. The upstream authenticates internally.
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(_url(model, stream), json=payload, headers={"Content-Type": "application/json"})
    if response.status_code >= 400:
        return JSONResponse(status_code=response.status_code, content=response.json())
    if not stream:
        return JSONResponse(_openai_response(model, response.json()))

    async def chunks() -> AsyncIterator[str]:
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            try:
                text = _openai_response(model, json.loads(data))["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
            if text:
                chunk = {"id": f"chatcmpl-gemini-{uuid.uuid4().hex}", "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")
