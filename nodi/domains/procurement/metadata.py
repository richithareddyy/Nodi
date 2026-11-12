"""Trusted procurement vocabulary, schemas and prices."""
CATALOG = {
    "monitor-basic": {"category": "monitor", "price": 18000, "name": "24-inch office monitor"},
    "monitor-pro": {"category": "monitor", "price": 27500, "name": "27-inch productivity monitor"},
    "monitor-ultra": {"category": "monitor", "price": 45000, "name": "32-inch studio monitor"},
    "headphones": {"category": "audio", "price": 17900, "name": "Wireless headphones"},
}


TOOLS = frozenset({"search_products", "compare_products", "purchase", "send_email", "upload_file"})
RESOURCES = frozenset({"catalog:monitors", "vendor:approved", "file:employees.csv", "file:quote.pdf", "storage:external"})

SCHEMAS = {
    "search_products": ({"query"}, set()),
    "compare_products": ({"product_ids"}, set()),
    "purchase": ({"product_id", "quantity"}, set()),
    "send_email": ({"recipient", "body"}, {"attachment"}),
    "upload_file": ({"file", "destination"}, set()),
}
