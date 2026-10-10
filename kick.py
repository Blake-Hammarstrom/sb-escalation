#!/usr/bin/env python3
"""Reliable clock for the escalation runner (GitHub's schedule proved unreliable: 2 runs in ~10 h for */5).
Run every 5 min by a Render cron job. It only ever requests a TICK-ONLY run (empty event): it can't open, acknowledge or
close anything. Token: DISPATCH_TOKEN, a fine-grained PAT for this repo only, Actions read/write, nothing else.
Exit non-zero on any failure so Render shows the run as failed; the vault's heartbeat check blocks work regardless."""
import json, os, sys, urllib.error, urllib.request

URL = "https://api.github.com/repos/Blake-Hammarstrom/sb-escalation/actions/workflows/escalate.yml/dispatches"


def kick(token, opener=urllib.request.urlopen):
    req = urllib.request.Request(URL, method="POST", data=json.dumps({"ref": "main", "inputs": {"event": "", "project": ""}}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "sb-escalation-kick"})
    with opener(req, timeout=30) as r:
        return r.status


if __name__ == "__main__":
    token = os.environ.get("DISPATCH_TOKEN", "").strip()
    if not token:
        print("kick: DISPATCH_TOKEN missing", file=sys.stderr)
        sys.exit(2)
    try:
        status = kick(token)
    except (urllib.error.URLError, OSError) as e:  # never print the token; the error text doesn't contain it
        print(f"kick: failed: {e!r}"[:300], file=sys.stderr)
        sys.exit(1)
    print(f"kick: {status}")
    sys.exit(0 if status == 204 else 1)
