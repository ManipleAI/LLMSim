"""The process a code evaluator runs in. Not imported: run as  python3 -I _evalchild.py FILE FUNCTION DENIED_JSON.

Reads the run as JSON on stdin, calls FUNCTION(run), writes the answer as JSON on stdout.

This is defence in depth and not a sandbox. An audit hook refuses what an evaluator has no business
doing — opening the files LLMSim keeps credentials and runs in, opening a socket, starting a process, loading
native code — and the process has no environment and little time. A determined function written in C could
still get round it. An evaluator is code somebody wrote and committed to the repository, and it should be read
like any other code that runs on the machine.
"""

import json
import os
import sys

REFUSED = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "ctypes.", "os.startfile", "os.fwalk", "os.walk")


def main():
    path, func, denied = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    run = json.loads(sys.stdin.read())
    denied_files = {os.path.realpath(p) for p in denied}

    def hook(event, args):
        if event == "open":
            target = args[0]
            if isinstance(target, (bytes, str)):
                real = os.path.realpath(os.fsdecode(target))
                if real in denied_files or any(real.startswith(d + "-") for d in denied_files) or os.path.basename(real).startswith("llmsim."):
                    raise PermissionError("LLMSim does not let an evaluator open %s" % os.path.basename(real))
        elif event.startswith(REFUSED):
            raise PermissionError("LLMSim does not let an evaluator use %s" % event)
    import importlib.util
    spec = importlib.util.spec_from_file_location("evaluator", path)
    module = importlib.util.module_from_spec(spec)
    sys.addaudithook(hook)
    spec.loader.exec_module(module)
    out = getattr(module, func)(run)
    sys.stdout.write(json.dumps(out if isinstance(out, (dict, bool)) else {"status": "error", "detail": "the function returned %s, not True/False or a dict" % type(out).__name__}))


if __name__ == "__main__":
    try:
        main()
    except PermissionError as exc:
        sys.stdout.write(json.dumps({"status": "error", "detail": str(exc)}))
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"status": "error", "detail": "%s: %s" % (type(exc).__name__, exc)}))
