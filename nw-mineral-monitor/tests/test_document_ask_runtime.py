import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
sys.path.insert(0, str(INFRA))


class FakeClientError(Exception):
    def __init__(self, code="AccessDeniedException"):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeBedrock:
    def __init__(self):
        self.response = {}
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeCognito:
    def get_user(self, **kwargs):
        return {"Username": "fixture"}


fake_bedrock = FakeBedrock()
fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda name: fake_bedrock if name == "bedrock-runtime" else FakeCognito()
fake_exceptions = types.ModuleType("botocore.exceptions")
fake_exceptions.ClientError = FakeClientError
fake_botocore = types.ModuleType("botocore")
fake_botocore.exceptions = fake_exceptions

def _load_ask_lambda(enable_legacy_doc_store=None):
    """Import a fresh relay module under the current environment.

    The tool set and system prompt are built at import time from
    ENABLE_LEGACY_DOC_STORE, so proving both sides of that switch means
    importing twice rather than mutating the module afterwards.
    """
    # Deployment preflight inherits ENABLE_LEGACY_DOC_STORE from the operator.
    # Isolate the module's default test from that outer setting while still
    # testing the explicit false branch below.
    with mock.patch.dict(os.environ), mock.patch.dict(sys.modules, {
                "boto3": fake_boto3,
                "botocore": fake_botocore,
                "botocore.exceptions": fake_exceptions,
        }):
        if enable_legacy_doc_store is None:
            os.environ.pop("ENABLE_LEGACY_DOC_STORE", None)
        else:
            os.environ["ENABLE_LEGACY_DOC_STORE"] = enable_legacy_doc_store
        sys.modules.pop("ask_lambda", None)
        module = importlib.import_module("ask_lambda")
        sys.modules.pop("ask_lambda", None)
    return module


ask_lambda = _load_ask_lambda()


def conversation_with_search_result():
    return [
        {"role": "user", "content": [{"text": "What was production?"}]},
        {"role": "assistant", "content": [{"toolUse": {
            "toolUseId": "tool-1", "name": "search_documents",
            "input": {"query": "production", "mine_id": "IF0126"}}}]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": "tool-1", "content": [{"json": {
                "status": "loaded", "hits": [{
                    "excerpt": "Production totaled 250 tons.",
                    "citation": {
                        "document_title": "MILS property record IF0126",
                        "page": 2,
                        "source_url": "https://pubs.usgs.gov/if0126.pdf",
                    },
                }],
            }}]}}]},
    ]


