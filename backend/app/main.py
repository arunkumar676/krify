"""Krify server. Run:  uv run python -m app.main

The side panel connects to ws://127.0.0.1:8765/ws.
Side panel → server: {"type": "task", "text"} | {"type": "stop"} | {"type": "reset"}
                     | {"type": "result", "id", "ok", "value" | "error"}
Server → side panel: {"type": "run", "id", "name", "args"}  (do a browser operation)
                     | {"type": "step" | "done" | "ask" | "stopped" | "error", "text"}
"""

import asyncio
import itertools
import logging

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.agent.loop import Agent
from app.config import PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("krify")

app = FastAPI(title="Krify")


@app.get("/")
def health():
    return {"status": "Krify server is running"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Only our browser extension may connect, not random websites.
    if not (ws.headers.get("origin") or "").startswith("chrome-extension://"):
        await ws.close(code=1008)
        return
    await ws.accept()
    log.info("Side panel connected")

    pending = {}  # operation id -> Future waiting for the browser's answer
    ids = itertools.count(1)

    async def ui(kind, text):
        await ws.send_json({"type": kind, "text": text})

    async def browser(name, args):
        op_id = next(ids)
        future = asyncio.get_running_loop().create_future()
        pending[op_id] = future
        await ws.send_json({"type": "run", "id": op_id, "name": name, "args": args})
        try:
            msg = await asyncio.wait_for(future, timeout=60)
        finally:
            pending.pop(op_id, None)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error", "Operation failed"))
        return msg.get("value")

    agent = Agent(browser, ui)
    task = None

    async def run_task(text):
        try:
            await agent.run(text)
        except Exception as err:
            log.exception("Task failed")
            await ui("error", f"Something went wrong: {err}")

    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "result":
                future = pending.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(msg)
            elif kind == "task":
                if task and not task.done():
                    await ui("error", "Krify is still working. Press Stop first.")
                else:
                    log.info("Task: %s", msg.get("text"))
                    task = asyncio.create_task(run_task(msg.get("text", "")))
            elif kind == "stop":
                agent.stopped = True
            elif kind == "reset":
                agent.reset()
    except WebSocketDisconnect:
        log.info("Side panel disconnected")
        if task:
            task.cancel()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT)
