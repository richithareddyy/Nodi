"""Local operator commands; generated credentials never enter source control."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys


def main():
    if sys.argv[1:2] == ["policy"]:
        from .policy_files import main as policy_main
        return policy_main(sys.argv[2:])
    parser = argparse.ArgumentParser(prog="nodi")
    parser.add_argument("command", choices=["init", "serve", "demo", "evaluate"])
    parser.add_argument("--config", default=".nodi.local.json")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--domain", choices=["procurement", "research"], default="procurement")
    parser.add_argument("--dataset", help="Custom evaluation JSON; defaults to the bundled regression dataset")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.command == "init":
        config = {"admin_token": secrets.token_urlsafe(32), "agent_tokens": {secrets.token_urlsafe(32): "procurement"}, "database": "nodi.db"}
        fd = os.open(args.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(config, file, indent=2)
        print(f"Created {args.config} with owner-only permissions. Keep credentials private.")
    elif args.command == "serve":
        import uvicorn
        from .api import create_app
        from .gateway import Gateway
        from .semantic import SemanticReviewer
        config = json.loads(Path(args.config).read_text())
        semantic = config.get("semantic_service")
        evaluator = SemanticReviewer(**semantic) if semantic else None
        app = create_app(Gateway(config["database"], evaluator=evaluator), config["admin_token"], config["agent_tokens"])
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "demo":
        from .demo import run
        result = run()
        rendered = json.dumps(result, indent=2)
        if args.output:
            Path(args.output).write_text(rendered + "\n")
        else:
            print(rendered)
    else:
        from .evaluation import run
        try:
            result = run(args.dataset, domain=args.domain)
        except (ValueError, OSError):
            print("Evaluation dataset could not be loaded or validated; no report written.", file=sys.stderr)
            raise SystemExit(2) from None
        rendered = json.dumps(result, indent=2)
        if args.output:
            Path(args.output).write_text(rendered + "\n")
        else:
            print(rendered)
        if result["exact_decision_accuracy"] < 1 or result["dangerous_false_allow_rate"] not in (None, 0):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
