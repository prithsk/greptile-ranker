"""greptile-ranker: which Greptile review comments do developers actually act on?

Greptile counts a comment as addressed when a later commit on the PR touches the flagged file. This
uses that same signal as the label, collects Greptile comments from public merged PRs, and trains a
ranker. The question: keeping 90% of the comments developers address, how much of the rest can go?

  python ranker.py collect --max-prs 400     # needs GITHUB_TOKEN in .env (gh auth token works)
  python ranker.py evaluate
"""
import argparse, collections, json, math, random, re, time, datetime as dt
from pathlib import Path
import requests
from common import ROOT, need, os

API = "https://api.github.com"
DATA = ROOT / "data" / "comments.jsonl"
CATS = {"bug": ("bug", "crash", "null", "undefined", "exception", "race", "leak", "off-by-one", "infinite"),
        "security": ("security", "injection", "xss", "secret", "auth", "credential", "sanitize"),
        "perf": ("performance", "slow", "n+1", "memory", "cache", "inefficient"),
        "style": ("naming", "readab", "typo", "style", "unused", "nit", "consistent"),
        "tests": ("test", "coverage", "assert")}
HEDGE = ("might", "could", "consider", "maybe", "possibly", "perhaps", "may want")
SURE = ("will fail", "will crash", "must", "always", "never", "breaks", "incorrect")
EXTS = (".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".rb", ".md", ".json", ".yml", ".yaml")

def _h(): return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
def get(url, **params):
    for attempt in range(5):
        r = requests.get(url, headers=_h(), params=params, timeout=30)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            wait = max(5, int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time()))
            print(f"  rate limited, sleeping {wait}s", flush=True); time.sleep(min(wait, 900)); continue
        r.raise_for_status(); return r.json()
    raise SystemExit("GitHub kept rate limiting")

def collect(max_prs):
    need("GITHUB_TOKEN"); DATA.parent.mkdir(exist_ok=True)
    seen = set()
    if DATA.exists():
        for l in DATA.read_text(encoding="utf-8").splitlines(): seen.add(json.loads(l)["pr"])
    prs, found = [], set(seen)
    end = dt.date.today()
    for _ in range(18):                               # one month at a time; each search caps at 1000 results
        start = end - dt.timedelta(days=30)
        for page in range(1, 11):
            res = get(f"{API}/search/issues", q=f"commenter:greptile-apps[bot] is:pr is:merged is:public merged:{start}..{end}", per_page=100, page=page)
            items = [i for i in res.get("items", []) if i["html_url"] not in found]
            for i in items: found.add(i["html_url"])
            prs += items
            if len(res.get("items", [])) < 100 or len(prs) >= max_prs: break
        if len(prs) >= max_prs: break
        end = start
    with DATA.open("a", encoding="utf-8") as out:
        for n, item in enumerate(prs[:max_prs], 1):
            repo = "/".join(item["repository_url"].split("/")[-2:]); num = item["number"]
            try:
                comments = [c for c in get(f"{API}/repos/{repo}/pulls/{num}/comments", per_page=100) if "greptile" in (c.get("user") or {}).get("login", "").lower()]
                if not comments: continue
                commits = []
                for c in get(f"{API}/repos/{repo}/pulls/{num}/commits", per_page=100):
                    files = {f["filename"] for f in get(c["url"]).get("files", [])}
                    commits.append((c["commit"]["committer"]["date"], files))
            except requests.HTTPError as e:
                print(f"  skip {repo}#{num}: {e}"); continue
            for i, c in enumerate(comments):
                addressed = any(when > c["created_at"] and c.get("path") in files for when, files in commits)
                out.write(json.dumps({"pr": item["html_url"], "repo": repo, "body": c.get("body") or "", "path": c.get("path") or "",
                                      "idx": i, "n_in_pr": len(comments), "addressed": int(addressed)}) + "\n")
            print(f"  {n}/{len(prs[:max_prs])} {repo}#{num}: {len(comments)} comments", flush=True)

LABELS = ROOT / "data" / "llm_labels.jsonl"
GRADE = """You are grading one automated code review comment. File: {path}
Comment:
{body}

Reply with only JSON: {{"severity": 1-5, "concrete": true or false}}
severity: 1 = style nit or naming, 2 = minor cleanup, 3 = likely real issue, 4 = real bug, 5 = definite bug, crash or security hole.
concrete: true only if the comment names the exact problem and a specific fix."""

def _key(r): return r["pr"] + "|" + str(r["idx"])

