#!/usr/bin/env python3
"""Scrape gatekeeper-library examples to generate k8s-bench benchmarks."""

import os
import yaml
import requests
from pathlib import Path

GITHUB_API = "https://api.github.com/repos/open-policy-agent/gatekeeper-library/contents"
RAW_BASE = "https://raw.githubusercontent.com/open-policy-agent/gatekeeper-library/master"
CATEGORIES = ["library/general", "library/pod-security-policy"]
OUTPUT_DIR = Path(__file__).parent.parent / "tasks" / "gatekeeper"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def generate_description(constraint: str) -> str:
    """Use Gemini to generate a natural language description of the constraint."""
    if not GEMINI_API_KEY:
        return ""
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
        json={
            "contents": [{"parts": [{"text": f"""Describe this Gatekeeper constraint policy in plain English. Be concise (2-3 sentences). Focus on what the policy requires/forbids. Don't mention Gatekeeper or Kubernetes jargon.

{constraint}"""}]}]
        },
        timeout=30,
    )
    if resp.ok:
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    return ""


def fetch_json(path: str) -> list:
    """Fetch directory listing from GitHub API."""
    resp = requests.get(f"{GITHUB_API}/{path}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_raw(path: str) -> str:
    """Fetch raw file content."""
    resp = requests.get(f"{RAW_BASE}/{path}", timeout=30)
    resp.raise_for_status()
    return resp.text


def neutralize_manifest(manifest: str, suffix: str) -> str:
    """Neutralize resource names, container names, and labels to avoid leaking info."""
    docs = list(yaml.safe_load_all(manifest))
    for doc in docs:
        if not doc or "metadata" not in doc:
            continue
        # Neutralize metadata.name
        doc["metadata"]["name"] = f"resource-{suffix}"
        # Neutralize app labels if present
        labels = doc["metadata"].get("labels", {})
        if "app" in labels:
            labels["app"] = f"app-{suffix}"
        # Neutralize container names
        for key in ["containers", "initContainers"]:
            for i, c in enumerate(doc.get("spec", {}).get(key, [])):
                c["name"] = f"container-{suffix}-{i}" if i else f"container-{suffix}"
    return yaml.dump_all(docs, default_flow_style=False)


def process_sample(policy_path: str, sample_name: str) -> dict | None:
    """Process a single sample directory."""
    sample_path = f"{policy_path}/samples/{sample_name}"
    try:
        files = {f["name"]: f["path"] for f in fetch_json(sample_path)}
    except Exception:
        return None

    # Find constraint and examples
    constraint_file = files.get("constraint.yaml")
    allowed_files = [f for f in files if f.startswith("example_allowed")]
    disallowed_files = [f for f in files if f.startswith("example_disallowed") and "both" not in f]

    if not constraint_file or not allowed_files or not disallowed_files:
        return None

    constraint = fetch_raw(files["constraint.yaml"])
    allowed = fetch_raw(files[allowed_files[0]])
    disallowed = fetch_raw(files[disallowed_files[0]])

    # Neutralize names, container names, and labels
    allowed = neutralize_manifest(allowed, "alpha")
    disallowed = neutralize_manifest(disallowed, "beta")

    return {
        "constraint": constraint,
        "allowed": allowed,
        "disallowed": disallowed,
        "sample_name": sample_name,
    }


def generate_benchmark(policy_name: str, category: str, sample: dict, idx: int):
    """Generate benchmark files for a sample."""
    task_name = f"gk-{category}-{policy_name}-{idx:02d}"
    task_dir = OUTPUT_DIR / task_name
    artifacts_dir = task_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    namespace = f"gk-test-{idx:03d}"

    # Write artifacts
    (artifacts_dir / "resource-alpha.yaml").write_text(sample["allowed"])
    (artifacts_dir / "resource-beta.yaml").write_text(sample["disallowed"])
    (artifacts_dir / "constraint.yaml").write_text(sample["constraint"])

    # setup.sh
    setup = f"""#!/usr/bin/env bash
set -e
kubectl delete namespace {namespace} --ignore-not-found --wait=true
kubectl create namespace {namespace}
kubectl apply -f artifacts/resource-alpha.yaml -n {namespace}
kubectl apply -f artifacts/resource-beta.yaml -n {namespace}
echo "Resources deployed. Waiting for readiness..."
sleep 3
kubectl get all -n {namespace}
"""
    (task_dir / "setup.sh").write_text(setup)
    os.chmod(task_dir / "setup.sh", 0o755)

    # cleanup.sh
    cleanup = f"""#!/usr/bin/env bash
kubectl delete namespace {namespace} --ignore-not-found
exit 0
"""
    (task_dir / "cleanup.sh").write_text(cleanup)
    os.chmod(task_dir / "cleanup.sh", 0o755)

    # verify.sh - placeholder since model output needs manual verification
    verify = """#!/usr/bin/env bash
# Manual verification required - check if model identified the violating resource
exit 0
"""
    (task_dir / "verify.sh").write_text(verify)
    os.chmod(task_dir / "verify.sh", 0o755)

    # Generate natural language description
    description = generate_description(sample["constraint"])
    if not description:
        description = "A compliance policy is in effect for this cluster."

    # task.yaml
    task = {
        "script": [{
            "prompt": f"""{description}

Two resources have been deployed in namespace '{namespace}'. Examine them and determine which resource(s) violate the policy.

After your analysis, output a single line starting with "VIOLATING:" followed by the resource name(s) that violate the policy, or "VIOLATING: none" if all resources comply.""",
            "setup": "setup.sh",
            "cleanup": "cleanup.sh",
            "difficulty": "medium",
            "expect": [{"contains": "VIOLATING:"}, {"contains": "resource-beta"}],
        }]
    }
    (task_dir / "task.yaml").write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))

    return task_name


def main():
    """Main scraper entry point."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = []
    idx = 0

    for category_path in CATEGORIES:
        category = category_path.split("/")[-1]
        print(f"Processing {category}...")

        try:
            policies = fetch_json(category_path)
        except Exception as e:
            print(f"  Failed to fetch {category}: {e}")
            continue

        for policy in policies:
            if policy["type"] != "dir":
                continue

            policy_name = policy["name"]
            policy_path = policy["path"]
            print(f"  Policy: {policy_name}")

            try:
                samples_dir = fetch_json(f"{policy_path}/samples")
            except Exception:
                print(f"    No samples directory")
                continue

            for sample_dir in samples_dir:
                if sample_dir["type"] != "dir":
                    continue

                sample = process_sample(policy_path, sample_dir["name"])
                if sample:
                    task_name = generate_benchmark(policy_name, category, sample, idx)
                    generated.append(task_name)
                    print(f"    Generated: {task_name}")
                    idx += 1

    print(f"\nGenerated {len(generated)} benchmarks in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