class DocumentAskRuntimeTests(unittest.TestCase):
    def setUp(self):
        ask_lambda.ALLOW_ANON = True
        ask_lambda._working["id"] = None
        fake_bedrock.calls.clear()

    def event(self, body):
        return {"requestContext": {"http": {"method": "POST"}},
                "headers": {}, "body": json.dumps(body)}

    def test_tool_contract_includes_bounded_document_search(self):
        specs = {row["toolSpec"]["name"]: row["toolSpec"] for row in ask_lambda.TOOLS}
        self.assertIn("search_documents", specs)
        self.assertEqual(specs["search_documents"]["inputSchema"]["json"]
                         ["properties"]["limit"]["maximum"], 12)
        self.assertIn("[document title, p. N](source_url)", ask_lambda.SYSTEM)

    def test_the_stored_document_viewer_is_offered_by_default(self):
        # The store builder refuses to read the bytes of a row without an
        # affirmative public-domain basis, so nothing unresolved can reach the
        # tool set. Serving is therefore the default, and the switch below is
        # a deployment's way to withhold it rather than a rights gate.
        specs = {row["toolSpec"]["name"] for row in ask_lambda.TOOLS}
        self.assertIn("open_doc", specs)
        self.assertIn("Use open_doc", ask_lambda.SYSTEM)
        self.assertTrue(ask_lambda.ENABLE_LEGACY_DOC_STORE)

    def test_a_deployment_can_still_withhold_the_stored_document_viewer(self):
        withheld = _load_ask_lambda("false")
        specs = {row["toolSpec"]["name"] for row in withheld.TOOLS}
        self.assertNotIn("open_doc", specs)
        self.assertNotIn("Use open_doc", withheld.SYSTEM)
        self.assertIn("search_documents", specs)

    def test_site_sync_protects_private_ws12_and_original_prefixes(self):
        script = (ROOT / "infra" / "deploy.sh").read_text(encoding="utf-8")
        sync = script.split("sync_public_site_without_pointers()", 1)[1].split(
            "upload_doc_store()", 1)[0]
        for prefix in ("private/*", "ws12/*", "originals/*", "staging/*"):
            self.assertIn(f'--exclude "{prefix}"', sync)
        self.assertIn("remove_disabled_legacy_document_assets", script)

    def test_cloudfront_policy_cannot_read_private_databases_or_originals(self):
        template = (ROOT / "infra" / "template.yaml").read_text(encoding="utf-8")
        policy = template.split("  SiteBucketPolicy:", 1)[1].split(
            "\n  UpdaterRole:", 1)[0]
        self.assertNotIn("${SiteBucket.Arn}/*", policy)
        self.assertNotIn("${SiteBucket.Arn}/private/", policy)
        self.assertNotIn("${SiteBucket.Arn}/ws12/", policy)
        self.assertNotIn("${SiteBucket.Arn}/originals/", policy)
        for public_path in ("index.html", "auth.json", "assets/*", "data/*"):
            self.assertIn(f"${{SiteBucket.Arn}}/{public_path}", policy)

    def test_http_apis_bind_tokens_to_this_cognito_pool_and_client(self):
        template = (ROOT / "infra" / "template.yaml").read_text(encoding="utf-8")
        for name, following in (("AskAuthorizer", "AskIntegration"),
                                ("DocsAuthorizer", "DocsIntegration")):
            section = template.split(f"  {name}:", 1)[1].split(
                f"\n  {following}:", 1)[0]
            self.assertIn("AuthorizerType: JWT", section)
            self.assertIn("'$request.header.x-auth-token'", section)
            self.assertIn("Audience: [ !Ref UserPoolClient ]", section)
            self.assertIn("${UserPool}", section)
        for name, following in (("AskRoute", "AskStage"),
                                ("DocsRoute", "DocsStage")):
            section = template.split(f"  {name}:", 1)[1].split(
                f"\n  {following}:", 1)[0]
            self.assertIn("AuthorizationType: JWT", section)
            self.assertIn("AuthorizerId:", section)

    def test_generic_local_tool_mode_is_authenticated_dispatch_boundary(self):
        with mock.patch.object(ask_lambda, "execute_local_tool",
                               return_value={"status": "loaded", "count": 1}) as execute:
            response = ask_lambda.handler(self.event({"localTool": {
                "name": "search_documents", "input": {"query": "ore"}}}), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["result"]["count"], 1)
        execute.assert_called_once_with("search_documents", {"query": "ore"})

        with mock.patch.object(ask_lambda, "execute_local_tool",
                               side_effect=ValueError("unknown local tool nope")):
            response = ask_lambda.handler(self.event({"localTool": {
                "name": "nope", "input": {}}}), None)
        self.assertEqual(response["statusCode"], 400)

    def test_document_answer_without_resolvable_citation_is_withheld(self):
        fake_bedrock.response = {
            "output": {"message": {"role": "assistant", "content": [
                {"text": "Production totaled 250 tons."}]}},
            "stopReason": "end_turn", "usage": {},
        }
        response = ask_lambda.handler(
            self.event({"messages": conversation_with_search_result()}), None)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(body["citationGuarded"])
        text = body["message"]["content"][0]["text"]
        self.assertIn("withheld", text)
        self.assertIn("[MILS property record IF0126, p. 2]", text)
        self.assertIn("https://pubs.usgs.gov/if0126.pdf", text)

    def test_exact_title_page_url_citation_passes_guard(self):
        answer = ("Production totaled 250 tons "
                  "[MILS property record IF0126, p. 2]"
                  "(https://pubs.usgs.gov/if0126.pdf).")
        fake_bedrock.response = {
            "output": {"message": {"role": "assistant", "content": [{"text": answer}]}},
            "stopReason": "end_turn", "usage": {},
        }
        response = ask_lambda.handler(
            self.event({"messages": conversation_with_search_result()}), None)
        body = json.loads(response["body"])
        self.assertFalse(body["citationGuarded"])
        self.assertEqual(body["message"]["content"][0]["text"], answer)

    def test_prior_document_result_does_not_guard_unrelated_later_turn(self):
        messages = conversation_with_search_result() + [
            {"role": "assistant", "content": [{"text":
                "Production totaled 250 tons [MILS property record IF0126, p. 2]"
                "(https://pubs.usgs.gov/if0126.pdf)."}]},
            {"role": "user", "content": [{"text":
                "Now tell me which geology map covers Jackson."}]},
        ]
        answer = "The current geology tool result names the Jackson source map."
        fake_bedrock.response = {
            "output": {"message": {"role": "assistant", "content": [{"text": answer}]}},
            "stopReason": "end_turn", "usage": {},
        }
        response = ask_lambda.handler(self.event({"messages": messages}), None)
        body = json.loads(response["body"])
        self.assertFalse(body["citationGuarded"])
        self.assertEqual(body["message"]["content"][0]["text"], answer)

    def test_dispatch_can_route_document_and_spatial_modules(self):
        fake_document = types.ModuleType("document_tools")
        fake_document.TOOL_NAMES = frozenset({"search_documents"})
        fake_document.execute = lambda name, args: {"module": "document", "name": name}
        fake_spatial = types.ModuleType("spatial_tools")
        fake_spatial.TOOL_NAMES = frozenset({"geology_at"})
        fake_spatial.execute = lambda name, args: {"module": "spatial", "name": name}
        with mock.patch.dict(sys.modules, {
                "document_tools": fake_document, "spatial_tools": fake_spatial}):
            self.assertEqual(ask_lambda.execute_local_tool(
                "search_documents", {})["module"], "document")
            self.assertEqual(ask_lambda.execute_local_tool(
                "geology_at", {})["module"], "spatial")


if __name__ == "__main__":
    unittest.main()
