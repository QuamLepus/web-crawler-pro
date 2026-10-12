"""Real-time operations dashboard (FastAPI + WebSockets).

Displays live crawl metrics: frontier depth, throughput, worker status,
recent items, and Prometheus-style counters. Run:

    uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from src.config import get_config
from src.frontier.url_frontier import URLFrontier

app = FastAPI(title="Web Crawler Pro — Ops Dashboard")
cfg = get_config()

_HTML = """<!doctype html>
<html><head><title>Crawler Ops</title>
<style>
 body{font-family:system-ui;background:#0b1220;color:#e6edf3;margin:0;padding:24px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
 .card{background:#151d2e;border:1px solid #2a3550;border-radius:12px;padding:16px}
 .num{font-size:2em;font-weight:700;color:#58a6ff}
 h1{font-size:1.2em} #log{font-family:monospace;font-size:.85em;color:#8b949e;
 white-space:pre-wrap;max-height:40vh;overflow:auto;background:#101626;padding:12px;
 border-radius:8px;margin-top:16px}
</style></head><body>
<h1>🕷️ Web Crawler Pro — Live Ops</h1>
<div class="grid">
 <div class="card"><div>Pending URLs</div><div class="num" id="pending">–</div></div>
 <div class="card"><div>In-flight</div><div class="num" id="inflight">–</div></div>
 <div class="card"><div>Pages crawled</div><div class="num" id="crawled">0</div></div>
 <div class="card"><div>Errors</div><div class="num" id="errors">0</div></div>
</div>
<pre id="log">connecting…</pre>
<script>
let crawled=0, errors=0;
const ws=new WebSocket(`ws://${location.host}/ws/metrics`);
ws.onmessage=(e)=>{const d=JSON.parse(e.data);
 pending.textContent=d.pending; inflight.textContent=d.in_flight;
 crawled+=d.delta||0; errors+=d.errors||0;
 crawledEl.textContent=crawled; errorsEl.textContent=errors;
 log.textContent+=`\\n[${d.time}] ${d.message}`; log.scrollTop=log.scrollHeight;};
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


def _snapshot() -> dict:
    try:
        frontier = URLFrontier(cfg.REDIS_URL)
        stats = frontier.stats()
    except Exception as exc:  # noqa: BLE001 - redis down -> zeros
        stats = {"pending": 0, "in_flight": 0, "seen": 0, "error": str(exc)}
    stats["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return stats


@app.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_text(json.dumps(_snapshot()))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass


@app.get("/api/stats")
async def api_stats():
    return _snapshot()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
