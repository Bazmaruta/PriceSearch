import json
from urllib.parse import quote

BASE = "https://www.coles.com.au/product/"
names = [
    "a2 Full Cream Milk 2L",
    "Arnott's Tim Tam 200g",
    "Bega Tasty Cheese Block 500g",
    "Kettle Sea Salt Chips 165g",
    "So Good Almond Milk 1L",
]

payload = [{"url": BASE + quote(name)} for name in names]
print(json.dumps(payload, indent=2))
open(r"C:\Users\vprad\AppData\Local\Temp\opencode\bd_batch5_enc.json", "w").write(json.dumps(payload))
