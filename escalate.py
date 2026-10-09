#!/usr/bin/env python3
"""Step 5 escalation with acknowledgment. Stdlib only; the same file runs locally and in sb-escalation's GitHub Actions.

  escalate.py tick                                   # runner (Actions, cron */5): poll producers, notify, re-alert, park
  escalate.py open --event E --project P [--task T]  # runner (workflow_dispatch): open one escalation from validated inputs
  escalate.py raise --event E --project P [--task T] # local supervisor: ask the runner to open one (dispatch token)
  escalate.py status --project P                     # local: may work run? exit 0 yes, 1 blocked, 2 unknown (fail closed)

All decisions are deterministic: severity from a fixed table, deadlines from timestamps. No model anywhere.
"""
import argparse, datetime as dt, http.client, json, os, re, sys, urllib.error, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

OWNER = "Blake-Hammarstrom"
REPO = f"{OWNER}/sb-escalation"
BOT = "github-actions[bot]"
TZ = ZoneInfo("America/Denver")
QUIET = (22, 7)                                    # 22:00-07:00 local
SEVERITY = {"gate_drift": "P1", "site_down": "P1", "security": "P1", "production_blocking": "P1",
            "task_failed": "P2", "task_interrupted": "P2", "token_expiring": "P3", "routine": "P3"}
INTERVAL = {"P1": dt.timedelta(minutes=15), "P2": dt.timedelta(hours=1), "P3": dt.timedelta(hours=4)}
MAX_REALERTS = 2
HEARTBEAT_MAX = dt.timedelta(minutes=20)
PROJECT_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TASK_RX = re.compile(r"^t-[0-9a-f]{16}$")
KEY_RX = re.compile(r"<!-- sb-key: ([a-z0-9_:/.-]{1,120}) -->")
ALERT_MARK = "<!-- sb-alert -->"
CRED = Path.home() / ".sb-credentials/_escalation/dispatch_token"


class EscalationError(Exception):
    pass


# ---------------------------------------------------------------- pure rules
def validate(event, project, task=""):
    """The only door from outside data into an issue. Rejects anything not on the allowlists."""
    if event not in SEVERITY:
        raise EscalationError(f"unknown event {event!r}")
    if not PROJECT_RX.fullmatch(project or ""):  # fullmatch: "$" would allow a trailing newline
        raise EscalationError(f"invalid project {project!r}")
    if task and not TASK_RX.fullmatch(task):
        raise EscalationError(f"invalid task id {task!r}")
    return event, project, task


def in_quiet(t):
    h = t.astimezone(TZ).hour
    return h >= QUIET[0] or h < QUIET[1]


def next_allowed(t, sev):
    """When a notification due at t may actually be sent. P1 breaks quiet hours; P2/P3 wait for 07:00 local."""
    if sev == "P1" or not in_quiet(t):
        return t
    local = t.astimezone(TZ)
    day = local.date() if local.hour < QUIET[1] else local.date() + dt.timedelta(days=1)
    return dt.datetime(day.year, day.month, day.day, QUIET[1], tzinfo=TZ).astimezone(dt.timezone.utc)


def decide(now, sev, created, alerts, acked, parked):
    """Next action for one escalation. alerts = times the bot notified (first = assignment, then re-alerts)."""
    if acked:
        return "none"
    if not alerts:
        return "notify" if now >= next_allowed(created, sev) else "wait"
    due = alerts[-1] + INTERVAL[sev]
    if now < due:
        return "wait"
    if len(alerts) - 1 < MAX_REALERTS:
        return "realert" if now >= next_allowed(due, sev) else "wait"
    return "none" if parked else "park"   # stopping is always allowed, quiet hours or not