def label(model="gpt-4o-mini"):
    need("OPENAI_API_KEY")
    from openai import OpenAI
    client = OpenAI(max_retries=8); rows = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines()]
    done = {json.loads(l)["key"] for l in LABELS.read_text(encoding="utf-8").splitlines()} if LABELS.exists() else set()
    with LABELS.open("a", encoding="utf-8") as out:
        for n, r in enumerate(rows, 1):
            if _key(r) in done: continue
            txt = client.chat.completions.create(model=model, temperature=0, max_tokens=40, response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": GRADE.format(path=r["path"], body=r["body"][:3000])}]).choices[0].message.content
            try: j = json.loads(txt); sev = int(j.get("severity", 3)); con = bool(j.get("concrete", False))
            except Exception: sev, con = 3, False
            out.write(json.dumps({"key": _key(r), "severity": max(1, min(5, sev)), "concrete": con}) + "\n")
            if n % 25 == 0: print(f"  graded {n}/{len(rows)}", flush=True)

def load_labels():
    if not LABELS.exists(): return {}
    return {j["key"]: j for j in (json.loads(l) for l in LABELS.read_text(encoding="utf-8").splitlines())}

def features(r, llm=None):
    b = r["body"].lower(); f = {}
    f["bias"] = 1.0
    f["log_len"] = math.log1p(len(b)) / 8
    f["suggestion_block"] = float("```suggestion" in b)
    f["code_fence"] = float("```" in b)
    f["hedge"] = float(any(w in b for w in HEDGE))
    f["certain"] = float(any(w in b for w in SURE))
    for k, ws in CATS.items(): f["cat_" + k] = float(any(w in b for w in ws))
    ext = os.path.splitext(r["path"])[1].lower()
    for e in EXTS: f["ext" + e] = float(ext == e)
    f["first_in_pr"] = float(r["idx"] == 0)
    f["log_n_in_pr"] = math.log1p(r["n_in_pr"]) / 4
    f["rel_position"] = r["idx"] / max(1, r["n_in_pr"] - 1)
    if llm is not None:
        f["llm_severity"] = (llm.get("severity", 3) - 1) / 4; f["llm_concrete"] = float(llm.get("concrete", False))
    return f

def train(X, y, epochs=300, lr=0.5, l2=1e-3):
    keys = sorted({k for x in X for k in x}); w = {k: 0.0 for k in keys}
    pos = max(1, sum(y)); neg = max(1, len(y) - sum(y)); cw = {1: len(y) / (2 * pos), 0: len(y) / (2 * neg)}
    for _ in range(epochs):
        g = {k: 0.0 for k in keys}
        for x, t in zip(X, y):
            p = 1 / (1 + math.exp(-sum(w[k] * v for k, v in x.items())))
            for k, v in x.items(): g[k] += cw[t] * (p - t) * v
        for k in keys: w[k] -= lr * (g[k] / len(y) + l2 * w[k])
    return w

def score(w, x): return sum(w.get(k, 0.0) * v for k, v in x.items())

def auc(scores, y):
    pairs = sorted(zip(scores, y)); ranks = {}; i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]: j += 1
        for k in range(i, j): ranks[k] = (i + j + 1) / 2
        i = j
    pos = [ranks[i] for i, (_, t) in enumerate(pairs) if t]; P, N = len(pos), len(y) - len(pos)
    return (sum(pos) - P * (P + 1) / 2) / (P * N) if P and N else float("nan")

def cut_at_recall(scores, y, target=0.9):
    """Lowest threshold that keeps >= target of addressed comments; report how many unaddressed get dropped."""
    order = sorted(zip(scores, y), reverse=True); P = sum(y); kept_pos = 0
    for i, (s, t) in enumerate(order):
        kept_pos += t
        if P and kept_pos / P >= target:
            kept = order[: i + 1]; dropped_neg = sum(1 for _, t2 in order[i + 1:] if not t2); N = len(y) - P
            return {"kept": len(kept), "dropped_unaddressed": dropped_neg, "unaddressed": N,
                    "noise_cut": dropped_neg / N if N else float("nan"), "precision_kept": kept_pos / len(kept)}
    return {}

def _cv_scores(rows, ys, fold_of, folds, featfn):
    scores = [None] * len(rows)
    for k in range(folds):
        tr = [i for i, r in enumerate(rows) if fold_of[r["repo"]] != k]; te = [i for i, r in enumerate(rows) if fold_of[r["repo"]] == k]
        if not tr or not te: continue
        w = train([featfn(rows[i]) for i in tr], [ys[i] for i in tr])
        for i in te: scores[i] = score(w, featfn(rows[i]))
    idx = [i for i, s in enumerate(scores) if s is not None]
    return [scores[i] for i in idx], [ys[i] for i in idx]

def _rate(rows, pick):
    g = [r["addressed"] for r in rows if pick(r)]; return (sum(g) / len(g), len(g)) if g else (float("nan"), 0)

