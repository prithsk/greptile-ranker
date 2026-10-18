import json, random, ranker
rng = random.Random(1); rows = []
for i in range(600):
    sug = rng.random() < 0.3; nit = rng.random() < 0.3
    p = 0.15 + 0.5 * sug - 0.1 * nit
    rows.append({"pr": f"p{i//4}", "repo": f"r{i%12}", "body": ("```suggestion\nx\n```" if sug else "") + (" nit: naming" if nit else " possible null deref"),
                 "path": "a.py", "idx": i % 4, "n_in_pr": 4, "addressed": int(rng.random() < p)})
ranker.DATA.parent.mkdir(exist_ok=True); ranker.DATA.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
res = ranker.evaluate()
assert res["auc"] > 0.65 and 0 < res["at_90_recall"]["noise_cut"] < 1, res
assert ranker.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
ranker.DATA.unlink(); print("all tests passed")
