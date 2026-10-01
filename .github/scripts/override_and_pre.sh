#!/usr/bin/env bash
# override_and_pre.sh <owner/repo> <pinned-ref> <other-ref> [input=value ...]
#
# Run as the FIRST step of a job that also has `uses: <owner/repo>@<pinned-ref>`. By then the runner
# has downloaded the pinned version and already run its pre step. This script:
#   1. records the runner's copy (the pinned version),
#   2. replaces it with <other-ref> downloaded from GitHub's tarball endpoint,
#   3. prepends a PROBE-MARKER line to each of the new version's pre/main/post scripts, so the job log
#      shows which files every later stage actually executed,
#   4. runs the new version's pre by hand, the way the runner would: the runner's bundled node for
#      runs.using, INPUT_* from the given inputs and the action.yml defaults, and a fresh GITHUB_STATE.
# Inputs not given and whose default is an expression (${{ ... }}) are left unset and reported - a
# step cannot evaluate them the way the runner does.
set -uo pipefail

repo="$1" pinned="$2" other="$3"; shift 3
actions_root="$(dirname "$RUNNER_WORKSPACE")/_actions"
dir="$actions_root/$repo/$pinned"
work="$RUNNER_TEMP/pre-rerun"
mkdir -p "$work"

say() { printf '%s\n' "$*"; }
stage_files() {  # prints "stage path" for each of pre/main/post declared in $1/action.yml
  python3 - "$1" <<'EOF'
import sys, os, yaml
d = sys.argv[1]
for name in ("action.yml", "action.yaml"):
    p = os.path.join(d, name)
    if os.path.exists(p):
        runs = (yaml.safe_load(open(p)) or {}).get("runs", {})
        print("using", runs.get("using", ""))
        for stage in ("pre", "main", "post"):
            if runs.get(stage):
                print(stage, runs[stage])
        break
EOF
}
describe() {  # $1 label, $2 dir
  say "::group::$1 ($2)"
  if [ -f "$2/package.json" ]; then say "  package.json version: $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version"))' "$2/package.json")"; fi
  while read -r stage path; do
    [ "$stage" = using ] && { say "  runs.using: $path"; continue; }
    say "  $stage: $path sha256=$(sha256sum "$2/$path" | cut -c1-16)"
  done < <(stage_files "$2")
  say "::endgroup::"
}

say "PROBE-STEP override_and_pre: $repo pinned=$pinned other=$other"
[ -d "$dir" ] || { say "runner copy not found at $dir"; ls -la "$actions_root/$repo" 2>&1; exit 1; }
describe "runner copy before override = $repo@$pinned" "$dir"

# 2. replace with the other version
curl -sSfL -o "$work/other.tar.gz" -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/$repo/tarball/$other" || { say "download failed"; exit 1; }
mkdir -p "$work/other" && tar -xzf "$work/other.tar.gz" -C "$work/other"
top=$(ls "$work/other")
say "downloaded $repo@$other as $top"
mv "$dir" "$work/pinned-original"
mv "$work/other/$top" "$dir"
describe "runner copy after override = $repo@$other" "$dir"

# 3. markers in the new version's stage scripts
while read -r stage path; do
  [ "$stage" = using ] && continue
  f="$dir/$path"
  marker="console.log('PROBE-MARKER $repo@$other $stage-script ran: ' + (process.argv[1] || ''));"
  if head -c2 "$f" | grep -q '#!'; then sed -i "1a $marker" "$f"; else sed -i "1i $marker" "$f"; fi
done < <(stage_files "$dir")

# 4. run the new version's pre by hand
using=$(stage_files "$dir" | awk '$1=="using"{print $2}')
pre=$(stage_files "$dir" | awk '$1=="pre"{print $2}')
[ -n "$pre" ] || { say "new version declares no pre - nothing to run"; exit 0; }
pid=$$; worker=""
while [ "$pid" -gt 1 ]; do
  case "$(readlink "/proc/$pid/exe" 2>/dev/null)" in *Runner.Worker*) worker=$pid; break;; esac
  pid=$(sed -E 's/^[0-9]+ \(.*\) [A-Z] ([0-9]+) .*/\1/' "/proc/$pid/stat")
done
node=node
if [ -n "$worker" ]; then
  root=$(dirname "$(dirname "$(readlink "/proc/$worker/exe")")")
  [ -x "$root/externals/$using/bin/node" ] && node="$root/externals/$using/bin/node"
fi
say "node for $using: $node ($("$node" --version))"

declare -A given=()
for kv in "$@"; do given["${kv%%=*}"]="${kv#*=}"; done
envs=()
while IFS=$'\t' read -r name default; do
  key="INPUT_$(printf '%s' "$name" | tr '[:lower:] ' '[:upper:]_')"
  if [ -n "${given[$name]+x}" ]; then envs+=("$key=${given[$name]}")
  elif [[ "$default" == *'${{'* ]]; then
    envname="PRE_INPUT_$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')"
    if [ -n "${!envname:-}" ]; then envs+=("$key=${!envname}"); say "  input $name: from \$$envname (workflow-evaluated)"
    else say "  input $name: default is an expression ($default) - left unset"; fi
  else envs+=("$key=$default"); fi
done < <(python3 - "$dir" <<'EOF'
import sys, os, yaml
d = sys.argv[1]
for name in ("action.yml", "action.yaml"):
    p = os.path.join(d, name)
    if os.path.exists(p):
        for k, v in ((yaml.safe_load(open(p)) or {}).get("inputs") or {}).items():
            dv = (v or {}).get("default", "")
            print(f"{k}\t{'' if dv is None else str(dv).replace(chr(10), ' ')}")
        break
EOF
)

state="$work/state" out="$work/output" envf="$work/env" pathf="$work/path"
: > "$state"; : > "$out"; : > "$envf"; : > "$pathf"
say "::group::manual pre of $repo@$other ($pre)"
env "${envs[@]}" GITHUB_ACTION_PATH="$dir" GITHUB_ACTION_REPOSITORY="$repo" GITHUB_ACTION_REF="$pinned" \
  GITHUB_STATE="$state" GITHUB_OUTPUT="$out" GITHUB_ENV="$envf" GITHUB_PATH="$pathf" \
  "$node" "$dir/$pre" < /dev/null
rc=$?
say "::endgroup::"
say "PROBE-RESULT manual pre of $repo@$other exit=$rc"
say "  state written by the manual pre (the runner will NOT hand this to main/post):"; sed 's/^/    /' "$state"
say "  env written: $(wc -l < "$envf") line(s); path written: $(wc -l < "$pathf") line(s)"
exit 0
