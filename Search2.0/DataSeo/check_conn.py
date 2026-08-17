import os
import requests
from pathlib import Path

env = {}
for line in Path(".env").read_text().splitlines():
    if line.strip() and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

login = env.get("DATAFORSEO_LOGIN", "")
password = env.get("DATAFORSEO_PASSWORD", "")

if not login or "your_dataforseo" in login or "your_dataforseo" in password:
    print("ERROR: credentials not set in .env")
    raise SystemExit(1)

url = "https://api.dataforseo.com/v3/serp/google/locations/countries"
try:
    r = requests.get(url, auth=(login, password), timeout=30)
    print(f"HTTP {r.status_code}")
    body = r.json()
    print("status:", body.get("status_code"), "-", body.get("status_message"))
    tasks = body.get("tasks", []) or []
    result = tasks[0].get("result") if tasks else None
    print("result_count:", len(result) if result else 0)
except Exception as e:
    print("ERROR:", type(e).__name__, e)
