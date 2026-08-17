"""Run N sequential batches (test mode = 1 batch each) and record status."""
import subprocess
import sys
import time

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 10
CONSOLE = sys.argv[2] if len(sys.argv) > 2 else "logs/batches_run.log"

for i in range(COUNT):
    batch_no = i + 1
    print(f"=== BATCH {batch_no} START {time.strftime('%H:%M:%S')} ===", flush=True)
    with open(CONSOLE, "a", encoding="utf-8") as f:
        f.write(f"=== BATCH {batch_no} START {time.strftime('%H:%M:%S')} ===\n")
    r = subprocess.run(
        ["python", "run_canonical_batch.py", "--verbose"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=r"C:\Users\vprad\Agents\PriceSearch\Search2.0\DataSeo",
    )
    with open(CONSOLE, "a", encoding="utf-8") as f:
        f.write(r.stdout)
        f.write(r.stderr)
        f.write(f"=== BATCH {batch_no} END {time.strftime('%H:%M:%S')} exit={r.returncode} ===\n")
    print(f"=== BATCH {batch_no} END {time.strftime('%H:%M:%S')} exit={r.returncode} ===", flush=True)

print(f"ALL {COUNT} BATCHES DONE {time.strftime('%H:%M:%S')}", flush=True)
