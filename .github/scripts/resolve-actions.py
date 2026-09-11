import json,os,sys,urllib.request,base64,collections
try: import yaml
except ImportError: yaml=None

TOK=os.environ.get("GITHUB_TOKEN","")
REPO=os.environ["GITHUB_REPOSITORY"]
SHA=os.environ["GITHUB_SHA"]
WREF=os.environ["GITHUB_WORKFLOW_REF"]       # owner/repo/.github/workflows/x.yml@refs/...
JOB=os.environ["GITHUB_JOB"]
wf_path=WREF.split("@")[0].split("/",2)[2]   # strip owner/repo, keep the path

def fetch(path):
    url=f"https://api.github.com/repos/{REPO}/contents/{path}?ref={SHA}"
    req=urllib.request.Request(url,headers={"Accept":"application/vnd.github+json","User-Agent":"probe"})
    if TOK: req.add_header("Authorization","Bearer "+TOK)
    try:
        with urllib.request.urlopen(req,timeout=20) as r:
            return base64.b64decode(json.load(r)["content"]).decode()
    except Exception as e:
        print(f"  FETCH FAIL {path}: {e}"); return None

def uses_of(doc,job=None):
    """Return every `uses:` string: workflow job steps, or composite action steps."""
    out=[]
    if job is not None:
        j=(doc.get("jobs") or {}).get(job) or {}
        for s in (j.get("steps") or []):
            if isinstance(s,dict) and s.get("uses"): out.append(s["uses"])
        if j.get("uses"): out.append(j["uses"])            # reusable workflow call
    else:
        r=doc.get("runs") or {}
        for s in (r.get("steps") or []):
            if isinstance(s,dict) and s.get("uses"): out.append(s["uses"])
    return out

print(f"workflow={wf_path} job={JOB} sha={SHA[:8]} token={'yes' if TOK else 'NO'}")
src=fetch(wf_path)
if src is None: sys.exit("cannot read workflow via API")
third,local,seen=[],collections.deque(),set()
for u in uses_of(yaml.safe_load(src),job=JOB):
    (local.append((u,"<workflow>")) if u.startswith("./") else third.append((u,"<workflow>")))

while local:
    lpath,parent=local.popleft()
    base=lpath[2:].rstrip("/")
    if base in seen: continue
    seen.add(base)
    doc=None
    for name in ("action.yml","action.yaml"):
        c=fetch(f"{base}/{name}")
        if c is not None: doc=yaml.safe_load(c); break
    if doc is None: print(f"  no action.yml for {lpath}"); continue
    for u in uses_of(doc):
        (local.append((u,lpath)) if u.startswith("./") else third.append((u,lpath)))

print("\nTHIRD-PARTY ACTIONS RESOLVED AT JOB START:")
for u,p in third: print(f"  {u}   (via {p})")
print(f"\ncount={len(third)}  local_composites_walked={len(seen)}")
