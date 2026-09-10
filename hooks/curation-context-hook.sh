#!/usr/bin/env bash
# Curation context probe - the ACTIONS_RUNNER_HOOK_JOB_STARTED half.
#
# What a pre-job hook can see cannot be measured from a workflow file: a hook is
# not a step, and no YAML key reaches it. This script measures it from inside
# the hook instead. It only reads and prints - it never fails a job.
#
# Install on a self-hosted runner:
#   cp curation-context-hook.sh /etc/runner/curation-context-hook.sh
#   chmod +x /etc/runner/curation-context-hook.sh
#   # in the runner's .env (or service environment), then restart the runner:
#   ACTIONS_RUNNER_HOOK_JOB_STARTED=/etc/runner/curation-context-hook.sh
#
# Then run any workflow on that runner and read the "Set up job" log group.
#
# The questions, and why each matters for `jf curate-gh-actions`:
#
#   1. Are GITHUB_WORKFLOW_REF and GITHUB_JOB set here? The CLI identifies the
#      workflow and job from them. If a hook does not get them, curation cannot
#      scope to the running job in this delivery model at all.
#
#   2. Is any token present? Without one the workflow YAML cannot be fetched
#      from the API either - and a hook gets no `${{ }}` interpolation pass, so
#      it cannot be handed `github.token` the way a step can.
#
#   3. Is GITHUB_WORKSPACE empty, and is the workflow YAML on disk? The hook
#      fires before every step, so checkout definitionally has not run. If the
#      workspace still holds a previous job's checkout, reading "the workflow
#      file" from disk reads another job's - possibly another repo's - YAML.
#
#   4. Is _work/_actions already populated? The whole delivery model rests on
#      this. Expected yes: the runner awaits PrepareActionsAsync in
#      JobExtension.InitializeJob before adding the hook to preJobSteps.
#
#   5. Is GITHUB_STEP_SUMMARY set? Decides whether the curation report can
#      render as a job summary here or degrades to the collapsed setup log.

set -uo pipefail   # deliberately no -e: a probe must not fail the job

say() { printf '%s\n' "$*"; }

say "==================== curation context: PRE-JOB HOOK ===================="
say "hook script : ${BASH_SOURCE[0]}"
say "shell       : ${BASH:-unknown} (${BASH_VERSION:-?})"
say "uid/gid     : $(id -u)/$(id -g)  user=$(id -un 2>/dev/null || echo '?')"
say "cwd         : $(pwd)"
say "date        : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

say ""
say "--- Q1: workflow/job identity -----------------------------------------"
for v in GITHUB_WORKFLOW GITHUB_WORKFLOW_REF GITHUB_WORKFLOW_SHA GITHUB_JOB \
         GITHUB_REPOSITORY GITHUB_RUN_ID GITHUB_SHA GITHUB_REF GITHUB_ACTOR \
         GITHUB_EVENT_NAME GITHUB_SERVER_URL GITHUB_API_URL RUNNER_NAME; do
  printf '  %-22s = %s\n' "$v" "${!v:-<UNSET>}"
done

WF_PATH=""
if [ -n "${GITHUB_WORKFLOW_REF:-}" ]; then
  # "<owner>/<repo>/<path>@<ref>" - strip the trailing @ref, then owner/repo.
  _head="${GITHUB_WORKFLOW_REF%@*}"
  WF_PATH="${_head#*/}"; WF_PATH="${WF_PATH#*/}"
  say "  derived workflow path  = ${WF_PATH}"
else
  say "  derived workflow path  = <cannot derive: GITHUB_WORKFLOW_REF unset>"
  say "  => curation cannot identify the running workflow in this model."
fi

say ""
say "--- Q2: credentials available to a hook -------------------------------"
for v in GITHUB_TOKEN GH_TOKEN ACTIONS_RUNTIME_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN \
         JF_URL JF_ACCESS_TOKEN JFC_REMOTE_REPO; do
  # Never print a secret; presence and length are the whole question.
  _val="${!v:-}"
  if [ -n "$_val" ]; then
    printf '  %-32s = <set, %s chars>\n' "$v" "${#_val}"
  else
    printf '  %-32s = <UNSET>\n' "$v"
  fi
