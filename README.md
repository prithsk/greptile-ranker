# greptile-ranker

Greptile-ranker: which Greptile review comments do developers actually act on?

Greptile counts a comment as addressed when a later commit on the PR touches the flagged file. This
uses that same signal as the label, collects Greptile comments from public merged PRs, and trains a
ranker. The question: keeping 90% of the comments developers address, how much of the rest can go?

## Results

<!-- results:start -->
_Run it to populate._
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
