#!/usr/bin/env python3
"""What LLMSim keeps outlives LLMSim: one directory for it, and a volume for that directory.

    python3 tests/test_state.py

Signing in to a ChatGPT plan, saving an Anthropic key, routing a model, writing a rule: each
is something a person did once and expects to stay done. In a container all of it used to
live beside the code, inside the image's filesystem, so every restart and every new image
started from nothing and the first anybody heard of it was the sign-in screen again.

LLMSIM_DATA names the one directory LLMSim writes to. These cases start LLMSim with nothing
but that, restart it, and check everything comes back; check that a file's own variable
still wins; that a checkout without it behaves as it always has; that a container with no
volume says so; and that the shipped image, compose file and cluster manifest put the
directory on a volume.
"""

import base64
import json
import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, run_cases  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PER_FILE = ("LLMSIM_CONFIG", "LLMSIM_SUITE", "LLMSIM_CASSETTES", "LLMSIM_CHATGPT_AUTH", "LLMSIM_RULES",
            "LLMSIM_ASSERTIONS", "LLMSIM_RUNS_DB", "LLMSIM_CREDENTIALS", "LLMSIM_EVALUATORS", "LLMSIM_DATA",
            "KUBERNETES_SERVICE_HOST")
ANTHROPIC_KEY = "sk-ant-api03-state-test-0000000000000000"
RULE = {"name": "kept-rule", "when": {"contains": "is my rule still here"}, "turns": [{"text": "It is."}]}


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt(claims):
    return ".".join([b64(b'{"alg":"none"}'), b64(json.dumps(claims).encode()), "sig"])


def sim_on(data, **env):
    """An LLMSim told only where its data directory is: none of the kit's per-file variables."""
    sim = Sim()
    for name in PER_FILE:
        sim.env.pop(name, None)
    sim.env.update(env)
    if data:
        sim.env["LLMSIM_DATA"] = data
    return sim


