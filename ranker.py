"""greptile-ranker: which Greptile review comments do developers actually act on?

Greptile counts a comment as addressed when a later commit on the PR touches the flagged file. This
uses that same signal as the label, collects Greptile comments from public merged PRs, and trains a
ranker. The question: keeping 90% of the comments developers address, how much of the rest can go?

  python ranker.py collect --max-prs 400     # needs GITHUB_TOKEN in .env (gh auth token works)
  python ranker.py evaluate
"""
import argparse, collections, json, math, random, re, time, datetime as dt
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
    prs, page = [], 1
    while len(prs) < max_prs and page <= 10:
        res = get(f"{API}/search/issues", q="commenter:greptile-apps[bot] is:pr is:merged is:public", per_page=100, page=page)
        prs += [i for i in res.get("items", []) if i["html_url"] not in seen]; page += 1
        if not res.get("items"): break
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

def features(r):
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

def evaluate(folds=5, seed=3):
    rows = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines()]
    repos = sorted({r["repo"] for r in rows}); random.Random(seed).shuffle(repos)
    fold_of = {r: i % folds for i, r in enumerate(repos)}        # whole repos held out: no leakage across a codebase
    scores, ys = [None] * len(rows), [r["addressed"] for r in rows]
    for k in range(folds):
        tr = [i for i, r in enumerate(rows) if fold_of[r["repo"]] != k]; te = [i for i, r in enumerate(rows) if fold_of[r["repo"]] == k]
        if not tr or not te: continue
        w = train([features(rows[i]) for i in tr], [ys[i] for i in tr])
        for i in te: scores[i] = score(w, features(rows[i]))
    idx = [i for i, s in enumerate(scores) if s is not None]; s = [scores[i] for i in idx]; y = [ys[i] for i in idx]
    w_all = train([features(r) for r in rows], ys)
    top = sorted(((v, k) for k, v in w_all.items() if k != "bias"), reverse=True)
    res = {"comments": len(rows), "repos": len(repos), "prs": len({r["pr"] for r in rows}), "addressed_rate": sum(ys) / len(ys),
           "auc": auc(s, y), "at_90_recall": cut_at_recall(s, y, 0.9), "top_up": top[:3], "top_down": top[-3:]}
    c = res["at_90_recall"]
    md = "\n".join([f"# Greptile comment ranker, {dt.date.today()}", "",
        f"{res['comments']} Greptile review comments from {res['prs']} merged PRs across {res['repos']} public repos. Addressed rate: {100*res['addressed_rate']:.1f}%.", "",
        "Evaluated with whole repos held out, five folds, so the ranker never sees the codebase it scores.", "",
        "| Metric | Value |", "|---|---|", f"| AUC | {res['auc']:.3f} |",
        f"| Keeping 90% of addressed comments, unaddressed comments cut | **{100*c.get('noise_cut', float('nan')):.1f}%** ({c.get('dropped_unaddressed')}/{c.get('unaddressed')}) |",
        f"| Share of kept comments that get addressed | {100*c.get('precision_kept', float('nan')):.1f}% vs {100*res['addressed_rate']:.1f}% for all |", "",
        "Strongest signals toward addressed: " + ", ".join(f"`{k}` ({v:+.2f})" for v, k in res["top_up"]),
        "", "Strongest signals against: " + ", ".join(f"`{k}` ({v:+.2f})" for v, k in res["top_down"]), "",
        "## Honest limits", "",
        "- \"Addressed\" means a later commit touched the flagged file, which is Greptile's own heuristic. A developer can touch a file for an unrelated reason or fix the issue elsewhere.",
        "- Public repos only, and only repos where Greptile comments on merged PRs. Private teams may behave differently.",
        "- A plain logistic model on surface features. It shows the signal is there, not the ceiling."])
    (ROOT / "results").mkdir(exist_ok=True); (ROOT / "results" / f"{dt.date.today()}.md").write_text(md, encoding="utf-8"); print(md)
    return res

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["collect", "evaluate"]); ap.add_argument("--max-prs", type=int, default=400)
    a = ap.parse_args(); collect(a.max_prs) if a.cmd == "collect" else evaluate()
