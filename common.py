import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
_env = ROOT / ".env"
if _env.exists():
    for _l in _env.read_text(encoding="utf-8-sig").splitlines():
        if "=" in _l and not _l.strip().startswith("#"):
            _k, _v = _l.split("=", 1)
            if _v.strip(): os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
def need(*keys):
    miss = [k for k in keys if not os.environ.get(k)]
    if miss: raise SystemExit("missing in .env: " + ", ".join(miss))
