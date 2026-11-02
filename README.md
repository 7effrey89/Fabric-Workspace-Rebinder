# Repair Fabric references after a Git restore

[rebind_fabric_workspace.py](./rebind_fabric_workspace.py) is an interactive,
REST-only tool. It is independent of any particular tenant, deployment
configuration, item names or historical GUIDs. You can also copy the single
script to another repository.

## Run

Requires Python 3.10+ and Azure CLI. No additional Python packages are needed.
Sign in as the destination user with access to the restored workspace:

```powershell
az login --tenant "<DESTINATION-TENANT-ID>" --allow-no-subscriptions
python .\rebind_fabric_workspace.py
```

The **first question asks for the destination workspace ID** (a GUID, not a
workspace name or portal URL).

The tool then:

1. Inventories every item in that workspace and attempts to export its current
   definition. Unsupported types and permission errors are explicitly reported.
2. Finds recognized resource references and checks whether their targets are
   accessible. Valid local and cross-workspace references are left unchanged.
3. Groups repeated references by old workspace/item ID and shows every use.
4. For each missing/unverified reference, lists compatible destination resources
   with their names, types and IDs. Enter a number and type `yes` to select one;
   Enter skips it, and `q` cancels before applying anything.
5. Shows the planned item/part changes and old-to-new mappings. Only an explicit
   final `yes` authorizes writes.
6. Backs up **all selected definitions before the first update**, checks for
   concurrent edits, submits each update once, and reads back the full definition
   to verify the result.

Optional arguments:

```powershell
# Read-only scan: no replacement prompts, backups, or updates
python .\rebind_fabric_workspace.py --scan-only

# Supply the workspace and tenant explicitly
python .\rebind_fabric_workspace.py --workspace "<WORKSPACE-ID>" --tenant "<TENANT-ID>"

# Choose a private backup folder outside your Git repository
python .\rebind_fabric_workspace.py --backup-dir "C:\Private\FabricRebind"
```

Backups default to `Fabric-rebind-backups` under your home directory, in a
timestamped subfolder. It contains each original definition, the planned payload,
and `results.json` recording verified, failed/unverified, and not-attempted items.
Definitions can contain sensitive code/configuration; restrict access to backups
and do not commit them. Access tokens are kept in memory and never saved.

## Recognized mappings

- JSON workspace/item references, including ontology data bindings,
  relationship contextualizations, report resource links, data-agent artifact
  references and pipeline notebook/activity references.
- Notebook default/known Lakehouse metadata, including the default Lakehouse's
  workspace and display name.
- OneLake DFS/Blob and ABFSS URLs containing workspace/item GUIDs, including
  URLs inside notebook source cells and TMDL expressions.
- Fabric/Power BI portal item URLs, GUID-based OneLake catalog detail URLs, and
  Fabric REST item URLs.
- TMDL resource links with sibling `workspaceId`/`itemId` properties.
- Report connection strings containing `semanticmodelid=<GUID>` or
  `datasetid=<GUID>`.

The tool updates references **in context**, not by globally replacing GUIDs.
Item identity, lineage tags, entity/property IDs, table names, source columns,
and `.platform` Git identity are not remapped. All definition parts are retained
in updates except `.platform`, which is not sent to `updateDefinition`.

## Important limits

**This is not a universal Fabric migration or credential-repair tool.**
Fabric has no single API that safely rebinds every workload.

- A 404 can mean missing **or hidden by permissions**. A 401/403 is treated as
  unverified access, never proof of deletion. Rebinding either requires your
  explicit selection and confirmation. Check the displayed identity/context
  yourself when using multiple tenants.
- Resource selection is manual. Matching names do not prove schema compatibility.
  The destination resource must contain the required data/tables/columns.
- Every item is inventoried, but only exportable definitions and recognized
  reference formats can be repaired. Unrecognized/binary parts are preserved and
  reported as not inspected. A clean recognized-reference scan does not prove
  that every dependency in a workload is valid.
- Arbitrary GUID constants in code, dynamically assembled URLs, parameterized
  workspace references, SQL server/catalog strings, cloud/gateway connections,
  credentials, external sources and schema changes require workload-specific
  configuration. Endpoint/database metadata alongside an item ID is blocked
  rather than partially rewritten.
- Definitions whose **part paths** also contain a selected old resource ID need
  workload-specific path migration; these are not blindly renamed.
- Semantic-model definition import can fail while validating the *old* Direct
  Lake source. This has been observed in a restored workspace. Per the REST-only design,
  such failures are reported, not retried through XMLA/TOM or worked around by
  deleting/recreating the model.
- No permissions, data, refresh schedules, Git state or connection credentials
  are changed. No pipeline/notebook executes, model refresh runs, or data agent
  publishes automatically.
- Updates are per item, not a workspace transaction. Earlier verified updates
  remain if a later item fails or the run is interrupted. There is no automatic
  rollback or blind write retry. A crash after submission may leave an unknown
  outcome: inspect the journal and live item before running again.
- The concurrent-edit check reduces risk but cannot eliminate the race between
  reading a definition and submitting its update. Avoid concurrent portal/Git
  changes while running the tool.
- Readback verification is conservative: service normalization beyond JSON
  formatting can be reported as unverified. Inspect the saved plan and live
  definition before deciding whether a further change is needed.
- Public-cloud Fabric endpoints are supported; sovereign clouds are not.

Exit codes: `0` = no outstanding detected issues or user cancelled before apply;
`2` = unresolved/skipped/unsupported/failed/unverified work; `1` = fatal error;
`130` = interrupted or terminal input closed. A verified update means the
definition persisted, **not** that downstream data access has been validated.

## After repair

Validate the affected models/reports/ontology instances in Fabric and configure
destination-user credentials where required. Refresh only after source access
and schema compatibility are established.

Commit repaired definitions from the destination workspace using Fabric Git
integration **before syncing from the repository again**, or the old references
may return. This tool deliberately does not commit, publish, or overwrite your
local Git files.

To restore a definition, use the item's saved `*.before.json` with the appropriate
Fabric `updateDefinition` API after reviewing it and removing `.platform`. Restore
is itself a live write and can fail if the old resource is still inaccessible.

## Development

Run the offline test suite from the repository root:

```powershell
python -m unittest discover -s tests
```

Tests simulate Fabric APIs and terminal input; they do not change live resources.
