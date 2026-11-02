import base64
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rebind_fabric_workspace import (
    ApiError, FabricClient, Snapshot, apply_plans, build_body,
    choose_mappings, comparable_parts, guid, main, read_documents,
    reference_status, rewrite_document, run, text_references,
)

OLD_WS = "11111111-1111-1111-1111-111111111111"
WORKSPACE = "22222222-2222-2222-2222-222222222222"
OLD_ITEM = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NEW_ITEM = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OTHER_ITEM = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OWNER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OPERATION = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
TARGET = {"id": NEW_ITEM, "displayName": "Destination Gold", "type": "Lakehouse"}
MODEL = {"id": OTHER_ITEM, "displayName": "Destination Model", "type": "SemanticModel"}
NOTEBOOK = {"id": OWNER, "displayName": "Notebook", "type": "Notebook"}
OLD_URL = f"https://onelake.dfs.fabric.microsoft.com/{OLD_WS}/{OLD_ITEM}"
NEW_URL = f"https://onelake.dfs.fabric.microsoft.com/{WORKSPACE}/{NEW_ITEM}"


def part(path, value):
    text = json.dumps(value) if not isinstance(value, str) else value
    return {"path": path, "payloadType": "InlineBase64",
            "payload": base64.b64encode(text.encode()).decode()}


def live_definition(*parts):
    return {"definition": {"parts": [
        *parts, part(".platform", {"config": {"logicalId": OLD_ITEM}})]}}


def decoded(part_value):
    return base64.b64decode(part_value["payload"]).decode("utf-8-sig")


def document(path, value):
    live = live_definition(part(path, value))
    docs, warnings = read_documents(live)
    if warnings:
        raise AssertionError(warnings)
    return docs[0]


class FakeClient:
    def __init__(self):
        self.items = [NOTEBOOK, TARGET, MODEL]
        self.live = live_definition(part("notebook-content.ipynb", {
            "metadata": {"dependencies": {"lakehouse": {
                "default_lakehouse": OLD_ITEM,
                "default_lakehouse_workspace_id": OLD_WS,
                "default_lakehouse_name": "Old Gold"}}},
            "cells": [{"source": [f'path = "{OLD_URL}/Tables/dbo/store"\n']}],
        }))
        self.updates = []
        self.reads = 0
        self.fail_update = False
        self.ignore_update = False
        self.concurrent = False

    def listing(self, path):
        return copy.deepcopy(self.items)

    def call(self, method, path, body=None, **kwargs):
        if method == "GET":
            if path == f"workspaces/{WORKSPACE}":
                return {"displayName": "Destination"}
            raise ApiError(404, "ItemNotFound")
        if "getDefinition" in path:
            if f"/{OWNER}/" not in path:
                raise ApiError(400, "Item type does not support definitions")
            self.reads += 1
            result = copy.deepcopy(self.live)
            if self.concurrent and self.reads > 1:
                result["definition"]["parts"].append(part("added.txt", "Concurrent edit"))
            return result
        if path.endswith("/updateDefinition"):
            self.updates.append(copy.deepcopy(body))
            if self.fail_update:
                raise ApiError(400, "Import failed")
            if not self.ignore_update:
                self.live = copy.deepcopy(body)
            return {}
        raise AssertionError((method, path))


