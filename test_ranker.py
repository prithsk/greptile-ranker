import json, random, tempfile, pathlib, ranker
tmp = pathlib.Path(tempfile.mkdtemp()); data = tmp / "comments.jsonl"            # never touches the real data/ or results/
rng = random.Random(1); rows = []
for i in range(600):
    sug = rng.random() < 0.3; nit = rng.random() < 0.3
    p = 0.15 + 0.5 * sug - 0.1 * nit
    rows.append({"pr": f"p{i//4}", "repo": f"r{i%12}", "body": ("```suggestion\nx\n```" if sug else "") + (" nit: naming" if nit else " possible null deref"),
                 "path": "a.py", "idx": i % 4, "n_in_pr": 4, "addressed": int(rng.random() < p)})
data.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
real = ranker.DATA.read_text(encoding="utf-8") if ranker.DATA.exists() else None
res = ranker.evaluate(data=data, out_dir=tmp / "results")
r = res["Surface features"]; assert r["auc"] > 0.65 and 0 < r["cut"]["noise_cut"] < 1, res
assert ranker.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
f = ranker.features(rows[0], {"severity": 5, "concrete": True}); assert f["llm_severity"] == 1.0 and f["llm_concrete"] == 1.0
assert (ranker.DATA.read_text(encoding="utf-8") if ranker.DATA.exists() else None) == real   # real collected data untouched
print("all tests passed")
