import json
from urllib.parse import quote

BASE = "https://www.coles.com.au/product/"
name = "Arnott's Tim Tam 200g"
payload = [{"url": BASE + quote(name)}]
print(json.dumps(payload, indent=2))
open(r"C:\Users\vprad\AppData\Local\Temp\opencode\bd_timtam.json", "w").write(json.dumps(payload))