class ReferenceTests(unittest.TestCase):
    def test_guid_validation(self):
        self.assertEqual(guid(OLD_ITEM.upper()), OLD_ITEM)
        for value in (None, 42, "not-a-guid", f"prefix{OLD_ITEM}"):
            self.assertIsNone(guid(value))

    def test_json_ontology_binding_and_relationship_sources(self):
        for parent in ("sourceTableProperties", "dataBindingTable"):
            value = {parent: {"workspaceId": OLD_WS, "itemId": OLD_ITEM,
                              "sourceType": "LakehouseTable", "sourceTableName": "dim_store",
                              "sourceSchema": "dbo"},
                     "id": OLD_ITEM, "lineageTag": OLD_ITEM, "targetPropertyId": OLD_ITEM}
            doc = document("binding.json", value)
            self.assertEqual(len(doc.references), 1)
            self.assertEqual(doc.references[0].kinds, ("Lakehouse",))
            changed = json.loads(decoded(rewrite_document(doc, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)))
            self.assertEqual(changed[parent]["workspaceId"], WORKSPACE)
            self.assertEqual(changed[parent]["itemId"], NEW_ITEM)
            self.assertEqual(changed["id"], OLD_ITEM)
            self.assertEqual(changed["lineageTag"], OLD_ITEM)
            self.assertEqual(changed["targetPropertyId"], OLD_ITEM)

    def test_pipeline_notebook_reference(self):
        value = {"properties": {"activities": [{"typeProperties": {
            "notebookId": OLD_ITEM, "workspaceId": OLD_WS}}]}}
        doc = document("pipeline-content.json", value)
        self.assertEqual(doc.references[0].kinds, ("Notebook",))
        target = {**TARGET, "type": "Notebook"}
        new = json.loads(decoded(rewrite_document(doc, {(OLD_WS, OLD_ITEM): target}, WORKSPACE)))
        self.assertEqual(new["properties"]["activities"][0]["typeProperties"],
                         {"notebookId": NEW_ITEM, "workspaceId": WORKSPACE})

    def test_notebook_default_lakehouse_and_name(self):
        doc = document("notebook.ipynb", {"metadata": {"dependencies": {"lakehouse": {
            "default_lakehouse": OLD_ITEM, "default_lakehouse_workspace_id": OLD_WS,
            "default_lakehouse_name": "Old"}}}})
        result = json.loads(decoded(rewrite_document(doc, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)))
        self.assertEqual(result["metadata"]["dependencies"]["lakehouse"], {
            "default_lakehouse": NEW_ITEM, "default_lakehouse_workspace_id": WORKSPACE,
            "default_lakehouse_name": "Destination Gold"})

    def test_known_notebook_lakehouses(self):
        doc = document("notebook.ipynb", {"lakehouse": {"known_lakehouses": [
            {"id": OLD_ITEM, "workspaceId": OLD_WS, "name": "Old"}]}})
        result = json.loads(decoded(rewrite_document(doc, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)))
        self.assertEqual(result["lakehouse"]["known_lakehouses"][0]["name"], "Destination Gold")

    def test_urls_preserve_suffixes_and_do_not_touch_unrelated_guids(self):
        texts = [
            OLD_URL + "/Tables/dbo/dim_store",
            f"abfss://{OLD_WS}@onelake.dfs.fabric.microsoft.com/{OLD_ITEM}/Tables/dbo/store",
            f"https://app.fabric.microsoft.com/groups/{OLD_WS}/reports/{OLD_ITEM}?page=1",
            f"https://app.fabric.microsoft.com/onelake/details/{OLD_WS}/dataset/{OLD_ITEM}/",
            f"https://api.fabric.microsoft.com/v1/workspaces/{OLD_WS}/items/{OLD_ITEM}",
        ]
        for text in texts:
            doc = document("expressions.tmdl", text + f"\nlineageTag: {OLD_ITEM}\n")
            self.assertEqual(len(doc.references), 1, text)
            changed = decoded(rewrite_document(doc, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE))
            self.assertIn(WORKSPACE, changed)
            self.assertIn(NEW_ITEM, changed)
            self.assertIn(f"lineageTag: {OLD_ITEM}", changed)
            self.assertTrue(changed.endswith(f"\nlineageTag: {OLD_ITEM}\n"))
        self.assertEqual(text_references(f"https://example.com/{OLD_WS}/{OLD_ITEM}", ()), [])

    def test_tmdl_resource_link_siblings_and_multiple_blocks(self):
        text = (f"entity Store\n\tresourceLink\n\t\titem\n\t\t\tworkspaceId: {OLD_WS}\n"
                f"\t\t\titemId: {OLD_ITEM}\n\n\tresourceLink\n\t\titem\n"
                f"\t\t\tworkspaceId: {WORKSPACE}\n\t\t\titemId: {OTHER_ITEM}\n")
        doc = document("entities/Store.tmdl", text)
        self.assertEqual([r.key for r in doc.references],
                         [(OLD_WS, OLD_ITEM), (WORKSPACE, OTHER_ITEM)])
        changed = decoded(rewrite_document(doc, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE))
        self.assertNotIn(OLD_WS, changed)
        self.assertIn(OTHER_ITEM, changed)

    def test_semantic_model_id_in_report_connection_string(self):
        doc = document("definition.pbir", {"datasetReference": {"byConnection": {
            "connectionString": f"semanticmodelid={OLD_ITEM}"}}})
        self.assertEqual(doc.references[0].key, (None, OLD_ITEM))
        result = decoded(rewrite_document(doc, {(None, OLD_ITEM): MODEL}, WORKSPACE))
        self.assertIn(OTHER_ITEM, result)

    def test_pbir_long_connection_drops_old_server_and_catalog(self):
        doc = document("definition.pbir", {"version": "4.0", "datasetReference": {"byConnection": {
            "connectionString": f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/Old;"
                                f"Initial Catalog=Old;semanticModelId={OLD_ITEM}",
            "pbiServiceVirtualServerName": "old"}}})
        result = json.loads(decoded(rewrite_document(doc, {(None, OLD_ITEM): MODEL}, WORKSPACE)))
        self.assertEqual(result["datasetReference"],
                         {"byConnection": {"connectionString": f"semanticmodelid={OTHER_ITEM}"}})
        self.assertEqual(result["version"], "4.0")

    def test_case_insensitive_guids(self):
        doc = document("source.tmdl", OLD_URL.upper().replace("HTTPS", "https"))
        self.assertEqual(doc.references[0].key, (OLD_WS, OLD_ITEM))

    def test_unselected_and_binary_parts_and_platform_identity_are_preserved(self):
        live = live_definition(part("source.tmdl", OLD_URL), {
            "path": "image.png", "payloadType": "InlineBase64", "payload": "/w=="},
            part("other.tmdl", f"lineageTag: {OLD_ITEM}"))
        before = copy.deepcopy(live)
        docs, warnings = read_documents(live)
        self.assertEqual(len(warnings), 1)
        body = build_body(Snapshot(NOTEBOOK, live, docs), {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)
        self.assertEqual(body["definition"]["parts"][1:],
                         live["definition"]["parts"][1:3])
        self.assertEqual(live, before)
        self.assertNotIn(".platform", [p["path"] for p in body["definition"]["parts"]])

    def test_non_inline_payload_is_preserved_and_reported(self):
        live = live_definition({"path": "external.json", "payloadType": "Other", "payload": "uri"})
        docs, warnings = read_documents(live)
        self.assertEqual(docs, [])
        self.assertIn("unsupported payloadType", warnings[0])
        self.assertEqual(comparable_parts(live)["external.json"], ("Other", "uri"))

    def test_duplicate_parts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            read_documents(live_definition(part("same.json", {}), part("same.json", {})))

    def test_dynamic_workspace_and_endpoint_metadata_are_blocked(self):
        for source in (
            {"workspaceId": "@pipeline().parameters.workspace", "itemId": OLD_ITEM},
            {"workspaceId": OLD_WS, "itemId": OLD_ITEM, "clusterUri": "old.kusto.windows.net"},
        ):
            doc = document("source.json", source)
            self.assertTrue(doc.references[0].blocked_reason)
            with self.assertRaises(ValueError):
                rewrite_document(doc, {doc.references[0].key: TARGET}, WORKSPACE)

    def test_shared_workspace_is_not_changed_for_unselected_reference(self):
        doc = document("source.json", {"workspaceId": OLD_WS,
                                       "lakehouseId": OLD_ITEM, "notebookId": OTHER_ITEM})
        with self.assertRaisesRegex(ValueError, "shared workspace"):
            rewrite_document(doc, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)

    def test_referenced_guid_in_part_path_requires_manual_migration(self):
        live = live_definition(part(f"Files/Config/{OLD_ITEM}/source.json",
                                    {"workspaceId": OLD_WS, "itemId": OLD_ITEM}))
        docs, _ = read_documents(live)
        with self.assertRaisesRegex(ValueError, "part path"):
            build_body(Snapshot(NOTEBOOK, live, docs), {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_valid_local_and_cross_workspace_references_are_not_prompted(self):
        api = FakeClient()
        self.assertEqual(reference_status(api, WORKSPACE, api.items, (None, NEW_ITEM)), "valid")
        with patch.object(api, "call", return_value={"id": OLD_ITEM}):
            self.assertEqual(reference_status(api, WORKSPACE, api.items, (OLD_WS, OLD_ITEM)), "valid")

    def test_access_denied_is_not_classified_as_deleted(self):
        api = FakeClient()
        with patch.object(api, "call", side_effect=ApiError(403, "Denied")):
            self.assertIn("unverified access", reference_status(api, WORKSPACE, [], (OLD_WS, OLD_ITEM)))

    def test_compatible_targets_and_confirmation(self):
        api = FakeClient()
        doc = document("source.json", {"workspaceId": OLD_WS, "lakehouseId": OLD_ITEM})
        snapshot = Snapshot(NOTEBOOK, {}, [doc])
        groups = {(OLD_WS, OLD_ITEM): [(snapshot, doc, doc.references[0])]}
        replies = iter(["99", "x", "1", "no", "1", "yes"])
        mappings, problems = choose_mappings(api, WORKSPACE, api.items, groups, lambda _: next(replies))
        self.assertEqual(mappings, {(OLD_WS, OLD_ITEM): TARGET})
        self.assertEqual(problems, [])

    def test_cancellation_has_no_writes(self):
        api = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            replies = iter(["1", "yes", "no"])
            status = run(api, WORKSPACE, Path(folder), ask=lambda _: next(replies))
            self.assertEqual(status, 0)
            self.assertEqual(api.updates, [])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_complete_interactive_run_backups_and_verified_readback(self):
        api = FakeClient()
        # Inspect only the notebook; keep its reference target in the inventory.
        original_call = api.call

        def all_definitions(method, path, body=None, **kwargs):
            if "getDefinition" in path and f"/{OWNER}/" not in path:
                return live_definition(part("empty.json", {}))
            return original_call(method, path, body, **kwargs)

        api.call = all_definitions
        with tempfile.TemporaryDirectory() as folder:
            replies = iter(["1", "yes", "yes"])
            status = run(api, WORKSPACE, Path(folder), ask=lambda _: next(replies))
            self.assertEqual(status, 0)
            self.assertEqual(len(api.updates), 1)
            self.assertEqual(len(list(Path(folder).glob("*/*.before.json"))), 1)
            journal = json.loads(next(Path(folder).glob("*/results.json")).read_text())
            self.assertEqual(journal["items"][0]["status"], "verified")
        self.assertNotIn(OLD_URL, decoded(api.live["definition"]["parts"][0]))

    def test_scan_only_does_not_prompt_or_write(self):
        api = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            status = run(api, WORKSPACE, Path(folder), scan_only=True,
                         ask=lambda _: self.fail("Unexpected prompt"))
        self.assertEqual(status, 2)
        self.assertEqual(api.updates, [])

    def test_workspace_is_the_first_interactive_question(self):
        with patch("sys.argv", ["rebind_fabric_workspace.py"]), \
                patch("builtins.input", side_effect=["not-a-guid", WORKSPACE]) as ask, \
                patch("rebind_fabric_workspace.run", return_value=0) as runner:
            self.assertEqual(main(), 0)
        self.assertIn("workspace ID", ask.call_args_list[0].args[0])
        self.assertEqual(runner.call_args.args[1], WORKSPACE)

    def test_apply_stops_on_concurrent_edit_and_logs_failure(self):
        api = FakeClient()
        docs, _ = read_documents(api.live)
        snapshot = Snapshot(NOTEBOOK, copy.deepcopy(api.live), docs)
        body = build_body(snapshot, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)
        api.reads = 1
        api.concurrent = True
        with tempfile.TemporaryDirectory() as folder:
            failures = apply_plans(api, WORKSPACE, [(snapshot, body)], Path(folder))
            self.assertIn("changed since scan", failures[0])
        self.assertEqual(api.updates, [])

    def test_failed_or_ignored_updates_are_not_reported_as_verified(self):
        for fail_update in (True, False):
            api = FakeClient()
            api.fail_update = fail_update
            api.ignore_update = not fail_update
            docs, _ = read_documents(api.live)
            snapshot = Snapshot(NOTEBOOK, copy.deepcopy(api.live), docs)
            body = build_body(snapshot, {(OLD_WS, OLD_ITEM): TARGET}, WORKSPACE)
            with tempfile.TemporaryDirectory() as folder:
                failures = apply_plans(api, WORKSPACE, [(snapshot, body)], Path(folder))
                self.assertEqual(len(failures), 1)
                journal = json.loads(next(Path(folder).glob("*/results.json")).read_text())
                self.assertEqual(journal["items"][0]["status"], "failed_or_unverified")
            self.assertEqual(len(api.updates), 1)


class TransportTests(unittest.TestCase):
    def test_lro_polling_and_result(self):
        api = FabricClient()
        live = live_definition(part("file.json", {}))
        with patch.object(api, "request", side_effect=[
            (202, {"x-ms-operation-id": OPERATION}, None),
            (200, {}, {"status": "Running"}),
            (200, {}, {"status": "Succeeded"}),
            (200, {}, live),
        ]) as request, patch("rebind_fabric_workspace.time.sleep"):
            result = api.call("POST", "items/getDefinition", {}, result=True, skill="fabriciq-ontology-cli")
        self.assertEqual(result, live)
        self.assertEqual(request.call_args.kwargs["skill"], "fabriciq-ontology-cli")

    def test_failed_async_operation_is_not_retried(self):
        api = FabricClient()
        with patch.object(api, "request", side_effect=[
            (202, {"x-ms-operation-id": OPERATION}, None),
            (200, {}, {"status": "Failed", "error": {"message": "old artifact"}}),
        ]) as request, patch("rebind_fabric_workspace.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "old artifact"):
                api.call("POST", "updateDefinition", {})
        self.assertEqual(request.call_count, 2)

    def test_pagination_by_uri_or_token(self):
        for page in (
            {"value": [1], "continuationUri": "https://api.fabric.microsoft.com/v1/next"},
            {"value": [1], "continuationToken": "a+b/c"},
        ):
            api = FabricClient()
            with patch.object(api, "call", side_effect=[page, {"value": [2]}]) as call:
                self.assertEqual(api.listing("workspaces/ws/items?type=Lakehouse"), [1, 2])
            if "continuationToken" in page:
                self.assertIn("&continuationToken=a%2Bb%2Fc", call.call_args.args[1])

    def test_api_host_guard_precedes_token_acquisition(self):
        api = FabricClient()
        with patch.object(api, "token") as token:
            for url in ("https://example.com/v1/items", "https://api.fabric.microsoft.com.evil/v1/items"):
                with self.assertRaises(ValueError):
                    api.request("GET", url)
        token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