def plant_sign_in(data):
    """What a finished ChatGPT sign-in leaves on disk. Nobody signs in during a test."""
    os.makedirs(data, exist_ok=True)
    claims = {"exp": int(time.time()) + 3600}
    auth = {"access_token": jwt(claims), "refresh_token": "refresh-state", "id_token": "", "client_id": "oaiapp_state",
            "ext_agent_host_id": "urn:uuid:state", "subject": "user-state", "scopes": "openid chatgpt.tokens.use.direct",
            "email": "plan@example.test", "plan": "plus", "expires_at": time.time() + 3600, "saved_at": time.time()}
    fd = os.open(os.path.join(data, "llmsim.chatgpt-auth.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(auth, fh)


def case_everything_a_person_set_comes_back_after_a_restart(_sim):
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "nested", "data")          # not there yet: LLMSim makes it
        plant_sign_in(data)
        sim = sim_on(data).start()
        try:
            health = sim.api("GET", "/healthz")[1]
            assert health["state"] == {"dir": data, "kept": True}, health["state"]
            assert health["chatgpt"]["signedIn"] is True, health["chatgpt"]
            # what a person does in the console, and through the API
            status, out = sim.api("POST", "/api/ui/config", {
                "routing": {"default": "chatgpt", "models": {"grok-4.5": "scripted"}},
                "anthropic": {"key": ANTHROPIC_KEY, "auth": "api_key"}})
            assert status == 200, out
            assert sim.api("POST", "/api/rules", {"rule": RULE})[0] == 200
            assert sim.api("PUT", "/api/credentials/agent-token", {"type": "bearer", "token": "kept-token-value"})[0] == 200
            agent = {"id": "kept-agent", "label": "Kept", "system": "custom",
                     "connection": {"driver": "http", "url": "http://127.0.0.1:9/chat", "reply": "output.text", "body": {"input": "{{message}}"}}}
            status, out = sim.api("PUT", "/api/agents/kept-agent", agent)
            assert status == 200, out

            sim.restart()

            health = sim.api("GET", "/healthz")[1]
            assert health["chatgpt"]["signedIn"] is True, "the ChatGPT sign-in did not survive a restart"
            assert health["anthropic"]["keySet"] is True, "the Anthropic key did not survive a restart"
            assert health["mode"] == "chatgpt" and "kept-rule" in health["rules"], health
            routing = sim.api("GET", "/api/routing")[1]
            assert (routing["default"], routing["models"]) == ("chatgpt", {"grok-4.5": "scripted"}), routing
            assert [c["label"] for c in sim.api("GET", "/api/credentials")[1]["credentials"]] == ["agent-token"]
            assert sim.api("GET", "/api/agents/kept-agent")[0] == 200, "a saved agent did not survive a restart"
        finally:
            sim.stop()
        kept = sorted(os.listdir(data))
        for name in ("llmsim.config.json", "llmsim.chatgpt-auth.json", "llmsim.rules.json", "llmsim.credentials.json", "llmsim.runs.db"):
            assert name in kept, (name, kept)
        # the files with keys in them are the owner's alone, wherever they are kept
        for name in ("llmsim.chatgpt-auth.json", "llmsim.credentials.json"):
            mode = stat.S_IMODE(os.stat(os.path.join(data, name)).st_mode)
            assert mode == 0o600, (name, oct(mode))


def case_nothing_is_written_beside_the_code_when_a_data_directory_is_named(_sim):
    before = set(os.listdir(ROOT))
    with tempfile.TemporaryDirectory() as data:
        sim = sim_on(data).start()
        try:
            sim.api("POST", "/api/ui/config", {"routing": {"default": "scripted", "models": {"gpt-5": "chatgpt"}}})
            sim.api("POST", "/api/rules", {"rule": RULE})
            assert sim.api("POST", "/api/sessions/record", {"name": "state-test"})[0] == 200
            sim.chat("is my rule still here")
            sim.api("POST", "/api/sessions/stop", {})
        finally:
            sim.stop()
        assert "llmsim.config.json" in os.listdir(data)
    new = sorted(set(os.listdir(ROOT)) - before - {"__pycache__"})
    assert new == [], "written beside the code: %s" % new


def case_a_files_own_variable_still_wins(_sim):
    with tempfile.TemporaryDirectory() as tmp:
        data, elsewhere = os.path.join(tmp, "data"), os.path.join(tmp, "config-here.json")
        sim = sim_on(data, LLMSIM_CONFIG=elsewhere).start()
        try:
            sim.api("POST", "/api/ui/config", {"routing": {"default": "chatgpt", "models": {}}})
            sim.api("POST", "/api/rules", {"rule": RULE})
        finally:
            sim.stop()
        assert json.load(open(elsewhere))["routing"]["default"] == "chatgpt"
        assert "llmsim.config.json" not in os.listdir(data) and "llmsim.rules.json" in os.listdir(data)


def case_without_it_a_checkout_keeps_its_state_where_it_always_did(sim):
    """The kit's own simulator names every file and no data directory: the directory is the checkout."""
    state = sim.api("GET", "/healthz")[1]["state"]
    assert state == {"dir": ROOT, "kept": True}, state


def case_a_container_with_no_volume_says_it_will_forget(_sim):
    with tempfile.TemporaryDirectory() as data:
        sim = sim_on(data, KUBERNETES_SERVICE_HOST="10.43.0.1").start()
        try:
            state = sim.api("GET", "/healthz")[1]["state"]
            assert state == {"dir": data, "kept": False}, state
            assert sim.api("GET", "/api/ui/state")[1]["state"]["kept"] is False
        finally:
            proc = sim.proc
            sim.stop()
        assert "is inside this container" in proc.stderr.read().decode(), "the log does not say it"
        page = open(os.path.join(ROOT, "ui.html")).read()
        assert 'id="cg-kept"' in page and "STATE = s.state || null" in page, "the console does not say it"
        assert 'k: "Settings kept"' in page


def case_the_image_compose_file_and_cluster_put_it_on_a_volume(_sim):
    docker = open(os.path.join(ROOT, "Dockerfile")).read()
    assert "ENV LLMSIM_DATA=/data" in docker and "VOLUME /data" in docker
    compose = open(os.path.join(ROOT, "compose.yml")).read()
    assert "llmsim-data:/data" in compose and "\nvolumes:\n  llmsim-data:" in compose
    k8s = open(os.path.join(ROOT, "deploy", "k8s.yaml")).read()
    assert "{ name: LLMSIM_DATA, value: /data }" in k8s and "{ name: data, mountPath: /data }" in k8s
    assert "persistentVolumeClaim: { claimName: llmsim-data }" in k8s
    # one pod on the volume at a time; and an install over an older RollingUpdate one is accepted
    assert "strategy: { type: Recreate, rollingUpdate: null }" in k8s
    claim = open(os.path.join(ROOT, "deploy", "k8s-data.yaml")).read()
    assert "kind: PersistentVolumeClaim" in claim and "name: llmsim-data" in claim and "storageClassName" not in claim
    install = open(os.path.join(ROOT, "deploy", "install.sh")).read()
    # the claim is made once and never re-applied; the old container's state is carried over, never over the volume's
    assert 'get pvc llmsim-data >/dev/null 2>&1; then\n  kubectl -n "$NS" apply -f "$HERE/deploy/k8s-data.yaml"' in install
    assert "the volume already holds LLMSim's state" in install
    # seeded from a helper pod before the new LLMSim starts, not under a running one; and judged by what a
    # person makes, since any first start writes a run database and the sample suite by itself
    seed = install.index("name: llmsim-seed"); start = install.index('apply -f "$HERE/deploy/k8s.yaml"')
    assert seed < start, "the volume is seeded after the new LLMSim has started"
    assert "ls llmsim.config.json llmsim.chatgpt-auth.json llmsim.credentials.json llmsim.rules.json llmsim.assertions.json" in install


if __name__ == "__main__":
    run_cases([v for k, v in list(globals().items()) if k.startswith("case_")])
