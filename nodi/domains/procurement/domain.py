"""Procurement is one registered domain; legacy tool IDs remain unchanged."""
from ...models import Action
from .contracts import draft
from .metadata import CATALOG, RESOURCES, SCHEMAS, TOOLS
from .tools import prepare
from .intent import ProcurementEvaluator


class ProcurementDomain:
    name = "procurement"
    purpose = "monitor_procurement"
    tools = TOOLS
    resources = RESOURCES
    tool_schemas = SCHEMAS
    trusted_metadata = {"catalog": CATALOG}
    aliases = {"web.search": "search_products", "product.lookup": "compare_products"}
    evaluator = ProcurementEvaluator()
    dataset = "cases.json"
    prepare = staticmethod(prepare)

    def default_contract(self, agent_id, now):
        return draft("Buy a monitor under $300.", agent_id, now)

    def setup_action(self, visa_id):
        return Action("setup", visa_id, "search_products", "catalog:monitors", {"query": "monitor"})
