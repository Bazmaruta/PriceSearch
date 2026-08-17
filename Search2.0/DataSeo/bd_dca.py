import os
import time
import json
import requests
from pathlib import Path

env = {}
for line in Path(__file__).parent.joinpath(".env.brightdata").read_text().splitlines():
    if line.strip() and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

API_KEY = os.environ.get("BRIGHTDATA_API_KEY") or env.get("BRIGHTDATA_API_KEY")
COLLECTOR_ID = os.environ.get("BRIGHTDATA_COLLECTOR_ID") or env.get("BRIGHTDATA_COLLECTOR_ID")

if not API_KEY:
    raise SystemExit("BRIGHTDATA_API_KEY not set")
if not COLLECTOR_ID:
    raise SystemExit("BRIGHTDATA_COLLECTOR_ID not set (add to .env.brightdata)")

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

URL = os.environ.get("TARGET_URL") or "https://www.woolworths.com.au/shop/search/products?searchTerm=a2%20full%20cream%20milk%202l"


def trigger():
    r = requests.post(
        "https://api.brightdata.com/dca/trigger",
        headers=HEADERS,
        json={"collector": COLLECTOR_ID, "input": {"url": URL}},
        timeout=60,
    )
    r.raise_for_status()
    print("trigger:", r.json())
    return r.json().get("run_id") or r.json().get("job_id")


def poll(run_id):
    for attempt in range(40):
        time.sleep(10)
        r = requests.get(
            f"https://api.brightdata.com/dca/result/{COLLECTOR_ID}",
            params={"job_id": run_id} if run_id else {},
            headers=HEADERS,
            timeout=60,
        )
        print(f"poll {attempt + 1}: HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            print("  non-json:", r.text[:200])
            continue
        if isinstance(data, dict) and data.get("status"):
            print("  status:", data["status"])
            if data["status"] == "ready":
                return data
            if data["status"] in ("failed", "error"):
                print("  error:", data)
                return None
        elif isinstance(data, list) and data:
            return {"status": "ready", "data": data}
    return None


if __name__ == "__main__":
    run_id = trigger()
    result = poll(run_id)
    if result:
        out = Path(__file__).parent / "bd_result.json"
        out.write_text(json.dumps(result, indent=2))
        print(f"\nsaved result to {out}")
    else:
        print("no result")
