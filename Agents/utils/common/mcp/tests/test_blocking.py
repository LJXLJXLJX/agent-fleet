import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "exa"
sys.path.insert(0, str(SOURCE))
from blocking import BLOCKED, LeakPolicy

spec = importlib.util.spec_from_file_location("exa_proxy_test", SOURCE / "__main__.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
QUESTION = "Which coastal city hosted the international astronomy conference during June 1995?"


def request(name="web_search", **arguments):
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def result(text="Independent scientific source"):
    return {"jsonrpc": "2.0", "id": 7,
            "result": {"content": [{"type": "text", "text": text}]}}


class BlockingTest(unittest.TestCase):
    def setUp(self):
        self.policy = LeakPolicy("browsecomp", f"Shared formatting instructions\n\nQuestion:\n{QUESTION}")

    def test_presets_and_missing_question(self):
        self.assertEqual((self.policy.question_size, self.policy.query_size), (5, 5))
        deep = LeakPolicy("deepsearchqa", QUESTION)
        self.assertEqual((deep.question_size, deep.query_size), (8, 6))
        self.assertFalse(deep.blocked_response(result("Which coastal city hosted the")))
        self.assertTrue(self.policy.blocked_response(result("Which coastal city hosted the")))
        with self.assertRaises(ValueError):
            LeakPolicy("browsecomp", "")
        with self.assertRaises(KeyError):
            LeakPolicy("typo", QUESTION)

    def test_request_blocking_precedes_network(self):
        for value in ("BrowseComp answers", "DEEP-search_QA", "\uff22\uff52\uff4f\uff57\uff53\uff45\uff23\uff4f\uff4d\uff50", "%62rowse%63omp"):
            with self.subTest(value=value), patch.object(proxy, "_request") as network:
                response = proxy._forward(request(query=value), self.policy)
                self.assertEqual(response, self.policy.response(request()))
                network.assert_not_called()
        with patch.object(proxy, "_request") as network:
            proxy._forward(request("web_fetch", url="https://example.org/browse%63omp"), self.policy)
            network.assert_not_called()

    def test_configured_url_and_result_metadata_are_checked(self):
        with patch("blocking.BLOCKED_URLS", ("example.org/answer-archive",)):
            self.assertTrue(self.policy.blocked_request({"url": "https://EXAMPLE.org/answer%2Darchive"}))
        response = result()
        response["result"]["structuredContent"] = {"reference": QUESTION}
        with patch.object(proxy, "_request", return_value=response):
            filtered = proxy._forward(request("web_fetch", url="https://example.org"), self.policy)
        self.assertEqual(filtered["result"]["content"][0]["text"], BLOCKED)
        self.assertNotIn("structuredContent", filtered["result"])
        response = result()
        response["result"]["_meta"] = {"text": "deepsearchqa answer archive"}
        self.assertTrue(self.policy.blocked_response(response))

    def test_question_and_query_ngrams_are_scoped(self):
        self.assertTrue(self.policy.blocked_response(result(QUESTION.replace("city", "city\x00"))))
        other = LeakPolicy("browsecomp", "How many chemical elements were discovered by the research institute?")
        self.assertFalse(other.blocked_response(result(QUESTION)))
        query = "astronomy meeting records in late June"
        response = result("Page repeats astronomy meeting records in late June exactly")
        with patch.object(proxy, "_request", return_value=response):
            self.assertTrue(proxy._forward(request(query=query), self.policy)["result"]["isError"])
            self.assertEqual(proxy._forward(request("web_fetch", url="https://example.org"), self.policy), response)

    def test_scan_before_truncation_and_reject_binary_content(self):
        self.assertTrue(self.policy.blocked_response(result("x" * 500_000 + " " + QUESTION)))
        response = result()
        response["result"]["content"] = [{"type": "image", "data": "opaque", "mimeType": "image/png"}]
        self.assertTrue(self.policy.blocked_response(response))

    def test_safe_results_are_unchanged(self):
        response = result("Mercury orbital period: 88 days")
        with patch.object(proxy, "_request", return_value=response) as network:
            self.assertEqual(proxy._forward(request(query="Mercury orbit"), self.policy), response)
            self.assertEqual(network.call_args.args[0]["params"]["name"], "web_search_exa")

    def test_only_filtered_tools_are_exposed(self):
        response = {"result": {"tools": [
            {"name": "web_search_exa", "description": "Search"},
            {"name": "web_fetch_exa", "description": "Fetch"},
            {"name": "new_unfiltered_tool", "description": "Other"},
        ]}}
        with patch.object(proxy, "_request", return_value=response):
            listing = proxy._forward({"id": 1, "method": "tools/list"}, self.policy)
        self.assertEqual([t["name"] for t in listing["result"]["tools"]], ["web_search", "web_fetch"])
        self.assertIn("not evidence of relevance", listing["result"]["tools"][0]["description"])
        with patch.object(proxy, "_request") as network:
            proxy._forward(request("new_unfiltered_tool"), self.policy)
            unsupported = proxy._forward({"id": 1, "method": "resources/read"}, self.policy)
            self.assertEqual(unsupported["error"]["code"], -32601)
            network.assert_not_called()

    def test_notifications_and_errors_cannot_bypass_policy(self):
        no_id = request(query="browsecomp answers")
        no_id.pop("id")
        messages = [no_id, request(query="Mercury orbit")]
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"WEB_MCP_INSTRUCTION": QUESTION}),
            patch.object(sys, "stdin", io.StringIO("\n".join(map(json.dumps, messages)))),
            patch.object(proxy, "_request", side_effect=ValueError("private upstream content")) as network,
            contextlib.redirect_stdout(output),
        ):
            proxy.main()
        self.assertEqual(network.call_count, 1)
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"]["message"], "ValueError")
        self.assertNotIn("private upstream content", output.getvalue())


if __name__ == "__main__":
    unittest.main()
