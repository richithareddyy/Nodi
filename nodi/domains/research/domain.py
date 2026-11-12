"""A bounded research simulation: fixture pages/files, never host or network I/O."""
from ...domain_types import Intent, Prepared
from ...models import Action, Contract, text

PAGES = {
    "https://docs.python.org/3/tutorial/": "Python supports modules, functions and structured data.",
    "https://docs.python.org/3/library/sqlite3.html": "SQLite is an embedded transactional database.",
}
FILES = {"notes/source.txt": "Research topic: Python and SQLite for small local applications."}
SUMMARY = "summaries/summary.txt"
SCHEMAS = {
    "web.fetch": ({"url"}, set()),
    "filesystem.read": ({"path"}, set()),
    "filesystem.write": ({"path", "content"}, set()),
}


class ResearchEvaluator:
    def evaluate(self, contract, action, prepared):
        if prepared.category != "research":
            return Intent(0, 1, "Outside the research domain", [], "DENY")
        if action.tool == "filesystem.write":
            return Intent(.5, .5, "A proposed summary needs exact-content human review",
                          ["simulated workspace write"], "ASK_HUMAN")
        return Intent(.95, .05, "Read a trusted research fixture", sorted(prepared.resources), "ALLOW")


class ResearchDomain:
    name = "research"
    purpose = "research_assistant"
    tools = frozenset(SCHEMAS)
    resources = frozenset({"web:docs.python.org", "workspace:notes/source.txt",
                           "workspace:summaries/summary.txt"})
    tool_schemas = SCHEMAS
    trusted_metadata = {"pages": PAGES, "files": FILES, "summary_path": SUMMARY,
                        "workspace": "logical research workspace; no host filesystem"}
    aliases = {}
    evaluator = ResearchEvaluator()
    dataset = "research_cases.json"

    def default_contract(self, agent_id, now):
        return Contract(agent_id, "Research Python and SQLite; summarize approved sources.",
                        purpose=self.purpose, allowed_tools=sorted(self.tools),
                        allowed_resources=sorted(self.resources), forbidden_resources=[],
                        forbidden_operations=[], spending_limit=0, maximum_transaction=0,
                        approval_threshold=0, expires_at=now + 3600)

    def setup_action(self, visa_id):
        return Action("setup", visa_id, "filesystem.read", "workspace:notes/source.txt",
                      {"path": "notes/source.txt"})

    def prepare(self, action):
        if action.tool not in SCHEMAS:
            raise ValueError("unknown research tool")
        required, optional = SCHEMAS[action.tool]
        args = action.arguments
        if not required <= args.keys() or not args.keys() <= required | optional:
            raise ValueError("unexpected or missing research arguments")
        if action.tool == "web.fetch":
            text(args["url"], "url", 2048)
            if args["url"] not in PAGES:
                raise ValueError("URL is outside the exact trusted research fixture allowlist")
            resource, impact = "web:docs.python.org", "read"
            result = {"url": args["url"], "content": PAGES[args["url"]], "source": "offline fixture"}
        else:
            path = args["path"]
            text(path, "path", 200)
            if path.startswith("/") or "\\" in path or any(p in ("", ".", "..") for p in path.split("/")):
                raise ValueError("path must be an exact relative workspace fixture path")
            resource = "workspace:" + path
            if action.tool == "filesystem.read":
                if path not in FILES:
                    raise ValueError("file is outside the read fixture allowlist")
                impact, result = "read", {"path": path, "content": FILES[path], "source": "offline fixture"}
            else:
                if path != SUMMARY:
                    raise ValueError("only the designated summary fixture may be written")
                text(args["content"], "content", 4000)
                impact, result = "workspace_write", {"path": path, "content": args["content"], "written": True}
        if action.resource != resource:
            raise ValueError("claimed resource does not match research destination")
        return Prepared(frozenset({resource}), 0, "research", impact, result)