def evaluate(folds=5, seed=3, data=None, out_dir=None):
    data = data or DATA; out_dir = out_dir or (ROOT / "results")
    rows = [json.loads(l) for l in Path(data).read_text(encoding="utf-8").splitlines()]
    labels = load_labels() if data == DATA else {}
    repos = sorted({r["repo"] for r in rows}); random.Random(seed).shuffle(repos)
    fold_of = {r: i % folds for i, r in enumerate(repos)}        # whole repos held out: no leakage across a codebase
    ys = [r["addressed"] for r in rows]; base = sum(ys) / len(ys)
    runs = {"Surface features": lambda r: features(r)}
    if labels and all(_key(r) in labels for r in rows):
        runs["Surface + LLM grades"] = lambda r: features(r, labels[_key(r)])
    res = {}
    for name, fn in runs.items():
        s_, y_ = _cv_scores(rows, ys, fold_of, folds, fn); res[name] = {"auc": auc(s_, y_), "cut": cut_at_recall(s_, y_, 0.9)}
    groups = [("Hedged (might, consider, maybe)", lambda r: any(w in r["body"].lower() for w in HEDGE)),
              ("Not hedged", lambda r: not any(w in r["body"].lower() for w in HEDGE)),
              ("Style or naming", lambda r: any(w in r["body"].lower() for w in CATS["style"])),
              ("Has a suggested code change", lambda r: "```suggestion" in r["body"].lower()),
              ("First comment on the PR", lambda r: r["idx"] == 0), ("Later comments on the PR", lambda r: r["idx"] > 0)]
    if labels:
        groups += [("LLM severity 4-5 (real bug)", lambda r: labels.get(_key(r), {}).get("severity", 0) >= 4),
                   ("LLM severity 1-2 (nit or cleanup)", lambda r: 0 < labels.get(_key(r), {}).get("severity", 0) <= 2),
                   ("LLM says concrete fix", lambda r: labels.get(_key(r), {}).get("concrete") is True)]
    md = [f"# Greptile comment ranker, {dt.date.today()}", "",
          f"{len(rows)} Greptile review comments from {len({r['pr'] for r in rows})} merged PRs across {len(repos)} public repos. Addressed rate: {100 * base:.1f}%.", "",
          "Evaluated with whole repos held out, five folds, so the ranker never sees the codebase it scores.", "",
          "| Ranker | AUC | Unaddressed cut at 90% recall | Kept comments addressed |", "|---|---|---|---|"]
    for name, r in res.items():
        c = r["cut"]
        md.append(f"| {name} | {r['auc']:.3f} | {100 * c.get('noise_cut', float('nan')):.1f}% ({c.get('dropped_unaddressed')}/{c.get('unaddressed')}) | {100 * c.get('precision_kept', float('nan')):.1f}% vs {100 * base:.1f}% |")
    md += ["", "## Addressed rate by comment type", "", "| Comments | Count | Addressed |", "|---|---|---|"]
    for name, pick in groups:
        rt, n = _rate(rows, pick)
        if n: md.append(f"| {name} | {n} | {100 * rt:.1f}% |")
    md += ["", "## Honest limits", "",
           "- \"Addressed\" means a later commit touched the flagged file, which is Greptile's own heuristic. A developer can touch a file for an unrelated reason or fix the issue elsewhere.",
           "- Public repos only, and only repos where Greptile comments on merged PRs. Private teams may behave differently.",
           "- An AUC near 0.5 means no better than chance. Group rates on small counts are noisy; the count column says how much to trust each row.",
           "- LLM grades come from gpt-4o-mini reading only the comment text, not the code."]
    out = "\n".join(md); Path(out_dir).mkdir(parents=True, exist_ok=True); (Path(out_dir) / f"{dt.date.today()}.md").write_text(out, encoding="utf-8"); print(out)
    return res


TIMELINE = ROOT / "data" / "timelines.jsonl"

def timeline():
    """For every PR already collected, record when Greptile reviewed and every commit's time and files after that."""
    need("GITHUB_TOKEN")
    prs = sorted({json.loads(l)["pr"] for l in DATA.read_text(encoding="utf-8").splitlines()})
    done = {json.loads(l)["pr"] for l in TIMELINE.read_text(encoding="utf-8").splitlines()} if TIMELINE.exists() else set()
    with TIMELINE.open("a", encoding="utf-8") as out:
        for n, url in enumerate(prs, 1):
            if url in done: continue
            parts = url.rstrip("/").split("/"); repo, num = parts[-4] + "/" + parts[-3], parts[-1]
            try:
                gc = [{"t": c["created_at"], "path": c.get("path")} for c in get(f"{API}/repos/{repo}/pulls/{num}/comments", per_page=100)
                      if "greptile" in (c.get("user") or {}).get("login", "").lower()]
                commits = []
                for c in get(f"{API}/repos/{repo}/pulls/{num}/commits", per_page=100):
                    commits.append({"t": c["commit"]["committer"]["date"], "files": sorted({f["filename"] for f in get(c["url"]).get("files", [])})})
            except requests.HTTPError as e:
                print(f"  skip {url}: {e}"); continue
            out.write(json.dumps({"pr": url, "repo": repo, "greptile": gc, "commits": commits}) + "\n")
            if n % 10 == 0: print(f"  {n}/{len(prs)} PRs", flush=True)

