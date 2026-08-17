import requests

TOKEN = "940d90d3-8f80-445a-9949-b7c8b6d2c88b"
URL = "https://www.coles.com.au/product/a2-milk-full-cream-milk-2l-9760091"

# Try without zone first to see the error, and with a guessed zone
for zone in [None, "web_unlocker", "unlocker", "zonename"]:
    body = {"url": URL, "format": "raw"}
    if zone:
        body["zone"] = zone
    try:
        r = requests.post(
            "https://api.brightdata.com/request",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
            json=body,
            timeout=90,
        )
        print(f"zone={zone}: HTTP {r.status_code} | len={len(r.content)} | {r.text[:150]}")
    except Exception as e:
        print(f"zone={zone}: ERR {type(e).__name__} {e}")
