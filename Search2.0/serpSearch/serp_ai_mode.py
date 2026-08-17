import json

import serpapi

client = serpapi.Client(api_key="713dfda03832401affcb06e683a5cab48bcedd9fdc60b6c936f3a0f617a5094b")
results = client.search({
  "engine": "google_ai_mode",
  "q": """Cost of "Red Papaya" from IGA, ALDI, harrisfarm, woolworths and coles, 10km radius from NSW 2115, use this format {
        "product_name": "Coles Papaya Loose | 1 each",
        "product_size": "1 each",
        "price": {
          "value": 5.9,
          "currency": "AUD"
        },
        "unit_price": {
          "value": "$5.90/ 1ea",
          "currency": "AUD"
        },
        "image_url": "https://shop.coles.com.au/wcsstore/Coles-CAS/images/6/9/5/6950578-zm.jpg",
        "product_url": "https://www.coles.com.au/product/coles-papaya-loose-1-each-6950578",
        "product_page_url": "https://www.coles.com.au/product/coles-papaya-loose-1-each-6950578",
        "input": {
          "search_query": "Red Papaya",
          "url": "https://www.coles.com.au/search/products?q="
        }
      } must include actual product url & image""",
  "hl": "en",
  "gl": "au"
})

print(json.dumps(results.as_dict(), indent=2))
