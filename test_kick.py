import json, unittest
import kick


class R:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Kick(unittest.TestCase):
    def test_only_ever_requests_a_tick_only_run(self):
        seen = {}
        def opener(req, timeout):
            seen.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
            return R()
        self.assertEqual(kick.kick("tok", opener), 204)
        self.assertEqual(seen["body"], {"ref": "main", "inputs": {"event": "", "project": ""}})
        self.assertTrue(seen["url"].endswith("/sb-escalation/actions/workflows/escalate.yml/dispatches"))
        self.assertEqual(seen["auth"], "Bearer tok")


if __name__ == "__main__":
    unittest.main()
