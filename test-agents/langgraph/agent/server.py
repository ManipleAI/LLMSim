"""The agent's chat page and its API.

    GET  /                    the chat page
    GET  /api/threads         the chats kept, newest first
    POST /api/threads         a new, empty chat
    GET  /api/threads/{id}    one chat: every message, tool call and tool result
    DELETE /api/threads/{id}  forget a chat
    POST /api/chat/stream     send a message; the steps come back as they happen (server-sent events)
    POST /api/chat            send a message; one JSON answer when it is done
    GET  /healthz             up, and which model it uses

    POST /agents/{name}/api/chat   the same, with another agent of the team (team.py): ops,
                                   coordinator, researcher; "mcp", "api" and "peers" say what it reaches
    GET  /a2a/researcher/.well-known/agent-card.json, POST /a2a/researcher
                                   the researcher, as an A2A agent (JSON-RPC 0.3)
    GET  /v1/models, POST /v1/chat/completions, POST /v1/responses
                                   the assistant, as an OpenAI-compatible chat endpoint and the Responses API
    POST /services/mcp, GET /services/rest/...
                                   two small real services (an MCP server, a REST API) for a test to record

/api/chat is the one a test harness drives (LLMSim's http driver): {"message", "thread_id"?}
in, {"reply", "thread_id", "steps"} out. "input" and "session" are read too, so LLMSim's own
default body works unchanged.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from . import services, team
from .graph import build, checkpointer

ROOT = Path(__file__).resolve().parent.parent
SAVER, CONN = checkpointer()
GRAPH = build(SAVER)
LOCK = threading.Lock()
RECURSION_LIMIT = int(os.environ.get("AGENT_MAX_STEPS", "40"))
with LOCK:
    CONN.execute("create table if not exists chat_threads (id text primary key, title text, created real, updated real)")
    CONN.commit()

app = FastAPI(title="LangGraph agent")


def _touch(thread_id: str, title: str | None = None):
    now = time.time()
    with LOCK:
        CONN.execute("insert into chat_threads (id, title, created, updated) values (?,?,?,?) "
                     "on conflict(id) do update set updated=excluded.updated, title=coalesce(chat_threads.title, excluded.title)",
                     (thread_id, title, now, now))
        CONN.commit()


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content or [])


def _message(m) -> dict:
    if isinstance(m, HumanMessage):
        return {"role": "user", "text": _text(m.content)}
    if isinstance(m, AIMessage):
        return {"role": "assistant", "text": _text(m.content),
                "toolCalls": [{"id": c["id"], "name": c["name"], "args": c["args"]} for c in m.tool_calls or []]}
    if isinstance(m, ToolMessage):
        return {"role": "tool", "name": m.name, "callId": m.tool_call_id, "text": _text(m.content)}
    return {"role": getattr(m, "type", "other"), "text": _text(getattr(m, "content", ""))}


def _config(thread_id: str, test_run: dict | None = None) -> dict:
    return {"configurable": {"thread_id": thread_id, "test_run": test_run or {}}, "recursion_limit": RECURSION_LIMIT}


def _steps(message: str, thread_id: str, test_run: dict | None = None, graph=None):
    """Run one turn of the graph and say what each node did as it does it.

    A turn that goes round the model and its tools more than AGENT_MAX_STEPS times is stopped
    and says so: an agent that loops must end with an answer a person can read, not a crash."""
    try:
        yield from _graph_steps(message, thread_id, test_run, graph or GRAPH)
    except GraphRecursionError:
        yield {"type": "stopped", "text": f"I stopped: this task took more than {RECURSION_LIMIT} steps without finishing."}
    except Exception as exc:  # noqa: BLE001 — the model is down, refused, or answered nonsense: say so, do not crash
        detail = str(exc).splitlines()[0][:200] if str(exc) else ""
        yield {"type": "stopped", "text": f"I could not finish: the model call failed ({type(exc).__name__}{': ' + detail if detail else ''})."}


def _graph_steps(message: str, thread_id: str, test_run: dict | None, graph):
    spans = services.Spans(test_run)
    for update in graph.stream({"messages": [HumanMessage(message)]}, _config(thread_id, test_run), stream_mode="updates"):
        for node, out in update.items():
            for m in (out or {}).get("messages", []):
                spans.saw(m)
                d = _message(m)
                if d["role"] == "assistant":
                    for c in d["toolCalls"]:
                        yield {"type": "tool_call", "name": c["name"], "args": c["args"], "id": c["id"]}
                    if d["text"] and not d["toolCalls"]:
                        yield {"type": "answer", "text": d["text"]}
                    elif d["text"]:
                        yield {"type": "thought", "text": d["text"]}
                elif d["role"] == "tool":
                    yield {"type": "tool_result", "name": d["name"], "id": d["callId"], "text": d["text"]}
    spans.send()


def _incoming(body: dict) -> tuple[str, str, dict]:
    message = (body.get("message") or body.get("input") or "").strip()
    if not message:
        raise HTTPException(400, "message is what to send the agent")
    thread_id = body.get("thread_id") or body.get("session") or str(uuid.uuid4())
    _touch(thread_id, message[:60])
    # Sent by a test harness (LLMSim's {{run}} and {{agent}}); passed on to the model calls.
    test_run = {k: str(body[k]) for k in ("run", "agent") if body.get(k)}
    return message, thread_id, test_run


@app.get("/")
def page():
    return FileResponse(ROOT / "agent" / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": os.environ.get("MODEL_NAME"), "modelBaseUrl": os.environ.get("MODEL_BASE_URL")}


@app.get("/api/threads")
def threads():
    with LOCK:
        rows = CONN.execute("select id, title, created, updated from chat_threads order by updated desc").fetchall()
    return {"threads": [{"id": r[0], "title": r[1] or "New chat", "created": r[2], "updated": r[3]} for r in rows]}


@app.post("/api/threads")
def new_thread():
    thread_id = str(uuid.uuid4())
    _touch(thread_id)
    return {"id": thread_id}


@app.get("/api/threads/{thread_id}")
def thread(thread_id: str):
    state = GRAPH.get_state(_config(thread_id))
    return {"id": thread_id, "messages": [_message(m) for m in (state.values or {}).get("messages", [])]}


@app.delete("/api/threads/{thread_id}")
def forget(thread_id: str):
    SAVER.delete_thread(thread_id)
    with LOCK:
        CONN.execute("delete from chat_threads where id=?", (thread_id,))
        CONN.commit()
    return {"deleted": thread_id}


@app.post("/api/chat/stream")
def chat_stream(body: dict):
    message, thread_id, test_run = _incoming(body)

    def events():
        yield f"data: {json.dumps({'type': 'thread', 'id': thread_id})}\n\n"
        try:
            for step in _steps(message, thread_id, test_run):
                yield f"data: {json.dumps(step, default=str)}\n\n"
        except Exception as exc:  # noqa: BLE001 — said in the chat, not swallowed
            yield f"data: {json.dumps({'type': 'error', 'text': f'{type(exc).__name__}: {exc}'})}\n\n"
        _touch(thread_id)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def _turn(message, thread_id, test_run, graph=None):
    steps = list(_steps(message, thread_id, test_run, graph))
    _touch(thread_id)
    answers = [s["text"] for s in steps if s["type"] in ("answer", "stopped")]
    return {"reply": answers[-1] if answers else "", "thread_id": thread_id, "steps": steps,
            "stopped": any(s["type"] == "stopped" for s in steps)}


@app.post("/api/chat")
def chat(body: dict):
    message, thread_id, test_run = _incoming(body)
    return _turn(message, thread_id, test_run)


def _team_graph(name, options, test_run):
    if name not in team.ALL:
        raise HTTPException(404, f"there is no agent {name!r}; there are {', '.join(team.ALL)}")
    tools, system = team.setup(name, options, test_run)
    return build(SAVER, tools, system)


@app.post("/agents/{name}/api/chat")
def team_chat(name: str, body: dict):
    message, thread_id, test_run = _incoming(body)
    graph = _team_graph(name, body, test_run)
    return dict(_turn(message, f"{name}:{thread_id}", test_run, graph), thread_id=thread_id, agent=name)


# -- the researcher, as an A2A agent -----------------------------------------------------

A2A_TOKEN = os.environ.get("AGENT_A2A_TOKEN", "")


def _a2a_allowed(request: Request):
    return not A2A_TOKEN or request.headers.get("authorization", "") == f"Bearer {A2A_TOKEN}"


@app.get("/a2a/researcher/.well-known/agent-card.json")
@app.get("/a2a/researcher/.well-known/agent.json")
def researcher_card(request: Request):
    base = str(request.base_url).rstrip("/")
    return {"name": "researcher", "description": "Answers questions that need working out, with Python.",
            "protocolVersion": "0.3.0", "version": "1.0.0", "url": f"{base}/a2a/researcher", "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": "research", "name": "Research", "description": "Work a question out and answer it", "tags": ["math", "research"]}],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}} if A2A_TOKEN else {}}


@app.post("/a2a/researcher")
def researcher_rpc(body: dict, request: Request):
    rid = body.get("id")
    if not _a2a_allowed(request):
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": "unauthorized"}}, 401)
    if body.get("method") != "message/send":
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"{body.get('method')} is not supported"}}
    msg = (body.get("params") or {}).get("message") or {}
    text = "".join(p.get("text", "") for p in msg.get("parts") or [] if p.get("kind", "text") == "text").strip()
    context = msg.get("contextId") or str(uuid.uuid4())
    out = _turn(text or "(empty)", f"researcher:{context}", {}, _team_graph("researcher", {}, {}))
    return {"jsonrpc": "2.0", "id": rid, "result": {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
                                                     "contextId": context, "parts": [{"kind": "text", "text": out["reply"]}]}}


# -- the assistant, as an OpenAI-compatible endpoint ---------------------------------------

def _lc_messages(messages):
    out = []
    for m in messages or []:
        text = _text(m.get("content"))
        if m.get("role") == "user":
            out.append(HumanMessage(text))
        elif m.get("role") == "assistant":
            out.append(AIMessage(text))
    return out


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "langgraph-assistant", "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
def completions(body: dict):
    """The whole conversation comes with each request, so this keeps nothing: the graph runs
    over the messages it is sent, and its last answer is the completion."""
    history = _lc_messages(body.get("messages"))
    if not history or not isinstance(history[-1], HumanMessage):
        raise HTTPException(400, "the last message is the user's")
    graph = build(None)
    try:
        final = graph.invoke({"messages": history}, {"recursion_limit": RECURSION_LIMIT})["messages"][-1]
        reply = _text(final.content)
    except Exception as exc:  # noqa: BLE001
        reply = f"I could not finish: {type(exc).__name__}"
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    if body.get("stream"):
        def chunks():
            yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}}]}) + "\n\n"
            yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")
    return {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": body.get("model") or "langgraph-assistant",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]}


RESPONSES: dict[str, str] = {}


@app.post("/v1/responses")
def responses(body: dict):
    """The Responses API: a response chains to the one before it by previous_response_id, which
    is kept here as the chat thread it belongs to."""
    given = body.get("input")
    if isinstance(given, list):
        given = " ".join(_text(i.get("content")) for i in given if isinstance(i, dict) and i.get("role", "user") == "user")
    if not isinstance(given, str) or not given.strip():
        raise HTTPException(400, "input is what the user says")
    thread_id = RESPONSES.get(body.get("previous_response_id") or "") or str(uuid.uuid4())
    out = _turn(given.strip(), thread_id, {})
    rid = "resp_" + uuid.uuid4().hex[:24]
    RESPONSES[rid] = thread_id
    return {"id": rid, "object": "response", "status": "completed", "model": body.get("model") or "langgraph-assistant",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": out["reply"]}]}],
            "output_text": out["reply"]}


app.include_router(services.router)
