import time
import requests
from pathlib import Path

env = {}
for line in Path(".env").read_text().splitlines():
    if line.strip() and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

LOGIN = env["DATAFORSEO_LOGIN"]
PASSWORD = env["DATAFORSEO_PASSWORD"]
BASE = "https://api.dataforseo.com/v3/merchant/google/products"

KEYWORD = "a2 Full Cream Milk 2L"
LOCATION_CODE = 2036  # Australia
SE_DOMAIN = "google.com.au"

STORES = ["woolworths", "coles", "aldi", "harris farm", "harrisfarm", "iga"]

s = requests.Session()
s.auth = (LOGIN, PASSWORD)


def post_task():
    payload = [
        {
            "keyword": KEYWORD,
            "location_code": LOCATION_CODE,
            "language_code": "en",
            "se_domain": SE_DOMAIN,
            "depth": 40,
        }
    ]
    r = s.post(BASE + "/task_post", json=payload, timeout=60)
    body = r.json()
    print(f"task_post HTTP {r.status_code} status {body.get('status_code')} {body.get('status_message')}")
    task = body.get("tasks", [])[0]
    if task.get("status_code") not in (20000, 20100):
        raise SystemExit(f"task creation failed: {task}")
    return task["id"]


def get_result(task_id):
    r = s.get(f"{BASE}/task_get/advanced/{task_id}", timeout=60)
    body = r.json()
    task = body.get("tasks", [])[0]
    print(f"task_get status {task.get('status_code')} {task.get('status_message')}")
    return task.get("result") or []


task_id = post_task()
result = None
for attempt in range(20):
    time.sleep(5)
    result = get_result(task_id)
    if result:
        break
    print(f"waiting... attempt {attempt + 1}")

if not result:
    print("no result after polling")
    raise SystemExit(1)

items = []
for res in result:
    items.extend(it for it in (res.get("items") or []) if isinstance(it, dict))

rows = []
for it in items:
    title = it.get("title", "")
    seller = (it.get("seller") or "").strip()
    price = it.get("price")
    currency = it.get("currency", "AUD")
    url = it.get("shopping_url") or it.get("url") or ""
    old_price = it.get("old_price")
    store = next((st for st in STORES if st in seller.lower()), None)
    if store:
        rows.append({"store": store, "seller": seller, "title": title, "price": price, "old_price": old_price, "currency": currency, "url": url})

print(f"\n=== {len(items)} total shopping items | {len(rows)} from target stores ===")
for r in rows:
    line = f"[{r['store']:<10}] {r['seller']} | {r['title']} | {r['currency']} {r['price']}"
    if r["old_price"]:
        line += f" (was {r['currency']} {r['old_price']})"
    print(line)

found = {r["store"] for r in rows}
missing = [st for st in ["woolworths", "coles", "aldi", "harris farm", "iga"] if st not in found]
print(f"\nFound: {sorted(found)}")
print(f"Missing: {missing}")
