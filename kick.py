#!/usr/bin/env python3
"""Reliable clock for the escalation runner (GitHub's schedule proved unreliable: 2 runs in ~10 h for */5).
Run every 5 min by a Render cron job. It only ever requests a TICK-ONLY run (empty event): it can't open, acknowledge or
close anything.

Endpoint: POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches ("Create a workflow dispatch event").
GitHub docs: fine-grained token needs "Actions" repository permission (write); success is 204.
(NOT /repos/{owner}/{repo}/dispatches, which needs Contents: write.)
Token: DISPATCH_TOKEN, fine-grained, only this repo, Actions: read and write, nothing else.

On failure it prints a diagnostic of NON-SECRET response details only (status, GitHub's message, the permission the
endpoint accepts, request id) plus a read-only probe. The token is never printed; request headers are never printed."""
import json, os, sys, urllib.error, urllib.request

REPO = "Blake-Hammarstrom/sb-escalation"
API = f"https://api.github.com/repos/{REPO}"
DISPATCH = f"{API}/actions/workflows/escalate.yml/dispatches"
SAFE_HEADERS = ("X-Accepted-GitHub-Permissions", "X-GitHub-Request-Id", "X-RateLimit-Remaining", "X-GitHub-SSO",
                "GitHub-Authentication-Token-Expiration")


def request(token, method, url, body=None, opener=urllib.request.urlopen):
    """(status, safe_details). Never raises for HTTP answers; never includes the token."""
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sb-escalation-kick"})
    try:
        with opener(req, timeout=30) as r:
            return r.status, {}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        details = {"message": payload.get("message"), "documentation_url": payload.get("documentation_url")}
        details.update({h: e.headers.get(h) for h in SAFE_HEADERS if e.headers and e.headers.get(h)})
        return e.code, details


def scrub(text, token):
    return text.replace(token, "<redacted>") if token else text  # belt and braces: nothing above echoes it


def diagnose(token, opener=urllib.request.urlopen):
    """Read-only probes that separate the usual 403 causes without changing anything."""
    lines = []
    for name, url in (("repo visible to token (Metadata: read)", API), ("Actions readable (Actions: read)", f"{API}/actions/workflows")):
        st, d = request(token, "GET", url, opener=opener)
        lines.append(f"  probe {name}: GET {url.replace(API, '/repos/' + REPO)} -> {st} {json.dumps(d) if d else ''}")
    lines.append("  reading: probe 1 not 200 -> token not granted this repo (or wrong owner / expired / revoked);"
                 " probes 200 but dispatch 403 -> token lacks Actions: WRITE")
    return lines


def main(opener=urllib.request.urlopen):
    token = os.environ.get("DISPATCH_TOKEN", "").strip()
    if not token:
        print("kick: DISPATCH_TOKEN missing", file=sys.stderr)
        return 2
    try:
        status, details = request(token, "POST", DISPATCH, {"ref": "main", "inputs": {"event": "", "project": ""}}, opener)
    except (urllib.error.URLError, OSError) as e:
        print(scrub(f"kick: transport failure: {e!r}"[:300], token), file=sys.stderr)
        return 1
    if status == 204:
        print("kick: 204")
        return 0
    out = [f"kick: FAILED POST /repos/{REPO}/actions/workflows/escalate.yml/dispatches -> HTTP {status}",
           f"  github: {json.dumps(details)}", *diagnose(token, opener)]
    print(scrub("\n".join(out), token), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
