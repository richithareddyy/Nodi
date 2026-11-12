"""Procurement preparation; effects remain SQLite records in the gateway."""
from ...models import Action, integer, text
from ...domain_types import Prepared
from .metadata import CATALOG, SCHEMAS


def prepare(action: Action) -> Prepared:
    """Derive cost, destination and impact from typed args and server-owned metadata."""
    args = action.arguments
    schemas = SCHEMAS
    if action.tool not in schemas:
        raise ValueError("unknown tool")
    required, optional = schemas[action.tool]
    if not required <= args.keys() or not args.keys() <= required | optional:
        raise ValueError("unexpected or missing tool arguments")
    resources = {"catalog:monitors"}
    cost, category, impact = 0, "monitor", "read"
    result: dict = {}
    if action.tool == "search_products":
        text(args["query"], "query", 300)
        result = {"products": {key: item for key, item in CATALOG.items() if item["category"] == "monitor"}}
    elif action.tool == "compare_products":
        ids = args["product_ids"]
        if type(ids) is not list or not 1 <= len(ids) <= 5 or any(type(i) is not str or i not in CATALOG for i in ids):
            raise ValueError("invalid product_ids")
        category = "monitor" if all(CATALOG[i]["category"] == "monitor" for i in ids) else "other"
        result = {"products": {i: CATALOG[i] for i in ids}}
    elif action.tool == "purchase":
        product = args["product_id"]
        if type(product) is not str or product not in CATALOG:
            raise ValueError("unknown product")
        integer(args["quantity"], "quantity", 1)
        if args["quantity"] > 100:
            raise ValueError("quantity exceeds adapter limit")
        cost = CATALOG[product]["price"] * args["quantity"]
        category, impact = CATALOG[product]["category"], "irreversible"
        resources = {"vendor:approved"}
        result = {"product_id": product, "quantity": args["quantity"], "charged_cents": cost}
    elif action.tool == "send_email":
        text(args["recipient"], "recipient", 200)
        text(args["body"], "body", 2000)
        if args["recipient"] != "sales@approved.example":
            raise ValueError("recipient is outside the exact vendor allowlist")
        resources = {"vendor:approved"}
        if "attachment" in args:
            if args["attachment"] not in ("file:quote.pdf", "file:employees.csv"):
                raise ValueError("unknown attachment")
            resources.add(args["attachment"])
        impact = "external_write"
        result = {"recipient": args["recipient"], "sent": True}
    else:
        if args["file"] not in ("file:employees.csv", "file:quote.pdf") or args["destination"] != "storage:external":
            raise ValueError("unknown upload resource")
        resources = {args["file"], args["destination"]}
        impact = "external_write"
    primary = "catalog:monitors" if action.tool in ("search_products", "compare_products") else ("storage:external" if action.tool == "upload_file" else "vendor:approved")
    if action.resource != primary:
        raise ValueError("claimed resource does not match tool destination")
    return Prepared(frozenset(resources), cost, category, impact, result)
