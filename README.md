# Compliance Gate

A simple composite GitHub Action that **enforces** compliant versions of the third-party
actions a workflow uses. It builds on the [`gacpm`](../gh-action-compliance) package manager and
a curation service.

For each third-party action present on the runner it:

1. Scans `_work/_actions` for third-party actions (ignores GitHub-native `actions/*`, `github/*`,
   and this gate's own repo).
2. Resolves every version with `gacpm list owner/repo@ref`.
3. Calls `POST {api-base-url}/filterVersions` with `[[downloadUrl, version], ...]` and drops the
   `blocked_versions` it returns.
4. Picks the 0th (latest) remaining version and calls `POST {api-base-url}/curation` with its
   `downloadUrl`.
5. **Approved (200):** downloads that version and unzips it over the on-disk action in
   `_work/_actions/<owner>/<repo>/<ref>/`, enforcing the compliant version.
   **Rejected (403) / nothing compliant:** the gate fails the job, so later steps never run.

## Inputs

| Input | Default | Description |
|---|---|---|
| `github-token` | `${{ github.token }}` | Token for `gacpm`/`gh` to read tags and download zipballs. |
| `api-base-url` | the demo ngrok URL | Curation server base; `/filterVersions` and `/curation` are derived from it. |

## Prerequisite: `gacpm` on the runner PATH

This action assumes the `gacpm` binary is already installed on the (self-hosted) runner. Build and
install it once:

```bash
cd /path/to/gh-action-compliance
go build -o gacpm ./cmd/gacpm
sudo cp gacpm /usr/local/bin/gacpm
which gacpm && gacpm list actions/checkout@v4 >/dev/null && echo OK
```

`gh`, `jq`, and `curl` must also be on PATH (the preflight step checks all four).

## Usage

```yaml
- uses: actions/checkout@v4
- uses: dattathallam/gh-test-workflows@main
  # with:
  #   api-base-url: https://<your-ngrok-id>.ngrok-free.app
- uses: dattathallam/gh-action-go@v1   # runs only if the gate passed
  with:
    text: "hello"
```

## Caveat: Docker actions

The enforced swap rewrites the action's source in `_actions`. For **composite/JavaScript** actions
this takes effect immediately (their code is read from disk at step time). For **Docker** actions
the runner builds the image at job start, so a later source swap won't rebuild it — the on-disk
files are replaced, but the already-built image is what runs.
