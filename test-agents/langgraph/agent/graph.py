"""The agent: a LangGraph graph of a model and its tools.

    START → model ──(asks for tools)──→ tools ─┐
              ▲                                │
              └────────────────────────────────┘
            model ──(answers)──→ END

The model is whatever MODEL_BASE_URL serves under MODEL_NAME, spoken to in the OpenAI API.
That is Azure AI Foundry's /openai/v1/ today, and LLMSim's /openai/v1 when the agent is
under test: the graph does not change, only where it sends its model calls.

Conversations are kept by thread in a SQLite file (data/chats.sqlite), so a chat survives a
restart and can be picked up again.
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / os.environ.get("AGENT_ENV_FILE", ".env"), override=False)

from . import tools as agent_tools  # noqa: E402 — after the environment is read: it names the workspace

SYSTEM = """You are a capable assistant that carries out tasks, not just describes them.
You have tools: files in a workspace folder (list, read, write), Python to compute and transform,
fetching web pages, and the current time. When a task needs one, use it, check what came back,
and keep going until the task is done. Then say plainly what you did and what the result is.
If something fails, say what failed and what you tried. Do not invent results you did not get."""


def model():
    key = os.environ["MODEL_API_KEY"]
    extra = {}
    if os.environ.get("MODEL_REASONING_EFFORT"):
        extra["reasoning_effort"] = os.environ["MODEL_REASONING_EFFORT"]
    return ChatOpenAI(
        model=os.environ.get("MODEL_NAME", "gpt-5-nano"),
        base_url=os.environ["MODEL_BASE_URL"],
        api_key=key,
        # Azure reads its key from api-key; an OpenAI-style server (LLMSim) from Authorization.
        default_headers={"api-key": key},
        timeout=float(os.environ.get("MODEL_TIMEOUT_S", "120")),
        max_retries=2,
        **extra,
    )


def build(checkpointer=None, tools=None, system=SYSTEM):
    """The graph, with these tools and instructions; the assistant's own when none are given."""
    tools = agent_tools.available() if tools is None else tools
    llm = model().bind_tools(tools) if tools else model()

    def call_model(state: MessagesState, config: RunnableConfig):
        # A test harness says which test run this conversation is part of; the model calls
        # carry it on, the way a trace id is carried, so the harness can tell them apart.
        tag = (config.get("configurable") or {}).get("test_run") or {}
        headers = {k: v for k, v in (("X-LLMSim-Run", tag.get("run")), ("X-LLMSim-Agent", tag.get("agent"))) if v}
        reply = llm.invoke([SystemMessage(system), *state["messages"]], **({"extra_headers": headers} if headers else {}))
        return {"messages": [reply]}

    def bad_calls(state: MessagesState):
        # The model asked for a tool with arguments that are not valid JSON. Ending there gives
        # the person an empty answer; telling the model what was wrong lets it try again.
        last = state["messages"][-1]
        return {"messages": [ToolMessage(f"Error: the arguments for {c.get('name') or 'this tool'} were not valid JSON "
                                         f"({c.get('error') or 'could not be parsed'}). Call it again with valid JSON arguments.",
                                         tool_call_id=c.get("id") or "invalid", name=c.get("name") or "unknown")
                             for c in last.invalid_tool_calls]}

    def after_model(state: MessagesState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        if getattr(last, "invalid_tool_calls", None):
            return "bad_calls"
        return END

    g = StateGraph(MessagesState)
    g.add_node("model", call_model)
    g.add_node("tools", ToolNode(tools or []))
    g.add_node("bad_calls", bad_calls)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, {"tools": "tools", "bad_calls": "bad_calls", END: END})
    g.add_edge("tools", "model")
    g.add_edge("bad_calls", "model")
    return g.compile(checkpointer=checkpointer)


def checkpointer():
    data = Path(os.environ.get("AGENT_DATA") or ROOT / "data")
    data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data / "chats.sqlite", check_same_thread=False)
    return SqliteSaver(conn), conn
