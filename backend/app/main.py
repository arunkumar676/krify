"""Krify server. Run:  uv run python -m app.main

The side panel connects to ws://<server>:8765/ws?client=<install id>. The server listens on HOST
(.env; 127.0.0.1 = this PC only, 0.0.0.0 = other PCs on the network too). `client` is a random id
each extension install keeps, so task history is per person.
Side panel → server: {"type": "task", "text", "model", "location": {"country", "place"}} | {"type": "stop"} | {"type": "reset"}
                     | {"type": "result", "id", "ok", "value" | "error"}
                     | {"type": "confirm_result", "id", "allow"}
                     | {"type": "plan_review_result", "id", "action": "approve"|"update"|"reject", "steps"}
                     | {"type": "history"} | {"type": "history_delete", "text"} | {"type": "history_clear"}
                     | {"type": "dev_mode", "on"}  (debug trace appended to the final answer)
                     | {"type": "ai_view"}  (show the page view the AI would get right now)
                     | {"type": "llm_report", "task_id"?}  (every AI request of a task; default = your latest task)
Server → side panel: {"type": "models", "models": [{"id", "label", "status"}]}  (on connect + after each task)
                     | {"type": "run", "id", "name", "args"}  (do a browser operation)
                     | {"type": "confirm", "id", "text"}  (ask the user Allow / Block)
                     | {"type": "plan_review", "id", "steps"}  (ask the user to Approve / Update / Reject a plan)
                     | {"type": "history", "items": [{"id", "text", "status", "summary", "tokens", "calls",
                       "prompt_tokens", "completion_tokens", "created_at"}]}
                     | {"type": "ai_view", "ok", "text" | "error"}
                     | {"type": "llm_report", "ok", "report" {task, totals, by_model, by_purpose, calls}, "markdown" | "error"}
                     | {"type": "step" | "done" | "ask" | "stopped" | "error", "text"}
"""

import asyncio
import itertools
import logging
import re
import socket

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app import history
from app.agent.loop import Agent
from app.config import HOST, MODELS, PORT, PROVIDERS, provider_of
from app.llm import report
from app.llm.client import model_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("krify")

app = FastAPI(title="Krify")

# Each tab's Krify panel has its own connection (its own chat), but only one task runs at a time:
# free AI limits are shared, and two agents would fight over the browser.
running = {"task": None}

