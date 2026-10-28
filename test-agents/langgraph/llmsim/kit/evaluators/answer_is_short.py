"""A code check: the final answer is a few sentences, not an essay."""


def check(run):
    final = run.get("finalAnswer") or run.get("final") or ""
    if not final:
        replies = run.get("replies") or []
        final = replies[-1] if replies else ""
    if isinstance(final, dict):
        final = final.get("text") or ""
    words = len(str(final).split())
    return {"status": "pass" if 0 < words <= 120 else "fail", "detail": "the answer is %d words" % words}