def churn_rates(tl):
    """Per PR: commented files vs files Greptile never flagged, each judged by whether a commit after the review touched it."""
    rows = []
    for r in tl:
        if not r["greptile"] or not r["commits"]: continue
        t0 = min(g["t"] for g in r["greptile"])
        flagged = {g["path"] for g in r["greptile"] if g["path"]}
        before = [c for c in r["commits"] if c["t"] <= t0]; after = [c for c in r["commits"] if c["t"] > t0]
        in_pr = {f for c in before for f in c["files"]}                 # files the PR had already changed when Greptile reviewed
        touched_after = {f for c in after for f in c["files"]}
        prior = {f: sum(f in c["files"] for c in before) for f in in_pr}
        for f in in_pr:
            rows.append({"pr": r["pr"], "flagged": f in flagged, "after": f in touched_after, "busy": prior[f] >= 2})
    return rows

def control(seed=7):
    tl = [json.loads(l) for l in TIMELINE.read_text(encoding="utf-8").splitlines()]
    rows = churn_rates(tl)
    def rate(sel): g = [x["after"] for x in rows if sel(x)]; return (sum(g) / len(g), len(g)) if g else (float("nan"), 0)
    f_rate, f_n = rate(lambda x: x["flagged"]); u_rate, u_n = rate(lambda x: not x["flagged"])
    prs = sorted({x["pr"] for x in rows}); by = {}
    for x in rows: by.setdefault(x["pr"], []).append(x)
    rng = random.Random(seed); diffs = []
    for _ in range(2000):                                               # bootstrap over whole PRs
        pick = [x for p in (rng.choice(prs) for _ in prs) for x in by[p]]
        a = [x["after"] for x in pick if x["flagged"]]; b = [x["after"] for x in pick if not x["flagged"]]
        if a and b: diffs.append(sum(a) / len(a) - sum(b) / len(b))
    diffs.sort(); lo, hi = diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]
    strata = []
    for name, sel in (("Files changed once before the review", lambda x: not x["busy"]), ("Files changed 2+ times before the review", lambda x: x["busy"])):
        a, an = rate(lambda x, s=sel: s(x) and x["flagged"]); b, bn = rate(lambda x, s=sel: s(x) and not x["flagged"])
        strata.append((name, a, an, b, bn))
    md = [f"# Greptile's addressed rate against a control group, {dt.date.today()}", "",
          f"{len(prs)} merged PRs. For each PR, every file the PR had changed by the time Greptile reviewed it, split into files Greptile commented on and files it didn't. "
          "A file counts as changed afterward if any later commit on the PR touched it, which is Greptile's own addressed rule.", "",
          "| Files | Count | Changed after the review |", "|---|---|---|",
          f"| Greptile commented on it | {f_n} | **{100 * f_rate:.1f}%** |", f"| Greptile did not comment on it | {u_n} | **{100 * u_rate:.1f}%** |", "",
          f"**Churn-adjusted effect of a comment: {100 * (f_rate - u_rate):+.1f} points** (95% CI {100 * lo:+.1f} to {100 * hi:+.1f}, bootstrap over PRs).", "",
          "Split by how busy the file already was, since Greptile may comment more on files developers were already working on:", "",
          "| Files | Commented, changed after | Not commented, changed after | Difference |", "|---|---|---|---|"]
    for name, a, an, b, bn in strata:
        if an and bn: md.append(f"| {name} | {100 * a:.1f}% (n={an}) | {100 * b:.1f}% (n={bn}) | {100 * (a - b):+.1f} |")
    md += ["", "## Honest limits", "",
           "- Not a randomized test. Greptile chooses where to comment, and those files may differ in ways the busy-file split doesn't capture.",
           "- A file changed after a comment may have changed for another reason, and a comment can be addressed by editing a different file.",
           "- Public repos only. Reviews are timed by Greptile's first inline comment on each PR."]
    out = "\n".join(md); (ROOT / "results").mkdir(exist_ok=True); (ROOT / "results" / f"control-{dt.date.today()}.md").write_text(out, encoding="utf-8"); print(out)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["collect", "label", "evaluate", "timeline", "control"]); ap.add_argument("--max-prs", type=int, default=1000)
    a = ap.parse_args()
    if a.cmd == "collect": collect(a.max_prs)
    elif a.cmd == "label": label()
    elif a.cmd == "timeline": timeline()
    elif a.cmd == "control": control()
    else: evaluate()