CLIENT_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
legacy_claimed = {"done": False}  # history saved before it was per person goes to this PC's own panel


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
    client = ws.query_params.get("client") or ""
    if not CLIENT_ID.match(client):
        client = "unknown"
    peer = ws.client.host if ws.client else "?"
    log.info("Side panel connected from %s (client %s)", peer, client[:8])
    if peer in LOOPBACK and client != "unknown" and not legacy_claimed["done"]:
        history.claim_legacy(client)
        legacy_claimed["done"] = True

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

    confirms = {}  # confirm id -> Future[bool]

    async def confirm(text):
        """Ask the user Allow / Block. No answer in 2 minutes counts as Block."""
        confirm_id = next(ids)
        future = asyncio.get_running_loop().create_future()
        confirms[confirm_id] = future
        log.info("[confirm] %s", text)
        await ws.send_json({"type": "confirm", "id": confirm_id, "text": text})
        try:
            return await asyncio.wait_for(future, timeout=120)
        except asyncio.TimeoutError:
            return False
        finally:
            confirms.pop(confirm_id, None)

    plan_reviews = {}  # plan review id -> Future[dict] (the raw plan_review_result message)

    async def plan_review(steps):
        """Show the proposed plan (editable) and wait for Approve / Update / Reject.
        No answer in 10 minutes counts as Reject – never silently proceed on an unapproved plan."""
        review_id = next(ids)
        future = asyncio.get_running_loop().create_future()
        plan_reviews[review_id] = future
        await ws.send_json({"type": "plan_review", "id": review_id, "steps": steps})
        try:
            msg = await asyncio.wait_for(future, timeout=600)
            edited = [str(s).strip() for s in (msg.get("steps") or []) if str(s).strip()]
            return msg.get("action", "reject"), edited or steps
        except asyncio.TimeoutError:
            return "reject", steps
        finally:
            plan_reviews.pop(review_id, None)

    agent = Agent(browser, ui, confirm, plan_review, client=client)
    task = None

    async def send_models():
        """Model list for the picker, with today's usage next to each one."""
        await ws.send_json(
            {
                "type": "models",
                "models": [
                    {
                        "id": m,
                        "label": f"{PROVIDERS[provider_of(m)]['name']} · {info['label']}",
                        "status": model_status(m),
                    }
                    for m, info in MODELS.items()
                ],
            }
        )

    async def run_task(text, model, location):
        try:
            await agent.run(text, model, location)
        except asyncio.CancelledError:  # Stop button
            agent.close_open_calls()
            await agent.finish("stopped", "Stopped by you.")
        except RuntimeError as err:  # friendly errors we raise ourselves
            agent.close_open_calls()
            await agent.finish("error", str(err))
        except Exception as err:
            agent.close_open_calls()
            log.exception("Task failed")
            await agent.finish("error", f"Something went wrong: {err}")
        finally:
            agent.task_id = None
            try:
                await send_models()  # refresh usage in the picker
            except Exception:
                pass  # panel already gone

    async def send_ai_view():
        """The page view the AI would get right now (manual test button). Read-only: no task state."""
        try:
            text = (await agent.look())[0]
            reply = {"type": "ai_view", "ok": True, "text": text}
        except Exception as err:
            reply = {"type": "ai_view", "ok": False, "error": str(err)}
        try:
            await ws.send_json(reply)
        except Exception:
            pass  # panel already gone

    await send_models()

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
                elif running["task"] and not running["task"].done():
                    await ui("error", "Krify is busy with another task (in another tab, or another person on the "
                             "network is using it). Try again when it finishes.")
                else:
                    model = msg.get("model") if msg.get("model") in MODELS else next(iter(MODELS))
                    log.info("Task (%s): %s", model, msg.get("text"))
                    location = msg.get("location") if isinstance(msg.get("location"), dict) else None
                    task = asyncio.create_task(run_task(msg.get("text", ""), model, location))
                    running["task"] = task
            elif kind == "confirm_result":
                future = confirms.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(bool(msg.get("allow")))
            elif kind == "plan_review_result":
                future = plan_reviews.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(msg)
            elif kind == "stop":
                if task and not task.done():
                    task.cancel()  # ends the task right away, even while waiting
            elif kind == "reset":
                agent.reset()
            elif kind == "dev_mode":
                agent.dev_mode = bool(msg.get("on"))
            elif kind == "ai_view":
                # Its own task: look() waits on a browser "result" that this same loop must receive.
                asyncio.create_task(send_ai_view())
            elif kind == "llm_report":
                task_id = msg.get("task_id")
                task_id = task_id if isinstance(task_id, int) else report.latest_task_id(client)
                data = report.task_report(task_id, client) if task_id else None  # only this client's own tasks
                if data:
                    await ws.send_json({"type": "llm_report", "ok": True, "report": data,
                                        "markdown": report.task_report_markdown(data)})
                else:
                    await ws.send_json({"type": "llm_report", "ok": False, "error": "No report for that task."})
            elif kind == "history":
                await ws.send_json({"type": "history", "items": history.recent(client)})
            elif kind == "history_delete":
                history.delete_text(msg.get("text", ""), client)
                await ws.send_json({"type": "history", "items": history.recent(client)})
            elif kind == "history_clear":
                history.clear(client)
                await ws.send_json({"type": "history", "items": []})
    except WebSocketDisconnect:
        log.info("Side panel disconnected")
        if task:
            task.cancel()


def lan_ip():
    """This PC's address on the local network (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    if HOST not in LOOPBACK:
        ip = lan_ip() if HOST in ("0.0.0.0", "::") else HOST
        log.info("Other PCs on your network can connect: in Krify's side panel → Server, enter %s "
                 "(ws://%s:%s/ws)", ip or "this PC's IP", ip or "<this PC's IP>", PORT)
    uvicorn.run("app.main:app", host=HOST, port=PORT)
