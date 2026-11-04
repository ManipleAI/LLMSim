#!/usr/bin/env python3
"""Output limits per model, and DeepSeek's Anthropic-format route.

    python3 tests/test_model_limits.py

A limit LLMSim invents is worse than none: a client that is right fails here
and nowhere else. Sonnet 5 matched the "claude-sonnet" family row and got
Sonnet 4.5's 64,000-token output limit, so OpenHands and goose, which ask
Sonnet 5 for its real 128,000, failed every session against LLMSim. And a
coding agent on a DeepSeek model, which speaks Messages through a gateway to
DeepSeek's Anthropic-compatible endpoint, got "No such route".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import run_cases, ANTHROPIC_KEY  # noqa: E402

DEEPSEEK_KEY = "sk-llmsim-deepseek-0000000000000000"
ANTH = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}
DEEP = {"x-api-key": DEEPSEEK_KEY, "anthropic-version": "2023-06-01"}


def messages(model, max_tokens, text="hello there"):
    return {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": text}]}


def case_sonnet_5_takes_its_published_output_limit(sim):
    status, body = sim.api("POST", "/anthropic/v1/messages", messages("claude-sonnet-5", 128000), ANTH)
    assert status == 200 and body.get("type") == "message", (status, body)


def case_sonnet_5_5_shares_it(sim):
    status, body = sim.api("POST", "/anthropic/v1/messages", messages("claude-sonnet-5-5", 128000), ANTH)
    assert status == 200 and body.get("type") == "message", (status, body)


def case_sonnet_5_above_it_is_still_refused(sim):
    status, body = sim.api("POST", "/anthropic/v1/messages", messages("claude-sonnet-5", 128001), ANTH)
    assert status == 400 and "128000" in str(body), (status, body)


def case_sonnet_4_5_keeps_its_own(sim):
    status, body = sim.api("POST", "/anthropic/v1/messages", messages("claude-sonnet-4-5", 128000), ANTH)
    assert status == 400 and "64000" in str(body), (status, body)


def case_deepseek_answers_in_anthropic_format(sim):
    status, body = sim.api("POST", "/deepseek/anthropic/v1/messages", messages("deepseek-chat", 1024), DEEP)
    assert status == 200 and body.get("type") == "message" and body.get("content"), (status, body)


def case_deepseek_counts_tokens_in_anthropic_format(sim):
    status, body = sim.api("POST", "/deepseek/anthropic/v1/messages/count_tokens", {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]}, DEEP)
    assert status == 200 and isinstance(body.get("input_tokens"), int), (status, body)


def case_deepseek_keeps_its_own_limit_in_that_format(sim):
    status, body = sim.api("POST", "/deepseek/anthropic/v1/messages", messages("deepseek-chat", 9000), DEEP)
    assert status == 400 and "8192" in str(body), (status, body)


def case_deepseek_chat_completions_unchanged(sim):
    status, body = sim.api("POST", "/deepseek/v1/chat/completions", {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
                           {"Authorization": "Bearer " + DEEPSEEK_KEY})
    assert status == 200 and body.get("choices"), (status, body)


def case_anthropic_messages_still_only_on_anthropic_and_deepseek(sim):
    status, _ = sim.api("POST", "/openai/anthropic/v1/messages", messages("gpt-5", 100), {"Authorization": "Bearer sk-llmsim-000000000000000000000000"})
    assert status == 404, status


if __name__ == "__main__":
    run_cases([v for k, v in sorted(globals().items()) if k.startswith("case_")])
