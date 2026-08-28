"""Header-clean OpenAI-compatible proxy for the local ChatGPT lab."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


UPSTREAM = os.environ.get("CHATGPT_LAB_BASE_URL", "http://127.0.0.1:3346").rstrip("/")
app = FastAPI(title="ChatGPT lab passthrough", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request):
    """Relay the OpenAI request without LiteLLM's upstream auth headers."""
    payload = await request.json()
    headers = {"Content-Type": "application/json"}

    if not payload.get("stream"):
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{UPSTREAM}/v1/chat/completions", json=payload, headers=headers
            )
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text, "type": "upstream_error"}}
        return JSONResponse(status_code=response.status_code, content=body)

    async def stream() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream(
                "POST", f"{UPSTREAM}/v1/chat/completions", json=payload, headers=headers
            ) as response:
                async for chunk in response.aiter_raw():
                    yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")
