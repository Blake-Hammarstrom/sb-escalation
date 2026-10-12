import email.message, io, json, unittest, urllib.error
import kick

TOKEN = "github_pat_" + "S" * 40  # synthetic


class R:
    def __init__(self, status=204): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def http_error(code, body, headers=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("u", code, "x", h, io.BytesIO(json.dumps(body).encode()))


class Kick(unittest.TestCase):
    def run_main(self, responses):
        calls = []
        def opener(req, timeout):
            calls.append((req.get_method(), req.full_url, req.data and json.loads(req.data)))
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        err = io.StringIO()
        import contextlib, os
        os.environ["DISPATCH_TOKEN"] = TOKEN
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = kick.main(opener)
        return code, calls, err.getvalue() + out.getvalue()

    def test_success_uses_workflow_dispatch_endpoint_with_tick_only_inputs(self):
        code, calls, out = self.run_main([R(204)])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("POST", kick.DISPATCH, {"ref": "main", "inputs": {"event": "", "project": ""}})])
        self.assertTrue(kick.DISPATCH.endswith("/actions/workflows/escalate.yml/dispatches"))
        self.assertNotIn("/repos/Blake-Hammarstrom/sb-escalation/dispatches", kick.DISPATCH)

    def test_403_reports_safe_details_and_never_the_token(self):
        e = http_error(403, {"message": "Resource not accessible by personal access token", "documentation_url": "https://docs"},
                       {"X-Accepted-GitHub-Permissions": "actions=write", "X-GitHub-Request-Id": "ABC"})
        code, calls, out = self.run_main([e, R(200), R(200)])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", out); self.assertIn("actions=write", out)
        self.assertIn("Resource not accessible by personal access token", out); self.assertIn("ABC", out)
        self.assertEqual([c[0] for c in calls], ["POST", "GET", "GET"])          # probes are read-only
        self.assertNotIn(TOKEN, out); self.assertNotIn("github_pat_", out); self.assertNotIn("Bearer", out)

    def test_probe_distinguishes_repo_not_granted(self):
        e = http_error(403, {"message": "Resource not accessible by personal access token"})
        code, calls, out = self.run_main([e, http_error(404, {"message": "Not Found"}), http_error(404, {"message": "Not Found"})])
        self.assertIn("/repos/Blake-Hammarstrom/sb-escalation -> 404", out)

    def test_transport_error_is_scrubbed(self):
        code, calls, out = self.run_main([urllib.error.URLError(f"weird {TOKEN}")])
        self.assertEqual(code, 1); self.assertNotIn(TOKEN, out)

    def test_missing_token(self):
        import os
        os.environ["DISPATCH_TOKEN"] = ""
        self.assertEqual(kick.main(lambda *a, **k: None), 2)


if __name__ == "__main__":
    unittest.main()
