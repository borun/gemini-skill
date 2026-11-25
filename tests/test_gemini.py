"""Offline tests for skills/gemini/scripts/gemini.py.

They run the script as a subprocess against a local fake of the Gemini API, so
they need no API key, no network and cost nothing:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import importlib.util
import datetime as dt
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "gemini" / "scripts" / "gemini.py"
BUNDLED_CATALOG = ROOT / "skills" / "gemini" / "catalog.json"
KEY = "dummy-local-test"
MODELS = [
    "gemini-4-carbon", "gemini-4-neon", "gemini-4-argon", "gemini-4-hydrogen",
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview", "gemini-3.8-flash-tts",
    "gemini-nano-banana-2.1", "gemini-3-pro-image", "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image", "gemini-3.1-flash-lite-image",
]


class FakeGemini(http.server.BaseHTTPRequestHandler):
    """Answers like the Gemini API: GET /models and POST /interactions."""

    models: list[str] = list(MODELS)
    models_down = False
    forced_status: int | None = None
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorised(self) -> bool:
        if self.headers.get("x-goog-api-key") == KEY:
            return True
        self.reply(400, {"error": {"code": 400, "message": "API key not valid."}})
        return False

    def do_GET(self):
        if not self.authorised():
            return
        if FakeGemini.models_down:
            return self.reply(500, {"error": {"code": 500, "message": "internal"}})
        self.reply(200, {"models": [
            {"name": f"models/{m}", "displayName": m, "inputTokenLimit": 1048576,
             "outputTokenLimit": 65536, "thinking": True}
            for m in FakeGemini.models
        ]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGemini.requests.append(body)
        if not self.authorised():
            return
        if FakeGemini.forced_status:
            return self.reply(FakeGemini.forced_status, {"error": {"message": "quota exceeded"}})
        model = body["model"]
        level = (body.get("generation_config") or {}).get("thinking_level")
        if model == "gemini-4-carbon" and level == "xhigh":
            return self.reply(400, {"error": {"code": 400, "message":
                              "Thinking level THINKING_LEVEL_XHIGH is not supported for this model."}})
        if "image" in model or "banana" in model:
            content = [{"type": "image", "mime_type": "image/jpeg",
                        "data": base64.b64encode(b"\xff\xd8\xff\xe0" + b"\0" * 32).decode()}]
        else:
            content = [{"type": "text", "text": f"answer from {model} at {level or 'default'}"}]
        self.reply(200, {
            "id": "int_123", "model": model, "status": "completed",
            "usage": {"total_input_tokens": 1000, "total_output_tokens": 200, "total_thought_tokens": 800},
            "steps": [{"type": "thought"}, {"type": "model_output", "content": content}],
        })


class GeminiScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGemini)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeGemini.models = list(MODELS)
        FakeGemini.models_down = False
        FakeGemini.forced_status = None
        FakeGemini.requests = []
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("GEMINI_", "GOOGLE_API_KEY", "XDG_"))
        }
        self.env.update({
            "GEMINI_API_BASE": self.base,
            "GEMINI_API_KEY": KEY,
            "GEMINI_USE_KEYCHAIN": "0",
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args: str, stdin: str | None = None, env: dict | None = None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True,
            text=True, encoding="utf-8", cwd=self.tmp, env={**self.env, **(env or {})},
        )

    def assertOk(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- model resolution -------------------------------------------------

    def test_models_lists_newest_stable_versions(self):
        result = self.run_script("models")
        self.assertOk(result)
        self.assertIn("gemini-3.8-flash", result.stdout)        # newest flash, not 3.7
        self.assertIn("gemini-3.5-flash-lite", result.stdout)   # newest lite, not 3.1
        self.assertIn("gemini-3-pro-image ", result.stdout)     # stable, not -preview
        self.assertNotIn("tts", result.stdout)

    def test_builtin_default_is_hydrogen(self):
        result = self.run_script("ask", "hi")
        self.assertOk(result)
        self.assertIn("answer from gemini-4-hydrogen at default", result.stdout)

    def test_config_file_sets_default(self):
        config = self.tmp / "config" / "gemini-skill" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"default_model": "neon-high"}))
        self.assertIn("gemini-4-neon at high", self.run_script("ask", "hi").stdout)
        override = self.run_script("ask", "hi", env={"GEMINI_DEFAULT_MODEL": "carbon"})
        self.assertIn("gemini-4-carbon at default", override.stdout)

    def test_effort_suffix_and_flag(self):
        self.assertIn("gemini-4-neon at high", self.run_script("ask", "x", "-m", "neon-high").stdout)
        self.assertIn("gemini-4-neon at low", self.run_script("ask", "x", "-m", "neon-high", "-t", "low").stdout)
        self.assertIn("gemini-4-neon at medium", self.run_script("ask", "x", "-m", "gemini-4-neon-medium").stdout)

    def test_typo_gets_suggestions(self):
        result = self.run_script("ask", "x", "-m", "noen-high")
        self.assertEqual(result.returncode, 1)
        self.assertIn("neon-high", result.stderr)
        self.assertEqual(FakeGemini.requests, [])               # nothing was sent

    def test_new_release_is_picked_up_and_flagged(self):
        self.assertOk(self.run_script("ask", "x", "-m", "neon"))
        FakeGemini.models.append("gemini-4.1-neon")
        self.assertOk(self.run_script("models", "--refresh"))
        result = self.run_script("ask", "x", "-m", "neon")
        self.assertOk(result)
        self.assertIn("answer from gemini-4.1-neon", result.stdout)
        self.assertIn('"neon" now means gemini-4.1-neon (was gemini-4-neon)', result.stderr)
        self.assertIn("no price for gemini-4.1-neon", result.stderr)

    def test_model_list_outage_uses_cache_then_builtin_names(self):
        self.assertOk(self.run_script("models"))
        FakeGemini.models_down = True
        cached = self.run_script("models", "--refresh")
        self.assertOk(cached)
        self.assertIn("using the copy from", cached.stderr)
        fresh = self.run_script("ask", "x", "-m", "carbon", env={"XDG_CACHE_HOME": str(self.tmp / "empty")})
        self.assertOk(fresh)
        self.assertIn("using built-in model names", fresh.stderr)

    # -- effort learning ----------------------------------------------------

    def test_rejected_effort_is_learned_with_suggestion(self):
        result = self.run_script("ask", "x", "-m", "carbon-xhigh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Nearest level known to work: high", result.stderr)
        check = self.run_script("check", "carbon-xhigh")
        self.assertIn("rejected: xhigh", check.stdout)
        probe = self.run_script("check", "carbon-xhigh", "--probe")
        self.assertEqual(probe.returncode, 1)
        self.assertIn("REJECTED", probe.stdout)

    # -- money ------------------------------------------------------------------

    def test_cost_guard_needs_yes(self):
        (self.tmp / "big.txt").write_text("a" * 700_000)
        blocked = self.run_script("ask", "summarise", "-f", "big.txt", "-m", "neon")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("--yes", blocked.stderr)
        self.assertEqual(FakeGemini.requests, [])
        self.assertOk(self.run_script("ask", "summarise", "-f", "big.txt", "-m", "neon", "--yes"))

    def test_cost_is_reported(self):
        result = self.run_script("ask", "x", "-m", "neon")
        self.assertIn("cost ≈ $0.02", result.stderr)  # 1000 in * $3 + 1000 out+thought * $15, per 1M

    def test_scheduled_price_change_applies_on_its_date(self):
        spec = importlib.util.spec_from_file_location("gemini_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        catalog = json.loads(BUNDLED_CATALOG.read_text())
        changes = catalog["models"]["gemini-4-carbon"].get("changes") or []
        if not changes:
            self.skipTest("no scheduled change in the catalog")
        change = changes[0]
        day = dt.date.fromisoformat(change["from"])
        store = module.Store.__new__(module.Store)
        store.catalog, store.state, store.dirty = catalog, {}, False
        module.TODAY = day - dt.timedelta(days=1)
        self.assertEqual(module.current_price(store, "gemini-4-carbon")["upcoming"]["from"], change["from"])
        module.TODAY = day
        self.assertEqual(module.current_price(store, "gemini-4-carbon")["input"], change["input"])

    def test_stale_catalog_warns(self):
        stale = json.loads(BUNDLED_CATALOG.read_text())
        stale["verified_on"] = "2000-01-01"
        path = self.tmp / "stale.json"
        path.write_text(json.dumps(stale))
        result = self.run_script("check", "neon", env={"GEMINI_CATALOG": str(path)})
        self.assertIn("refresh the catalog", result.stdout)

    # -- inputs and outputs ---------------------------------------------------

    def test_stdin_as_file_and_continue(self):
        self.assertOk(self.run_script("ask", "review", "-f", "-", "-c", "int_prev", stdin="diff --git a b"))
        body = FakeGemini.requests[-1]
        self.assertIn('<file path="stdin">', body["input"])
        self.assertEqual(body["previous_interaction_id"], "int_prev")

    def test_image_is_saved_as_jpg_without_overwriting(self):
        first = self.run_script("image", "a cat", "-o", "out/cat.png", "--aspect", "16:9")
        self.assertOk(first)
        self.assertTrue(first.stdout.strip().endswith("cat.jpg"))
        second = self.run_script("image", "a cat", "-o", "out/cat.jpg")
        self.assertTrue(second.stdout.strip().endswith("cat-2.jpg"))
        self.assertEqual(FakeGemini.requests[0]["response_format"], {"type": "image", "aspect_ratio": "16:9"})

    def test_quota_errors_explain_billing(self):
        FakeGemini.forced_status = 429
        result = self.run_script("ask", "x", "-m", "neon")
        self.assertEqual(result.returncode, 1)
        self.assertIn("billing-enabled key", result.stderr)

    # -- setup ------------------------------------------------------------------

    def test_missing_key_explains_setup(self):
        env = {k: v for k, v in self.env.items() if k != "GEMINI_API_KEY"}
        result = subprocess.run([sys.executable, str(SCRIPT), "ask", "x"], capture_output=True,
                                text=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("GEMINI_API_KEY", result.stderr)

    def test_doctor(self):
        result = self.run_script("doctor")
        self.assertOk(result)
        self.assertIn("found in $GEMINI_API_KEY", result.stdout)
        self.assertNotIn(KEY, result.stdout)
        self.assertIn("reachable", result.stdout)

    def test_catalog_command_creates_editable_copy(self):
        result = self.run_script("catalog")
        self.assertOk(result)
        path = Path(result.stdout.strip())
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).startswith(str(self.tmp)))
        edited = json.loads(path.read_text())
        edited["verified_on"] = "2999-01-01"
        edited["models"]["gemini-4-neon"]["input"] = 9.0
        path.write_text(json.dumps(edited))
        self.assertIn("$9/$15", self.run_script("check", "neon").stdout)  # the newer user copy wins


if __name__ == "__main__":
    unittest.main()
