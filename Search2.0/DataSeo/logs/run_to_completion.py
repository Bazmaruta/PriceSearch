"""Keep running batches until no eligible (unscraped in 24h) products remain."""
import subprocess
import sys
import time

CONSOLE = "logs/run_to_completion.log"
WD = r"C:\Users\vprad\Agents\PriceSearch\Search2.0\DataSeo"

batch = 0
while True:
    r = subprocess.run(
        ["python", "-c", "import db; print(len(db.get_eligible(batch_size=100000)))"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=WD,
    )
    try:
        n = int(r.stdout.strip().splitlines()[-1])
    except Exception:
        n = 0
    if n <= 0:
        print(f"DONE: 0 eligible remaining after {batch} batches", flush=True)
        break
    batch += 1
    print(f"=== BATCH {batch} START {time.strftime('%H:%M:%S')} ({n} eligible) ===", flush=True)
    rr = subprocess.run(
        ["python", "run_canonical_batch.py", "--verbose"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=WD,
    )
    with open(CONSOLE, "a", encoding="utf-8") as f:
        f.write(f"=== BATCH {batch} {time.strftime('%H:%M:%S')} ===\n")
        f.write(rr.stdout)
        f.write(rr.stderr)
    print(f"=== BATCH {batch} END {time.strftime('%H:%M:%S')} exit={rr.returncode} ===", flush=True)
    time.sleep(2)

print(f"ALL DONE {time.strftime('%H:%M:%S')}", flush=True)
