import json
import os
import re

from serpapi import Client

API_KEY = os.getenv("SERPAPI_KEY", "YOUR_SERPAPI_KEY")

STORES = ["Coles", "Woolworths", "IGA", "Harris Farm", "ALDI"]


def get_supermarket_product(query, store_name):
    params = {
        "engine": "google_shopping",
        "q": f"{store_name} {query}",
        "location": "Australia",
        "hl": "en",
        "gl": "au",
        "direct_link": "true",
        "api_key": API_KEY
    }

    client = Client(api_key=API_KEY)
    results = client.search(params).as_dict()
    shopping_results = results.get("shopping_results", [])

    if not shopping_results:
        return None

    item = shopping_results[0]

    extracted_price = item.get("extracted_price")
    price_str = f"{extracted_price:.2f}" if extracted_price else "N/A"

    title = item.get("title", "")
    product_url = item.get("product_link") or item.get("link") or ""

    return {
        "product_name": title,
        "product_size": extract_size(title),
        "price": {
            "value": extracted_price if extracted_price else None,
            "currency": "AUD"
        },
        "unit_price": {
            "value": f"${price_str}/ 1ea" if extracted_price else "N/A",
            "currency": "AUD"
        },
        "image_url": item.get("thumbnail", ""),
        "product_url": product_url,
        "product_page_url": product_url,
        "input": {
            "search_query": f"{store_name} {query}",
            "url": product_url
        }
    }


def extract_size(title):
    match = re.search(r'(\d+\s*(?:each|ea|g|kg|ml|l))', title, re.IGNORECASE)
    return match.group(0) if match else "1 each"


query = "Red Papaya"
output = []

for store in STORES:
    data = get_supermarket_product(query, store)
    if data:
        output.append(data)

print(json.dumps(output, indent=2))
