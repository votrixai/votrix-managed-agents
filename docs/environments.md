---
title: Environments
description: Build the sandbox image and machine used by Agent Sessions.
---

An Environment describes the sandbox image used when a Session starts. Put
packages that every Session needs in the Environment so VMA installs them once
while building the image instead of during every Agent turn.

## Create the base Environment

When no packages are declared, VMA uses its base image immediately:

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/environments \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{"name":"General workspace"}'
```

The response has `build_state: "ready"`, so its `id` can be used to create a
Session immediately. With `config` omitted, the stored recipe reports its
defaults of 2 CPU and 1,024 MiB memory.

## Build a custom Environment

Declare packages and machine settings in `config`:

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/environments \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "name": "Data analysis workspace",
    "description": "Python packages used by spreadsheet jobs.",
    "config": {
      "packages": {
        "apt": ["libmagic1"],
        "pip": ["pandas==2.2.3", "openpyxl==3.1.5"]
      },
      "cpu": 2,
      "memory_mb": 2048
    }
  }'
```

A non-empty package recipe starts an asynchronous image build and returns
`build_state: "building"`. Save the Environment `id` and poll it until the
state changes.

## Configuration fields

| Field | Default | Allowed values |
| --- | ---: | --- |
| `config.packages` | Empty | Lists for `apt`, `cargo`, `gem`, `go`, `npm`, and `pip`. |
| `config.cpu` | `2` | Integer from 1 through 8. |
| `config.memory_mb` | `1024` | Integer from 512 through 8,192 MiB. |

A custom image build starts only when at least one package is declared. With an
empty package recipe, VMA selects the base image immediately; CPU and memory are
image settings rather than per-Session overrides.

Package entries use the package manager's own syntax. Unpinned entries install
the version selected by that manager.

| Manager | Example entry |
| --- | --- |
| `apt` | `libmagic1` |
| `cargo` | `ripgrep@14.1.0` |
| `gem` | `rails:7.1.0` |
| `go` | `golang.org/x/tools/gopls@v0.16.2` |
| `npm` | `express@4.18.0` |
| `pip` | `pandas==2.2.3` |

Managers run in the order shown above. A later manager can therefore use
system packages or toolchains installed by an earlier one.

<Callout title="Send the complete config when updating" type="warn">

`config` is replaced as one value; it is not a nested patch. If an update sends
`config`, include the complete package lists, CPU, and memory that the updated
Environment should keep. An omitted package list becomes empty and omitted
machine fields return to their defaults.

</Callout>

## Wait for the build

Retrieve the Environment to refresh and read its current build state:

```bash
curl --request GET \
  --url https://api.vma.votrixai.com/v1/environments/YOUR_ENVIRONMENT_ID \
  --header 'x-api-key: YOUR_API_KEY'
```

| `build_state` | Meaning | What to do |
| --- | --- | --- |
| `building` | The image is still being prepared. | Poll the Environment again. |
| `ready` | New Sessions can use the Environment. | Create the Session. |
| `failed` | The image could not be built. | Read `build_error`, correct the complete recipe, and update it. |

VMA refreshes a pending build when the Environment is retrieved or listed.
Creating a Session against a `building` or `failed` Environment returns `409`.

## Update an Environment

Send only `name` or `description` to change its labels without rebuilding:

```json
{
  "description": "Shared environment for spreadsheet and CSV analysis."
}
```

Changing packages, CPU, or memory changes the image recipe. Send the complete
replacement config and wait for the new build to become ready:

```json
{
  "config": {
    "packages": {
      "apt": ["libmagic1"],
      "pip": ["pandas==2.2.3", "openpyxl==3.1.5", "pyarrow==17.0.0"]
    },
    "cpu": 4,
    "memory_mb": 4096
  }
}
```

Existing Sessions keep the sandbox image with which they started. Sessions
created after the update use the replacement image once it is ready.

## Archive or delete

Archive an Environment to keep its history while preventing new Sessions from
using it. Existing Sessions continue. Deletion is refused while any Session
still references the Environment; archive it instead when history must remain.

## Environment API Reference

| Operation | Purpose |
| --- | --- |
| [Create Environment](/docs/api/environments/create_environment_v1_environments_post) | Register a base or custom Environment. |
| [List Environments](/docs/api/environments/list_environments_v1_environments_get) | List Environments and refresh pending builds. |
| [Retrieve Environment](/docs/api/environments/retrieve_environment_v1_environments__environment_id__get) | Refresh and read one build state. |
| [Update Environment](/docs/api/environments/update_environment_v1_environments__environment_id__post) | Change labels or replace the image recipe. |
| [Archive Environment](/docs/api/environments/archive_environment_v1_environments__environment_id__archive_post) | Prevent new Sessions from selecting it. |
| [Delete Environment](/docs/api/environments/delete_environment_v1_environments__environment_id__delete) | Delete an Environment that no Session references. |
