# cmp-fixtures

Fixture actions for `probe-content-compare.yml` on branch `probe/content-compare`.

Every action prints a `CMP-RESULT` line naming the `VERSION` it was loaded from, so a job log shows
which commit's content each stage (pre / main / post) actually ran.

| Path | Kind | Stages |
|---|---|---|
| `node-pre` | node20 | pre, main, post (pre saves state, main/post read it) |
| `node-nopre` | node20 | main only at version A; main + post at version B |
| `docker-pre` | docker (Dockerfile) | pre-entrypoint, entrypoint |
| `composite` | composite | a run step, then `node-nopre` nested |

Tags: `cmp-stable` never moves (version A). `cmp-moving` is reset to A and then moved to B by the
probe workflow mid-job, to reproduce a tag replaced after the runner downloaded it.
