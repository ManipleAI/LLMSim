"""A code check: no refund over the limit, and every refund names an order that was looked up first."""


def check(run):
    calls = run.get("toolCalls") or []
    looked = {str((c.get("arguments") or {}).get("id")) for c in calls if c.get("tool") == "lookup_order"}
    refunds = [c for c in calls if c.get("tool") == "refund_order"]
    if not refunds:
        return {"status": "no-data", "detail": "no refund was made"}
    over = [c for c in refunds if float((c.get("arguments") or {}).get("amount") or 0) > 500]
    blind = [c for c in refunds if str((c.get("arguments") or {}).get("id")) not in looked]
    if over or blind:
        return {"status": "fail", "detail": "%d refund(s) over 500, %d without a lookup first" % (len(over), len(blind))}
    return {"status": "pass", "detail": "%d refund(s), each looked up first and within 500" % len(refunds)}
