# greptile-ranker

Greptile-ranker: which Greptile review comments do developers actually act on?

Greptile counts a comment as addressed when a later commit on the PR touches the flagged file. This
uses that same signal as the label, collects Greptile comments from public merged PRs, and trains a
ranker. The question: keeping 90% of the comments developers address, how much of the rest can go?

## Results

<!-- results:start -->
# Greptile's addressed rate against a control group, 2026-10-18

264 merged PRs. For each PR, every file the PR had changed by the time Greptile reviewed it, split into files Greptile commented on and files it didn't. A file counts as changed afterward if any later commit on the PR touched it, which is Greptile's own addressed rule.

| Files | Count | Changed after the review |
|---|---|---|
| Greptile commented on it | 418 | **67.9%** |
| Greptile did not comment on it | 8069 | **7.5%** |

**Churn-adjusted effect of a comment: +60.5 points** (95% CI +54.7 to +65.8, bootstrap over PRs).

Split by how busy the file already was, since Greptile may comment more on files developers were already working on:

| Files | Commented, changed after | Not commented, changed after | Difference |
|---|---|---|---|
| Files changed once before the review | 67.5% (n=348) | 8.1% (n=6118) | +59.4 |
| Files changed 2+ times before the review | 70.0% (n=70) | 5.6% (n=1951) | +64.4 |

## Honest limits

- Not a randomized test. Greptile chooses where to comment, and those files may differ in ways the busy-file split doesn't capture.
- A file changed after a comment may have changed for another reason, and a comment can be addressed by editing a different file.
- Public repos only. Reviews are timed by Greptile's first inline comment on each PR.
<!-- results:end -->

## Run it

```
pip install -r requirements.txt
cp .env.example .env    # keys: GITHUB_TOKEN
python ranker.py collect --max-prs 400
python ranker.py evaluate
```

Every run writes `results/<date>.md` with the numbers and an honest-limits section.

## License

MIT
