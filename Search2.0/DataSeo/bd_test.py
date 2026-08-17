import requests

TOKEN = "940d90d3-8f80-445a-9949-b7c8b6d2c88b"
TARGET = "https://www.coles.com.au/product/a2-milk-full-cream-milk-2l-9760091"

tests = [
    # Web Unlocker API endpoint (their documented one)
    ("zlw", f"https://api.brightdata.com/zlw?url={TARGET}", {"Authorization": f"Bearer {TOKEN}"}),
    ("zlw-get", f"https://api.brightdata.com/zlw", {"Authorization": f"Bearer {TOKEN}", "X-Target-Url": TARGET}),
    ("unlocker", f"https://api.brightdata.com/request?url={TARGET}", {"Authorization": f"Bearer {TOKEN}"}),
    ("whoami", "https://api.brightdata.com/zl/whoami", {"Authorization": f"Bearer {TOKEN}"}),
]

for name, url, headers in tests:
    try:
        r = requests.get(url, headers=headers, timeout=30)
        print(f"{name}: HTTP {r.status_code} | {r.text[:200]}")
    except Exception as e:
        print(f"{name}: ERR {type(e).__name__} {e}")
