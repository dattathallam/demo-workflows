#!/usr/bin/env bash
# Shared reporting body for the env-shadowing probes. Reads only; never fails a job.
set -uo pipefail

# Ground truth, discovered rather than assumed: a self-hosted runner does not use the
# hosted /home/runner/work layout. The primary signal does not depend on this - a shadow is
# detected by the sentinel value itself - so an undiscovered root only costs the comparison.
REAL_ACTIONS=""
for root in /home/runner/work "$HOME/Downloads/actions-runner/_work" "$RUNNER_WORKSPACE/.."; do
  [ -d "$root/_actions" ] && { REAL_ACTIONS="$(cd "$root/_actions" && pwd)"; break; }
done
[ -z "$REAL_ACTIONS" ] && REAL_ACTIONS="<not found>"

echo "::group::[$PROBE_SCOPE] values as the step process sees them"
for v in PROBE_MARKER RUNNER_WORKSPACE GITHUB_REPOSITORY GITHUB_WORKSPACE GITHUB_JOB; do
  printf '  %-20s = %s\n' "$v" "${!v:-<UNSET>}"
done
echo "::endgroup::"

echo "::group::[$PROBE_SCOPE] did the shadow land?"
ws_shadowed=false;   case "${RUNNER_WORKSPACE:-}"  in /tmp/shadow-*) ws_shadowed=true ;; esac
repo_shadowed=false; case "${GITHUB_REPOSITORY:-}" in attacker/*)    repo_shadowed=true ;; esac
echo "  RUNNER_WORKSPACE shadowed  = $ws_shadowed"
echo "  GITHUB_REPOSITORY shadowed = $repo_shadowed"
echo "::endgroup::"

echo "::group::[$PROBE_SCOPE] env-derived cache dir vs ground truth"
# Lexical, to match filepath.Join(RUNNER_WORKSPACE, "..", "_actions") in the Go code:
# it cleans the ".." textually rather than resolving it on disk.
derived="<cannot derive>"
if [ -n "${RUNNER_WORKSPACE:-}" ]; then derived="${RUNNER_WORKSPACE%/*}/_actions"; fi
echo "  derived    = $derived"
echo "  ground     = $REAL_ACTIONS"
echo "  derived exists = $([ -d "$derived" ] && echo true || echo false)"
echo "  ground  exists = $([ -d "$REAL_ACTIONS" ] && echo true || echo false)"
if [ -d "$REAL_ACTIONS" ]; then
  echo "  entries under ground truth:"
  find "$REAL_ACTIONS" -mindepth 3 -maxdepth 3 -type d 2>/dev/null | sed "s|^|    |" | head
fi
echo "  => a missing derived dir is what makes DiscoverActionCache return an"
echo "     empty scan with a nil error, which Run reports as 'nothing to curate'."
echo "::endgroup::"

{
  echo "### [$PROBE_SCOPE] env shadowing"
  echo ""
  echo "| check | value |"
  echo "|---|---|"
  echo "| \`PROBE_MARKER\` | \`${PROBE_MARKER:-<UNSET>}\` |"
  echo "| \`RUNNER_WORKSPACE\` | \`${RUNNER_WORKSPACE:-<UNSET>}\` |"
  echo "| \`GITHUB_REPOSITORY\` | \`${GITHUB_REPOSITORY:-<UNSET>}\` |"
  echo "| RUNNER_WORKSPACE shadowed | \`$ws_shadowed\` |"
  echo "| GITHUB_REPOSITORY shadowed | \`$repo_shadowed\` |"
  echo "| derived cache dir exists | \`$([ -d "$derived" ] && echo true || echo false)\` |"
} >> "$GITHUB_STEP_SUMMARY"

echo "::notice title=[$PROBE_SCOPE] shadowing::ws=$ws_shadowed repo=$repo_shadowed marker=${PROBE_MARKER:-unset}"
