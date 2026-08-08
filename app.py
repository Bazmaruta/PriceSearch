"""PriceSearch web app — FastAPI server with a live, streaming search UI.

Run:  python app.py        (or:  uvicorn app:app --reload)
Visit: http://localhost:8000

The page itself renders instantly (a shell); results then drip-feed in over
``/api/search/stream`` (SSE) as the parallel per-store searches and thumbnail
enrichment complete, so the first products appear in a few seconds instead of
after the whole search finishes.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from pricesearch import engine, render, stream

app = FastAPI(title="PriceSearch", description="Gemini-grounded grocery price search engine")


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str | None = None, model: str | None = None, category: str | None = None, mode: str | None = None, stores: list[str] | None = None) -> str:
    return render.render_shell(q or "", model, category, mode, ",".join(stores) if stores else None)


class SearchRequest(BaseModel):
    query: str
    stores: list[str] | None = None
    model: str | None = None
    category: str | None = None
    mode: str | None = None


@app.post("/api/search")
def api_search(body: SearchRequest) -> dict:
    result = engine.search(body.query, stores=body.stores, model=body.model, category=body.category, mode=body.mode)
    return result


def _sse(events) -> StreamingResponse:
    def generator():
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _parse_stores(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


@app.get("/api/search/stream")
def api_search_stream(q: str, model: str | None = None, category: str | None = None, mode: str | None = None, stores: str | None = None) -> StreamingResponse:
    return _sse(stream.search_stream(q, stores=_parse_stores(stores), model=model, category=category, mode=mode))


if __name__ == "__main__":
    import os
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