def is_ack(issue, comments):
    """Only the owner acknowledges: an owner comment, or the owner closing the issue."""
    if issue.get("state") == "closed" and (issue.get("closed_by") or {}).get("login") == OWNER:
        return True
    return any((c.get("user") or {}).get("login") == OWNER for c in comments)


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- GitHub (thin; swapped for a fake in tests)
class GitHub:
    def __init__(self, token=None):
        self.token = token

    def req(self, method, path, body=None):
        r = urllib.request.Request(f"https://api.github.com{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Accept": "application/vnd.github+json", "User-Agent": "sb-escalation",
                                            **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        for attempt in range(3 if method == "GET" else 1):  # reads retry (truncated bodies seen live); writes never do
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:  # a real HTTP answer (403/404/...): retrying won't change it
                raise EscalationError(f"{method} {path}: {e!r}"[:300])
            # every transport failure is 'unknown', never a crash: truncated bodies (IncompleteRead), resets, bad JSON
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
                err = e
        raise EscalationError(f"{method} {path}: {err!r}"[:300])

    def issues(self, state="all"):
        return [i for i in self.req("GET", f"/repos/{REPO}/issues?state={state}&labels=escalation&per_page=100&creator={BOT.replace('[', '%5B').replace(']', '%5D')}")
                if "pull_request" not in i]

    def comments(self, n):
        return self.req("GET", f"/repos/{REPO}/issues/{n}/comments?per_page=100")

    def create(self, title, body, labels):
        return self.req("POST", f"/repos/{REPO}/issues", {"title": title, "body": body, "labels": labels})

    def comment(self, n, body):
        return self.req("POST", f"/repos/{REPO}/issues/{n}/comments", {"body": body})

    def assign(self, n):
        return self.req("POST", f"/repos/{REPO}/issues/{n}/assignees", {"assignees": [OWNER]})

    def label(self, n, labels):
        return self.req("POST", f"/repos/{REPO}/issues/{n}/labels", {"labels": labels})

    def heartbeat(self):
        runs = self.req("GET", f"/repos/{REPO}/actions/workflows/escalate.yml/runs?status=success&per_page=1")["workflow_runs"]
        return ts(runs[0]["updated_at"]) if runs else None

    def dispatch(self, inputs):
        return self.req("POST", f"/repos/{REPO}/actions/workflows/escalate.yml/dispatches", {"ref": "main", "inputs": inputs})


# ---------------------------------------------------------------- runner
def key_of(issue):
    m = KEY_RX.search(issue.get("body") or "")
    return m.group(1) if m else None


def open_escalation(gh, event, project, task="", existing=None):
    """Open one escalation unless an unacknowledged one with the same key is already open (dedup)."""
    event, project, task = validate(event, project, task)
    key = f"{event}:{project}:{task or '-'}"
    for i in (existing if existing is not None else gh.issues("open")):
        if key_of(i) == key:
            return None
    sev = SEVERITY[event]
    head = f"**{sev}** `{event}` on `{project}`" + (f", task `{task}`" if task else "")
    body = "\n".join([head, "", f"Acknowledge within **{int(INTERVAL[sev].total_seconds() // 60)} min** by commenting here (owner only).",
                      f"Unacknowledged after {MAX_REALERTS} re-alerts, work on `{project}` is **stopped and parked**.",
                      "", f"<!-- sb-key: {key} -->"])
    made = gh.create(f"[{sev}] {event} · {project}" + (f" · {task}" if task else ""), body,
                     ["escalation", sev, f"project:{project}"])
    # GitHub silently drops labels it won't let this identity set; an unlabelled escalation would never be tracked
    if {"escalation", sev, f"project:{project}"} - {lb["name"] for lb in made.get("labels", [])}:
        raise EscalationError(f"issue #{made.get('number')} created without its labels: escalation untracked")
    return made


def tick(gh, now, probes=()):
    """One runner pass: producers first (each may open an escalation), then notify/re-alert/park every open one."""
    log = []
    open_issues = gh.issues("open")
    for event, project in probes:
        made = open_escalation(gh, event, project, existing=open_issues)
        if made:
            open_issues.append(made)
            log.append(f"opened {made['number']} {event}:{project}")
    for i in open_issues:
        labels = {lb["name"] for lb in i.get("labels", [])}
        sev = next((s for s in ("P1", "P2", "P3") if s in labels), None)
        if sev is None:
            continue
        comments = gh.comments(i["number"])
        alerts = sorted(ts(c["created_at"]) for c in comments
                        if (c.get("user") or {}).get("login") == BOT and ALERT_MARK in (c.get("body") or ""))
        action = decide(now, sev, ts(i["created_at"]), alerts, is_ack(i, comments), "parked" in labels)
        n = i["number"]
        if action == "notify":
            if OWNER not in [a["login"] for a in (gh.assign(n) or {}).get("assignees", [])]:
                raise EscalationError(f"#{n}: owner not assigned (silently dropped?)")
            gh.comment(n, f"@{OWNER} {sev} escalation: acknowledge by commenting. {ALERT_MARK}")
        elif action == "realert":
            gh.comment(n, f"@{OWNER} re-alert {len(alerts)}/{MAX_REALERTS}: still unacknowledged. {ALERT_MARK}")
        elif action == "park":
            gh.label(n, ["parked"])
            gh.comment(n, f"@{OWNER} no acknowledgment after {MAX_REALERTS} re-alerts: work on this project is **parked**. "
                          "Comment here to acknowledge; work stays parked until you do.")
        if action != "wait" and action != "none":
            log.append(f"{action} #{n}")
    return log


# ---------------------------------------------------------------- producers polled by the runner (public data only)
def rules_norm(rs):
    rules = {r["type"]: r.get("parameters", {}) for r in rs.get("rules", [])}
    return {"enforcement": rs.get("enforcement"), "bypass": rs.get("bypass_actors") or [], "rules": sorted(rules),
            "checks": sorted(c["context"] for c in rules.get("required_status_checks", {}).get("required_status_checks", []))}


def http_status(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sb-escalation"}), timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def probes(gh, cfg, now, fetch):
    """[(event, project)] for every condition that holds now. A probe that can't read its signal reports the condition:
    unknown is never healthy."""
    out = []
    want = rules_norm(cfg["ruleset"])
    for project in cfg["gated_repos"]:
        try:
            live = [r for r in gh.req("GET", f"/repos/{OWNER}/{project}/rules/branches/main") or []]
            types = sorted({r["type"] for r in live})
            checks = sorted(c["context"] for r in live if r["type"] == "required_status_checks"
                            for c in r["parameters"]["required_status_checks"])
            if types != want["rules"] or checks != want["checks"]:
                out.append(("gate_drift", project))
        except EscalationError:
            out.append(("gate_drift", project))
    for project, url in cfg["sites"].items():
        if fetch(url) != 200 and fetch(url) != 200:  # two tries: one blip isn't an outage
            out.append(("site_down", project))
    for project, expires in cfg["token_expiry"].items():
        if ts(expires) - now <= dt.timedelta(days=7):
            out.append(("token_expiring", project))
    return out


# ---------------------------------------------------------------- local side
def status(gh, project, now):
    """(code, reasons). 0 = may run, 1 = blocked, 2 = unknown -> callers treat 2 exactly like 1."""
    validate("routine", project)
    try:
        beat = gh.heartbeat()
        issues = gh.issues("open")
        acks = {i["number"]: is_ack(i, gh.comments(i["number"])) for i in issues
                if f"project:{project}" in {lb["name"] for lb in i.get("labels", [])}}
    except (EscalationError, KeyError, TypeError) as e:
        return 2, [f"escalation status unreadable: {e}"]
    reasons = []
    if beat is None or now - beat > HEARTBEAT_MAX:
        reasons.append(f"escalation runner heartbeat stale ({beat})")
    for i in issues:
        labels = {lb["name"] for lb in i.get("labels", [])}
        if f"project:{project}" not in labels:
            continue
        acked = acks[i["number"]]
        if not acked and ("parked" in labels or "P1" in labels):
            reasons.append(f"#{i['number']} {'parked' if 'parked' in labels else 'unacknowledged P1'}")
    return (1 if reasons else 0), reasons


def raise_remote(gh, event, project, task=""):
    validate(event, project, task)
    gh.dispatch({"event": event, "project": project, "task": task})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "open", "raise", "status"])
    ap.add_argument("--event", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--task", default="")
    a = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        if a.cmd in ("tick", "open"):
            gh = GitHub(os.environ.get("GITHUB_TOKEN"))
            if a.cmd == "open":
                made = open_escalation(gh, a.event, a.project, a.task)
                print(f"opened #{made['number']}" if made else "duplicate: already open")
            else:
                cfg = json.loads(Path(os.environ.get("SB_ESCALATION_CONFIG", "escalation.json")).read_text())
                print("\n".join(tick(gh, now, probes(gh, cfg, now, http_status))) or "tick: nothing due")
            return 0
        if a.cmd == "raise":
            token = CRED.read_text().strip()
            raise_remote(GitHub(token), a.event, a.project, a.task)
            print("raised")
            return 0
        code, reasons = status(GitHub(), a.project, now)
        print(json.dumps({"project": a.project, "mayRun": code == 0, "code": code, "reasons": reasons}))
        return code
    except (EscalationError, OSError) as e:
        print(f"ESCALATION ERROR (fail closed): {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
