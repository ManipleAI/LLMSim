# Security

## Reporting a vulnerability

Please don't report a vulnerability in a public issue. Report it privately instead: on this repository's **Security** tab, choose **Report a vulnerability**. Only the maintainers see it.

Include what you can:

- what an attacker can do, and what they need first, such as network access to the port or a signed-in browser;
- the steps to reproduce it, with the LLMSim commit you tested;
- how LLMSim was started, including any `LLMSIM_*` settings that matter.

We'll reply in the report, and agree a fix and when to disclose it with you there.

## Supported versions

Fixes go to `main` and to the latest release. Please test against the latest `main` before reporting.

## What is expected, not a vulnerability

LLMSim is a test tool, and some of it is open by design. The [README](README.md#security) and the [documentation](docs/guide.md#require-a-sign-in) say how:

- **No password set:** anyone who can reach the port can use the console and the whole API. Set a password to require a sign-in.
- **The vendors' routes** (`/anthropic`, `/openai`, `/deepseek`) take published fake keys, so any client can call them. Set your own with `LLMSIM_ANTHROPIC_KEY`, `LLMSIM_OPENAI_KEY` and `LLMSIM_DEEPSEEK_KEY`.
- **What an agent under test calls** stays open even with a sign-in, because an agent cannot sign in. That covers the vendors' routes and the test kit's stand-ins (`/mcp`, `/mock`, `/a2a`, `/a2a-proxy`, `/otlp`, `/callbacks`).
- **Plain HTTP:** LLMSim serves no TLS of its own. Put it behind HTTPS before exposing it beyond a network you trust.

A way around any of these protections is a vulnerability:

- the sign-in, the session cookie, or the API key;
- the cross-site checks on the console;
- the owner-only files LLMSim keeps keys in;
- the keys it stores for an upstream or Anthropic, which it never sends to the console.
