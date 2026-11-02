"""Interactively repair recognized Fabric item references after a Git restore.

Python 3.10+ and Azure CLI are required. No extra Python packages are needed.
Run az login --tenant <tenant-id> --allow-no-subscriptions first.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

BASE = "https://api.fabric.microsoft.com/v1/"
GUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
ID_FIELDS = {
    "itemid": (), "artifactid": (), "lakehouseid": ("Lakehouse",),
    "default_lakehouse": ("Lakehouse",), "notebookid": ("Notebook",),
    "datasetid": ("SemanticModel",), "semanticmodelid": ("SemanticModel",),
    "reportid": ("Report",), "warehouseid": ("Warehouse",),
    "pipelineid": ("DataPipeline",), "ontologyid": ("Ontology",),
    "eventhouseid": ("Eventhouse",), "kqldatabaseid": ("KQLDatabase",),
    "sqlendpointid": ("SQLEndpoint",), "environmentid": ("Environment",),
}
TYPE_HINTS = {
    "LakehouseTable": ("Lakehouse",), "LakehouseTables": ("Lakehouse",),
    "PowerBIReport": ("Report",), "KustoTable": ("Eventhouse",),
}
ROUTES = {
    "lakehouses": ("Lakehouse",), "warehouses": ("Warehouse",),
    "datasets": ("SemanticModel",), "semanticmodels": ("SemanticModel",),
    "reports": ("Report",), "notebooks": ("Notebook",),
    "datapipelines": ("DataPipeline",), "ontologies": ("Ontology",),
    "eventhouses": ("Eventhouse",), "kqldatabases": ("KQLDatabase",),
    "items": (),
}
ONELAKE_TYPES = ("Lakehouse", "Warehouse", "MirroredDatabase", "MirroredWarehouse")
URL_PATTERNS = [
    (re.compile(
        rf"(?:https://onelake\.(?:dfs|blob)\.fabric\.microsoft\.com/"
        rf"(?P<ws>{GUID})/|abfss://(?P<abfsws>{GUID})@"
        rf"onelake\.dfs\.fabric\.microsoft\.com/)(?P<item>{GUID})(?![0-9a-fA-F-])",
        re.I), ONELAKE_TYPES),
    (re.compile(
        rf"https://(?:app\.fabric\.microsoft\.com|app\.powerbi\.com)/"
        rf"groups/(?P<ws>{GUID})/(?P<route>{'|'.join(ROUTES)})/"
        rf"(?P<item>{GUID})(?![0-9a-fA-F-])", re.I), ()),
    (re.compile(
        rf"https://app\.fabric\.microsoft\.com/onelake/details/(?P<ws>{GUID})/"
        rf"(?P<route>dataset|report|lakehouse)/(?P<item>{GUID})(?![0-9a-fA-F-])",
        re.I), ()),
    (re.compile(
        rf"https://api\.fabric\.microsoft\.com/v1/workspaces/(?P<ws>{GUID})/"
        rf"(?P<route>{'|'.join(ROUTES)})/(?P<item>{GUID})(?![0-9a-fA-F-])",
        re.I), ()),
]
TMDL_ITEM = re.compile(
    rf"^(?P<indent>[ \t]*)(?:{'|'.join(ID_FIELDS)}):[ \t]*['\"]?"
    rf"(?P<item>{GUID})(?![0-9a-fA-F-])", re.I | re.M)
WORKSPACE_LINE = re.compile(
    rf"^[ \t]*workspaceId:[ \t]*['\"]?(?P<ws>{GUID})", re.I)
MODEL_CONNECTION = re.compile(
    rf"\b(?:semanticmodelid|datasetid)[ \t]*=[ \t]*(?P<item>{GUID})(?![0-9a-fA-F-])",
    re.I)
TEXT_SUFFIXES = {".json", ".ipynb", ".tmdl", ".pbir", ".pbism", ".py", ".sql",
                 ".m", ".txt", ".yaml", ".yml", ".xml", ".kql", ".md"}


def guid(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(GUID, value):
        return None
    return str(UUID(value))


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"HTTP {status}: {detail}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("API redirected; refusing to forward credentials or replay a write.")


class FabricClient:
    def __init__(self, tenant: str | None = None):
        self.tenant = tenant
        self.cached_token: tuple[float, str] | None = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def token(self) -> str:
        if self.cached_token is None or self.cached_token[0] <= time.time():
            az = shutil.which("az")
            if not az:
                raise RuntimeError("Azure CLI is missing. Install it and run az login first.")
            command = [az, "account", "get-access-token", "--resource",
                       "https://api.fabric.microsoft.com", "--output", "json"]
            if self.tenant:
                command += ["--tenant", self.tenant]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            if result.returncode:
                raise RuntimeError("Azure login/token acquisition failed:\n" + result.stderr)
            self.cached_token = (time.time() + 2400, json.loads(result.stdout)["accessToken"])
        return self.cached_token[1]

    def request(self, method: str, path: str, body=None, skill="git-integration-operations-cli"):
        url = path if path.startswith("https://") else BASE + path
        if not url.startswith(BASE):
            raise ValueError(f"Refusing API URL outside {BASE}")
        headers = {"Authorization": "Bearer " + self.token(),
                   "Content-Type": "application/json", "x-ms-fabric-skill": skill}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(5):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=120) as response:
                    raw = response.read()
                    return response.status, response.headers, json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                if error.code == 429 and attempt < 4:
                    wait = max(1, int(error.headers.get("Retry-After", "30")))
                    print(f"Rate limited; waiting {wait}s.", flush=True)
                    time.sleep(wait)
                    continue
                raise ApiError(error.code, detail) from error
        raise RuntimeError("Rate-limit retries exhausted.")

    def call(self, method: str, path: str, body=None, *, result=False,
             skill="git-integration-operations-cli"):
        status, headers, response = self.request(method, path, body, skill=skill)
        if status != 202:
            return response
        operation = headers.get("x-ms-operation-id")
        if not operation:
            raise RuntimeError("202 without operation ID. Outcome unknown; do not resubmit blindly.")
        operation = str(UUID(operation))
        print(f"  Waiting for operation {operation}", flush=True)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            time.sleep(max(1, int(headers.get("Retry-After", "5"))))
            _, headers, current = self.request("GET", f"operations/{operation}", skill=skill)
            if current["status"] == "Succeeded":
                if result:
                    return self.call("GET", f"operations/{operation}/result", skill=skill)
                return current
            if current["status"] in {"Failed", "Cancelled"}:
                raise RuntimeError(f"Operation {operation}: {json.dumps(current)}")
        raise TimeoutError(f"Operation {operation} timed out. Do not resubmit blindly.")

    def listing(self, path: str) -> list[dict]:
        rows = []
        seen = set()
        original = path
        while path:
            if path in seen:
                raise RuntimeError("Repeated continuation page; refusing an incomplete inventory.")
            seen.add(path)
            page = self.call("GET", path)
            rows.extend(page["value"])
            path = page.get("continuationUri")
            if not path and page.get("continuationToken"):
                separator = "&" if "?" in original else "?"
                path = original + separator + urllib.parse.urlencode(
                    {"continuationToken": page["continuationToken"]})
        return rows


@dataclass(frozen=True)
class Location:
    path: tuple
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class Reference:
    workspace: str | None
    item: str
    kinds: tuple[str, ...]
    item_location: Location
    workspace_location: Location | None = None
    name_location: Location | None = None
    blocked_reason: str = ""

    @property
    def key(self) -> tuple[str | None, str]:
        return self.workspace, self.item


@dataclass
class Document:
    part: dict
    value: object
    is_json: bool
    bom: bool
    references: list[Reference]


@dataclass
class Snapshot:
    item: dict
    live: dict
    documents: list[Document]


def text_references(text: str, path: tuple) -> list[Reference]:
    references = []
    for pattern, kinds in URL_PATTERNS:
        for match in pattern.finditer(text):
            workspace_group = "abfsws" if match.groupdict().get("abfsws") else "ws"
            route = match.groupdict().get("route", "").lower()
            route = {"dataset": "datasets", "report": "reports", "lakehouse": "lakehouses"}.get(
                route, route)
            references.append(Reference(
                guid(match[workspace_group]), str(UUID(match["item"])),
                ROUTES.get(route, kinds), Location(path, match.span("item")),
                Location(path, match.span(workspace_group))))
    for match in MODEL_CONNECTION.finditer(text):
        references.append(Reference(None, str(UUID(match["item"])), ("SemanticModel",),
                                    Location(path, match.span("item"))))
    lines = text.splitlines(keepends=True)
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    for index, line in enumerate(lines):
        match = TMDL_ITEM.match(line)
        if not match:
            continue
        indent = len(match["indent"].expandtabs(4))
        workspace_location = None
        workspace = None
        # Only sibling properties in the same TMDL object may share a workspace.
        for direction in (-1, 1):
            other = index + direction
            while 0 <= other < len(lines):
                sibling = lines[other]
                if sibling.strip():
                    sibling_indent = len(sibling[:len(sibling) - len(sibling.lstrip())].expandtabs(4))
                    if sibling_indent < indent:
                        break
                    if sibling_indent == indent:
                        ws_match = WORKSPACE_LINE.match(sibling)
                        if ws_match:
                            workspace = guid(ws_match["ws"])
                            start, end = ws_match.span("ws")
                            workspace_location = Location(path, (offsets[other] + start,
                                                                  offsets[other] + end))
                            break
                        if ":" not in sibling:
                            break
                other += direction
            if workspace_location:
                break
        key = line.strip().split(":", 1)[0].lower()
        start, end = match.span("item")
        references.append(Reference(
            workspace, str(UUID(match["item"])), ID_FIELDS[key],
            Location(path, (offsets[index] + start, offsets[index] + end)), workspace_location))
    return references


def collect_references(value, path: tuple = (), parent: str = "") -> list[Reference]:
    references = []
    if isinstance(value, dict):
        keys = {key.lower(): key for key in value}
        ws_key = keys.get("workspaceid") or keys.get("workspace_id")
        for key, entry in value.items():
            lower = key.lower()
            kinds = ID_FIELDS.get(lower)
            if lower == "id" and parent.lower() in {
                    "lakehouse", "defaultlakehouse", "lakehouses", "known_lakehouses"}:
                kinds = ("Lakehouse",)
            if kinds is None or not guid(entry):
                continue
            own_ws = keys.get("default_lakehouse_workspace_id") if lower == "default_lakehouse" else ws_key
            workspace = guid(value.get(own_ws)) if own_ws else None
            if lower in {"itemid", "artifactid"}:
                hint = value.get("sourceType", value.get("type"))
                kinds = TYPE_HINTS.get(hint, kinds)
            name_key = keys.get("default_lakehouse_name") if lower == "default_lakehouse" else None
            if lower == "id" and kinds == ("Lakehouse",):
                name_key = keys.get("name")
            blocked = ""
            if own_ws and value.get(own_ws) and not workspace:
                blocked = "Workspace is dynamic/not a GUID; update its parameter/configuration manually."
            if "clusteruri" in keys or "databasename" in keys or "connectionstring" in keys:
                blocked = "Source also carries endpoint/database metadata; an ID-only repair is unsafe."
            references.append(Reference(
                workspace, str(UUID(entry)), kinds, Location(path + (key,)),
                Location(path + (own_ws,)) if workspace else None,
                Location(path + (name_key,)) if name_key else None, blocked))
        for key, entry in value.items():
            references.extend(collect_references(entry, path + (key,), key))
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            references.extend(collect_references(entry, path + (index,), parent))
    elif isinstance(value, str):
        references.extend(text_references(value, path))
    return references


def read_documents(live: dict) -> tuple[list[Document], list[str]]:
    documents = []
    warnings = []
    paths = set()
    for part in live["definition"]["parts"]:
        path = part["path"]
        if path in paths:
            raise ValueError(f"Duplicate definition part: {path}")
        paths.add(path)
        if path == ".platform":
            continue
        if part["payloadType"] != "InlineBase64":
            warnings.append(f"{path}: unsupported payloadType {part['payloadType']}")
            continue
        if Path(path).suffix.lower() not in TEXT_SUFFIXES:
            warnings.append(f"{path}: unrecognized/binary format; not inspected")
            continue
        try:
            raw = base64.b64decode(part["payload"], validate=True)
            text = raw.decode("utf-8-sig")
        except (UnicodeDecodeError, binascii.Error) as error:
            warnings.append(f"{path}: could not decode as UTF-8/base64 ({error})")
            continue
        is_json = Path(path).suffix.lower() in {".json", ".ipynb", ".pbir", ".pbism"}
        value = json.loads(text) if is_json else text
        references = collect_references(value)
        if re.search(r'["\']?(?:connectionId|gatewayId)["\']?\s*[:=]\s*["\']?' + GUID,
                     text, re.I):
            warnings.append(f"{path}: cloud/gateway connection IDs need separate connection configuration")
        documents.append(Document(part, value, is_json, raw.startswith(b"\xef\xbb\xbf"),
                                  references))
    return documents, warnings


def get_at(value, path: tuple):
    for key in path:
        value = value[key]
    return value


def set_at(value, path: tuple, replacement):
    if not path:
        return replacement
    parent = get_at(value, path[:-1])
    parent[path[-1]] = replacement
    return value


def rewrite_document(document: Document, mappings: dict, workspace: str) -> dict:
    edits: dict[tuple, dict[tuple[int, int] | None, str]] = {}

    def add(location: Location, replacement: str):
        slots = edits.setdefault(location.path, {})
        if location.span in slots and slots[location.span] != replacement:
            raise ValueError(f"Conflicting replacements in {document.part['path']}")
        slots[location.span] = replacement

    for reference in document.references:
        target = mappings.get(reference.key)
        if target is None:
            continue
        if reference.blocked_reason:
            raise ValueError(reference.blocked_reason)
        for sibling in document.references:
            if (reference.workspace_location is not None
                    and reference.workspace_location == sibling.workspace_location
                    and sibling.key not in mappings and sibling.workspace != workspace):
                raise ValueError("A shared workspace field also belongs to an unselected reference. "
                                 "Map all references sharing that field or leave the item unchanged.")
        add(reference.item_location, target["id"])
        if reference.workspace_location:
            add(reference.workspace_location, workspace)
        if reference.name_location:
            add(reference.name_location, target["displayName"])
    part = copy.deepcopy(document.part)
    if not edits:
        return part
    value = copy.deepcopy(document.value)
    for path, spans in edits.items():
        if None in spans:
            if len(spans) != 1:
                raise ValueError("Conflicting whole-value and substring edits.")
            updated = spans[None]
        else:
            updated = get_at(value, path)
            previous_start = len(updated) + 1
            for span, replacement in sorted(spans.items(), reverse=True):
                if span is None:
                    raise ValueError("Unexpected whole-value edit.")
                start, end = span
                if end > previous_start:
                    raise ValueError("Overlapping reference replacements.")
                updated = updated[:start] + replacement + updated[end:]
                previous_start = start
        value = set_at(value, path, updated)
    if document.is_json and document.part["path"].endswith(".pbir"):
        connection_path = ("datasetReference", "byConnection", "connectionString")
        for reference in document.references:
            target = mappings.get(reference.key)
            if target and reference.item_location.path == connection_path:
                # Do not retain a source workspace/catalog alongside the new model ID.
                value = set_at(value, connection_path[:-1],
                               {"connectionString": f"semanticmodelid={target['id']}"})
    text = json.dumps(value, ensure_ascii=True, indent=2) if document.is_json else value
    encoded = text.encode("utf-8")
    if document.bom:
        encoded = b"\xef\xbb\xbf" + encoded
    part["payload"] = base64.b64encode(encoded).decode("ascii")
    return part


def build_body(snapshot: Snapshot, mappings: dict, workspace: str) -> dict:
    documents = {document.part["path"]: document for document in snapshot.documents}
    definition = copy.deepcopy(snapshot.live["definition"])
    parts = []
    for part in definition["parts"]:
        if part["path"] == ".platform":
            continue
        for (_, old_id), target in mappings.items():
            if target["id"] != old_id and old_id in part["path"].lower():
                raise ValueError(f"{part['path']}: reference ID is also in a part path. "
                                 "Requires workload-specific path migration; not rewritten.")
        document = documents.get(part["path"])
        parts.append(rewrite_document(document, mappings, workspace) if document else part)
    definition["parts"] = parts
    return {"definition": definition}


def skill_for(item: dict) -> str:
    return {"SemanticModel": "semantic-model-authoring", "Ontology": "fabriciq-ontology-cli",
            "Report": "powerbi-report-cli", "Notebook": "spark-cli"}.get(
                item["type"], "git-integration-operations-cli")


def definition_path(workspace: str, item: dict) -> str:
    return f"workspaces/{workspace}/items/{item['id']}"


def fetch_definition(api: FabricClient, workspace: str, item: dict) -> dict:
    query = {"SemanticModel": "?format=TMDL", "Report": "?format=PBIR",
             "Notebook": "?format=ipynb"}.get(item["type"], "")
    return api.call("POST", definition_path(workspace, item) + "/getDefinition" + query,
                    {}, result=True, skill=skill_for(item))


def scan_workspace(api: FabricClient, workspace: str, items: list[dict]):
    snapshots = []
    problems = []
    for index, item in enumerate(items, 1):
        print(f"[{index}/{len(items)}] Inspecting {item['displayName']} [{item['type']}]", flush=True)
        try:
            live = fetch_definition(api, workspace, item)
            documents, warnings = read_documents(live)
        except (ApiError, ValueError, RuntimeError, TimeoutError) as error:
            message = f"{item['displayName']} [{item['type']}]: NOT INSPECTED: {error}"
            print(message)
            problems.append(message)
            continue
        snapshots.append(Snapshot(item, live, documents))
        for warning in warnings:
            message = f"{item['displayName']}: {warning}"
            print("  WARNING:", message)
            problems.append(message)
        print(f"  {sum(len(d.references) for d in documents)} recognized reference(s).")
    return snapshots, problems


def group_references(snapshots: list[Snapshot]) -> dict:
    groups = {}
    for snapshot in snapshots:
        for document in snapshot.documents:
            for reference in document.references:
                groups.setdefault(reference.key, []).append((snapshot, document, reference))
    return groups


def reference_status(api: FabricClient, workspace: str, items: list[dict], key: tuple) -> str:
    source_ws, item_id = key
    if source_ws in (None, workspace) and any(item["id"].lower() == item_id for item in items):
        return "valid"
    try:
        api.call("GET", f"workspaces/{source_ws or workspace}/items/{item_id}")
        return "valid"
    except ApiError as error:
        if error.status == 404:
            return "missing or hidden (404; not proof that the resource was deleted)"
        if error.status in (401, 403):
            return f"unverified access (HTTP {error.status}; may still be a valid reference)"
        raise


def choose_mappings(api: FabricClient, workspace: str, items: list[dict], groups: dict,
                    ask=input) -> tuple[dict, list[str]]:
    mappings = {}
    unresolved = []
    for key, uses in groups.items():
        status = reference_status(api, workspace, items, key)
        if status == "valid":
            print(f"Keeping valid reference: {key[0] or workspace}/{key[1]}")
            continue
        print(f"\nReference: workspace={key[0] or '(implicit destination)'} item={key[1]}")
        print("Status:", status)
        for snapshot, document, reference in uses:
            print(f"  Used by {snapshot.item['displayName']} / {document.part['path']} "
                  f"at {reference.item_location.path}")
        blocked = {reference.blocked_reason for _, _, reference in uses if reference.blocked_reason}
        if blocked:
            message = f"{key}: {'; '.join(sorted(blocked))}"
            print("MANUAL REPAIR:", message)
            unresolved.append(message)
            continue
        constraints = [set(reference.kinds) for _, _, reference in uses if reference.kinds]
        kinds = set.intersection(*constraints) if constraints else set()
        if constraints and not kinds:
            message = f"{key}: conflicting resource-type hints; repair manually."
            print(message)
            unresolved.append(message)
            continue
        candidates = sorted(
            [item for item in items if not kinds or item["type"] in kinds],
            key=lambda item: (item["type"], item["displayName"].casefold(), item["id"]))
        for index, item in enumerate(candidates, 1):
            print(f"  {index}. {item['displayName']} [{item['type']}] {item['id']}")
        if not candidates:
            message = f"{key}: no compatible destination resources."
            print(message)
            unresolved.append(message)
            continue
        while True:
            choice = ask("Replacement number (Enter = skip, q = cancel): ").strip()
            if choice.lower() == "q":
                raise KeyboardInterrupt
            if not choice:
                unresolved.append(f"{key}: skipped by user ({status})")
                break
            if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
                print("Enter a number from the displayed list, Enter to skip, or q.")
                continue
            target = candidates[int(choice) - 1]
            confirmation = ask(
                f"Replace this missing/unverified reference with {target['displayName']} "
                f"[{target['type']}]? Type yes: ")
            if confirmation.strip().lower() == "yes":
                mappings[key] = target
                break
            print("Not confirmed; choose again or skip.")
    return mappings, unresolved


def comparable_parts(live: dict) -> dict:
    result = {}
    for part in live["definition"]["parts"]:
        if part["path"] == ".platform":
            continue
        if part["payloadType"] != "InlineBase64":
            result[part["path"]] = (part["payloadType"], part["payload"])
            continue
        raw = base64.b64decode(part["payload"], validate=True)
        if Path(part["path"]).suffix.lower() in {".json", ".ipynb", ".pbir", ".pbism"}:
            result[part["path"]] = json.loads(raw)
        else:
            result[part["path"]] = raw
    return result


def apply_plans(api: FabricClient, workspace: str, plans: list[tuple], backup_root: Path) -> list[str]:
    backup_dir = backup_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir.mkdir(parents=True, exist_ok=False)
    journal = {"workspaceId": workspace, "items": []}
    for snapshot, body in plans:
        item_id = str(UUID(snapshot.item["id"]))
        (backup_dir / f"{item_id}.before.json").write_text(
            json.dumps(snapshot.live, indent=2), encoding="utf-8")
        (backup_dir / f"{item_id}.planned.json").write_text(
            json.dumps(body, indent=2), encoding="utf-8")
        journal["items"].append({"id": item_id, "displayName": snapshot.item["displayName"],
                                 "status": "not_attempted"})
    journal_path = backup_dir / "results.json"

    def save_journal():
        journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")

    save_journal()
    print(f"\nBackups and results: {backup_dir}", flush=True)
    failures = []
    for (snapshot, body), entry in zip(plans, journal["items"]):
        try:
            current = fetch_definition(api, workspace, snapshot.item)
            if comparable_parts(current) != comparable_parts(snapshot.live):
                raise RuntimeError("Definition changed since scan. Refusing to overwrite; scan again.")
            entry["status"] = "submitting"
            save_journal()
            api.call("POST", definition_path(workspace, snapshot.item) + "/updateDefinition",
                     body, skill=skill_for(snapshot.item))
            entry["status"] = "submitted_readback_pending"
            save_journal()
            saved = fetch_definition(api, workspace, snapshot.item)
            if comparable_parts(saved) != comparable_parts(body):
                raise RuntimeError("Readback differs from the planned definition. Inspect backups/results.")
            entry["status"] = "verified"
            print(f"VERIFIED: {snapshot.item['displayName']}", flush=True)
        except (ApiError, RuntimeError, ValueError, TimeoutError, urllib.error.URLError) as error:
            entry["status"] = "failed_or_unverified"
            entry["error"] = str(error)
            message = f"{snapshot.item['displayName']}: {error}"
            if snapshot.item["type"] == "SemanticModel":
                message += ("\nREST-only: Fabric may reject an old Direct Lake source during import. "
                            "No XMLA fallback or model recreation is attempted.")
            print("FAILED:", message, flush=True)
            failures.append(message)
        save_journal()
    return failures


def run(api: FabricClient, workspace: str, backup_root: Path, scan_only=False, ask=input) -> int:
    ws = api.call("GET", f"workspaces/{workspace}")
    items = api.listing(f"workspaces/{workspace}/items")
    print(f"\nWorkspace: {ws['displayName']} ({workspace}); {len(items)} items.")
    print("Only recognized item references are scanned; this is not a full dependency/credential audit.")
    snapshots, problems = scan_workspace(api, workspace, items)
    groups = group_references(snapshots)
    if scan_only:
        for key, uses in groups.items():
            status = reference_status(api, workspace, items, key)
            print(f"{key[0] or workspace}/{key[1]}: {status}; {len(uses)} use(s)")
            if status != "valid":
                problems.append(f"{key}: {status}")
        print(f"Scan complete: {len(problems)} unresolved/inspection issue(s). No writes.")
        return 2 if problems else 0
    mappings, unresolved = choose_mappings(api, workspace, items, groups, ask)
    problems.extend(unresolved)
    plans = []
    print("\nCHANGE PREVIEW")
    for snapshot in snapshots:
        try:
            body = build_body(snapshot, mappings, workspace)
        except ValueError as error:
            message = f"{snapshot.item['displayName']}: cannot prepare safely: {error}"
            print("MANUAL REPAIR:", message)
            problems.append(message)
            continue
        if body["definition"]["parts"] == [
                p for p in snapshot.live["definition"]["parts"] if p["path"] != ".platform"]:
            continue
        plans.append((snapshot, body))
        old = {p["path"]: p["payload"] for p in snapshot.live["definition"]["parts"]}
        for part in body["definition"]["parts"]:
            if part["payload"] != old[part["path"]]:
                print(f"  MODIFY {snapshot.item['displayName']} / {part['path']}")
    for key, target in mappings.items():
        print(f"  {key[0] or '(implicit)'}/{key[1]} -> {workspace}/{target['id']} "
              f"({target['displayName']})")
    print(f"{len(plans)} item(s) to update; {len(problems)} unresolved/inspection issue(s).")
    if not plans:
        print("No selected changes. Nothing written.")
        return 2 if problems else 0
    print("Updates preserve full definitions except Git-only .platform metadata.")
    print("Backups may contain sensitive definition content; keep them private.")
    print("No data, tables, credentials, permissions, refreshes or Git commits will be changed.")
    if ask("Apply exactly this plan? Type yes (anything else cancels): ").strip().lower() != "yes":
        print("Cancelled. Nothing written.")
        return 0
    problems.extend(apply_plans(api, workspace, plans, backup_root))
    print(f"\nFinished with {len(problems)} unresolved/inspection/update issue(s).")
    print("Validate data access/refresh in Fabric, then commit repaired definitions through workspace Git.")
    return 2 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=lambda value: str(UUID(value)),
                        help="Omit to be prompted for the destination workspace ID.")
    parser.add_argument("--tenant", type=lambda value: str(UUID(value)),
                        help="Optional tenant ID; otherwise use the active Azure CLI account.")
    parser.add_argument("--scan-only", action="store_true", help="Inspect without replacement prompts/writes.")
    parser.add_argument("--backup-dir", type=Path, default=Path.home() / "Fabric-rebind-backups")
    args = parser.parse_args()
    workspace = args.workspace
    while not workspace:
        workspace = guid(input("Destination Fabric workspace ID: ").strip())
        if not workspace:
            print("Enter a valid workspace GUID (not a name or URL).")
    return run(FabricClient(args.tenant), workspace, args.backup_dir, args.scan_only)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Any earlier verified updates remain; check the backup results journal.")
        sys.exit(130)
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as error:
        print(f"ERROR: {error}\nNo automatic retry/rollback is performed.", file=sys.stderr)
        sys.exit(1)
