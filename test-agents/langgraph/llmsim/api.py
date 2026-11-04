"""Small client for the live LLMSim's test kit."""
import json, os, sys, urllib.request, urllib.error
# Its own LLMSim, not the one other tests on this machine use: the llmsim-agents container (README).
BASE = os.environ.get("LLMSIM_URL", "http://127.0.0.1:18110")
def api(method, path, body=None, timeout=600):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers=dict({"Content-Type": "application/json"},
                                              **({"X-LLMSim-Key": os.environ["LLMSIM_API_KEY"]} if os.environ.get("LLMSIM_API_KEY") else {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw[:500].decode(errors="replace")}
