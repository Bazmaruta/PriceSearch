import json
import os
import re

from serpapi import Client

API_KEY = os.getenv("SERPAPI_KEY", "YOUR_SERPAPI_KEY")

stores = {
    "Coles": "coles.com.au",
    "Woolworths": "woolworths.com.au",
    "IGA": "igashop.com.au",
    "Harris Farm": "harrisfarm.com.au",
    "ALDI": "aldi.com.au"
}


def fetch_product(query, store_name, domain):
    params = {
        "engine": "google",
        "q": f"site:{domain}/product/ OR site:{domain}/p/ OR site:{domain}/shop/ {query}",
        "location": "Australia",
        "hl": "en",
        "gl": "au",
        "api_key": API_KEY
    }

    client = Client(api_key=API_KEY)
    results = client.search(params).as_dict()

    organic_results = results.get("organic_results", [])
    if not organic_results:
        return None

    first = organic_results[0]
    title = first.get("title", "")
    link = first.get("link", "")
    snippet = first.get("snippet", "")

    rich_snippet = first.get("rich_snippet", {}).get("detected_extensions", {})
    price_val = rich_snippet.get("price")

    image_url = first.get("thumbnail") or (
        first.get("pagemap", {}).get("cse_image", [{}])[0].get("src") if "pagemap" in first else None
    )

    return {
        "product_name": title,
        "product_size": extract_size_from_title(title),
        "price": {
            "value": price_val if price_val else "N/A",
            "currency": "AUD"
        },
        "unit_price": {
            "value": f"${price_val}/ 1ea" if price_val else "N/A",
            "currency": "AUD"
        },
        "image_url": image_url or "",
        "product_url": link,
        "product_page_url": link,
        "input": {
            "search_query": query,
            "url": f"https://www.{domain}"
        }
    }


def extract_size_from_title(title):
    match = re.search(r'(\d+\s*(?:each|ea|g|kg|ml|l))', title, re.IGNORECASE)
    return match.group(0) if match else "1 each"


query_item = "Red Papaya"
product_data = []

for store, domain in stores.items():
    data = fetch_product(query_item, store, domain)
    if data:
        product_data.append(data)

print(json.dumps(product_data, indent=2))
