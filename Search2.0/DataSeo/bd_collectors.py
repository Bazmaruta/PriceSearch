import requests

API_KEY = "940d90d3-8f80-445a-9949-b7c8b6d2c88b"

# verify key + list collectors
r = requests.get(
    "https://api.brightdata.com/dca/collector_list",
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=30,
)
print("collector_list:", r.status_code, r.text[:500])
