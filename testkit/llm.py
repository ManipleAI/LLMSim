"""Asking a model, for the parts of the kit that need one.

A judge, a simulated user and a generator of test variations are all a model being asked a
question. They ask it the way an agent under test does: through LLMSim's own OpenAI-compatible
endpoint, under a run and an agent name. That gives them everything LLMSim already does for a
model — a script for the simulated tier, a recording to replay, a route to a real model — and means
a simulated user's turns land in the same recording as the agent's, so a replay repeats them.
"""

import json
import urllib.error
import urllib.request

from . import core


class ModelError(Exception):
    pass


def chat(model, messages, run, agent, timeout=120):
    """The text of one model answer. ModelError when there is none."""
    host = core.host
    url = "http://127.0.0.1:%d/run/%s/agent/%s/openai/v1/chat/completions" % (host.PORT, run, agent)
    req = urllib.request.Request(url, json.dumps({"model": model, "messages": messages}).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + host.API_KEYS["openai"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ModelError("the model answered HTTP %d: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200]))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ModelError("could not ask the model: %s" % getattr(exc, "reason", exc))
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ModelError("the model's answer has no text")


def default_model():
    return core.host.MODELS["openai"][0] if core.host.MODELS.get("openai") else "gpt-5"
