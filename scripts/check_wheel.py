"""Install one wheel in a fresh environment and exercise bundled offline commands."""

import argparse
import hashlib
import json
import subprocess
import tempfile
import venv
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="nodi-package-") as directory:
        root = Path(directory)
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin" / "python"
        # Offline commands use the standard library; no source checkout or dependency
        # installation is needed to prove that the wheel contains its own dataset.
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        origin = subprocess.run(
            [str(python), "-I", "-c", "import nodi; print(nodi.__file__)"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not Path(origin).resolve().is_relative_to(environment.resolve()):
            raise RuntimeError("package resolved outside the clean environment")

        def command(*arguments):
            result = subprocess.run(
                [str(python), "-I", "-m", "nodi", *arguments],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)

        evaluation = command("evaluate")
        with zipfile.ZipFile(wheel) as archive:
            expected_digest = hashlib.sha256(archive.read("nodi/data/cases.json")).hexdigest()
        if evaluation.get("dataset_sha256") != expected_digest:
            raise RuntimeError("installed evaluation used a different dataset")
        research = command("evaluate", "--domain", "research")
        with zipfile.ZipFile(wheel) as archive:
            research_digest = hashlib.sha256(
                archive.read("nodi/data/research_cases.json")
            ).hexdigest()
        if (
            research["dataset_sha256"] != research_digest
            or not research["cases"]
            or any(case["actual"] != case["expected"] for case in research["cases"])
        ):
            raise RuntimeError("installed research evaluation failed")
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "mission": "Research approved sources.",
                    "domain": "research",
                    "allow": ["web.fetch"],
                    "deny": ["filesystem.*"],
                    "budget": {"max_usd": 0, "max_transaction_usd": 0},
                    "delegation": {"max_depth": 0},
                    "approval": {"purchase_above_usd": 0},
                    "expires_in": "1h",
                }
            )
        )
        if not command("policy", "validate", str(policy))["valid"]:
            raise RuntimeError("installed policy validation failed")
        if command("policy", "explain", str(policy))["contract"]["allowed_tools"] != ["web.fetch"]:
            raise RuntimeError("installed policy compilation failed")
        policy_data = json.loads(policy.read_text())
        policy_data.update(domain="sandbox", allow=["shell.read"], deny=["filesystem.*"])
        policy.write_text(json.dumps(policy_data))
        sandbox = command("policy", "explain", str(policy))
        if (
            sandbox["mode"] != "sandbox-readonly"
            or sandbox["contract"]["purpose"] != "sandbox_readonly"
        ):
            raise RuntimeError("installed sandbox policy compilation failed")
        recovery = subprocess.run(
            [str(python), "-I", "-m", "nodi.outbox_demo"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        recovered = json.loads(recovery.stdout)
        if (
            recovered["committed_mock_charges"] != 1
            or recovered["remaining_reservations"] != 0
            or not recovered["audit_valid"]
        ):
            raise RuntimeError("installed outbox recovery failed")
        capability = subprocess.run(
            [str(python), "-I", "-m", "nodi.capability_demo"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        capability_result = json.loads(capability.stdout)
        if (
            capability_result["committed_effects"] != 1
            or capability_result["sibling_decision"] != "DENY"
            or not capability_result["revoked_token_rejected"]
            or not capability_result["audit_valid"]
        ):
            raise RuntimeError("installed capability delegation failed")
        demo = command("demo")
        if evaluation["case_count"] != 42 or evaluation["exact_decision_accuracy"] != 1:
            raise RuntimeError("installed evaluation failed")
        if [row["decision"] for row in demo] != [
            "ALLOW",
            "ALLOW",
            "DENY",
            "ASK_HUMAN",
            "ALLOW",
            "DENY",
            "DENY",
            "DENY",
        ]:
            raise RuntimeError("installed demo failed")
        print(
            "Wheel installed outside checkout: procurement/research evaluations, policy CLI, outbox recovery, capability delegation and all demo scenarios passed."
        )


if __name__ == "__main__":
    main()