done
say "  (a hook gets no \${{ }} interpolation pass, so github.token cannot be"
say "   passed to it the way a step receives it)"

say ""
say "--- Q3: workspace state before any step -------------------------------"
say "  GITHUB_WORKSPACE = ${GITHUB_WORKSPACE:-<UNSET>}"
say "  RUNNER_WORKSPACE = ${RUNNER_WORKSPACE:-<UNSET>}"
if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -d "$GITHUB_WORKSPACE" ]; then
  _n=$(find "$GITHUB_WORKSPACE" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')
  say "  workspace entries = ${_n}"
  find "$GITHUB_WORKSPACE" -mindepth 1 -maxdepth 1 2>/dev/null \
    | sed "s|^|    |" | head -20
  if [ -d "$GITHUB_WORKSPACE/.github/workflows" ]; then
    say "  .github/workflows PRESENT -> DIRTY workspace (previous job's checkout)."
    say "  => reading 'the workflow file' from disk here reads STALE content."
    ls -1 "$GITHUB_WORKSPACE/.github/workflows" 2>/dev/null | sed 's|^|    |'
  else
    say "  .github/workflows absent -> the workflow YAML is NOT on disk yet."
  fi
  if [ -n "$WF_PATH" ] && [ -f "$GITHUB_WORKSPACE/$WF_PATH" ]; then
    say "  the running workflow file IS on disk at $GITHUB_WORKSPACE/$WF_PATH"
  else
    say "  the running workflow file is NOT on disk"
  fi
else
  say "  workspace does not exist yet"
fi

say ""
say "--- Q4: _actions cache before any step --------------------------------"
ACTIONS_DIR="${RUNNER_WORKSPACE:-}/../_actions"
if [ -n "${RUNNER_WORKSPACE:-}" ] && [ -d "$ACTIONS_DIR" ]; then
  ACTIONS_DIR="$(cd "$ACTIONS_DIR" && pwd)"
  say "  _actions = $ACTIONS_DIR"
  # owner/repo/ref, exactly the three levels DiscoverActionCache walks.
  _count=0
  while IFS= read -r d; do
    printf '    %s\n' "${d#"$ACTIONS_DIR"/}"
    _count=$((_count + 1))
  done < <(find "$ACTIONS_DIR" -mindepth 3 -maxdepth 3 -type d 2>/dev/null | sort)
  say "  owner/repo/ref entries = ${_count}"
  if [ "$_count" -gt 0 ]; then
    say "  => cache IS populated before any step ran (delivery model holds)."
  else
    say "  => cache is EMPTY at hook time - the model's premise does not hold here."
  fi
  say "  non-conforming entries at each level (skipped by discovery):"
  find "$ACTIONS_DIR" -mindepth 1 -maxdepth 3 ! -type d 2>/dev/null \
    | sed "s|^|    |" | head -10
else
  say "  _actions not found at $ACTIONS_DIR"
  say "  => nothing to curate; the generated hook exits 0 on this condition."
fi

say ""
say "--- Q5: can a hook write a job summary? --------------------------------"
say "  GITHUB_STEP_SUMMARY = ${GITHUB_STEP_SUMMARY:-<UNSET>}"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  if printf '### curation context hook probe\n\nreached from the pre-job hook.\n' \
      >> "$GITHUB_STEP_SUMMARY" 2>/dev/null; then
    say "  wrote to it successfully -> the curation report can render as a summary."
  else
    say "  set but NOT writable -> report degrades to the setup log."
  fi
else
  say "  unset -> the curation report degrades to the collapsed 'Set up job' log."
fi

say ""
say "--- full environment (sorted, values of known-secret vars elided) ------"
env | sort | sed -E 's/^(GITHUB_TOKEN|GH_TOKEN|ACTIONS_RUNTIME_TOKEN|ACTIONS_ID_TOKEN_REQUEST_TOKEN|JF_ACCESS_TOKEN|.*_SECRET|.*_PASSWORD)=.*/\1=<elided>/' \
  | sed 's|^|  |'

say "==================== end curation context probe ========================"
exit 0
