#!/usr/bin/env python3
import os
import yaml
import requests
from pathlib import Path
import subprocess
import shutil

GITHUB_API = "https://api.github.com/repos/open-policy-agent/gatekeeper-library/contents"
RAW_BASE = "https://raw.githubusercontent.com/open-policy-agent/gatekeeper-library/master"
CATEGORIES = ["library/general", "library/pod-security-policy"]
OUTPUT_DIR = Path(__file__).parent.parent / "tasks" / "gatekeeper"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

EXCLUDED_POLICIES = [
    "verifydeprecatedapi",
    "imagedigests",
    "ephemeralstoragelimit",
    "requiredprobes",
]

WAITABLE_KINDS = {
    "Pod": "condition=Ready",
    "Deployment": "condition=Available",
    "StatefulSet": "condition=Ready",
    "DaemonSet": "condition=Ready",
    "ReplicaSet": "condition=Ready",
    "Job": "condition=Complete",
}

def get_wait_command(manifest_str: str, namespace: str) -> list[str]:
    """Generate kubectl wait commands for supported resources."""
    cmds = []
    try:
        docs = yaml.safe_load_all(manifest_str)
        for doc in docs:
            if not doc or "kind" not in doc:
                continue
            kind = doc["kind"]
            name = doc["metadata"]["name"]
            
            if kind in WAITABLE_KINDS:
                condition = WAITABLE_KINDS[kind]
                cmds.append(f"kubectl wait --for={condition} {kind.lower()}/{name} -n {namespace} --timeout=120s")
    except Exception:
        pass
    return cmds


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

REPO_URL = "https://github.com/open-policy-agent/gatekeeper-library.git"
LOCAL_REPO = Path(__file__).parent.parent / ".gatekeeper-library"
LIBRARY_PATH = LOCAL_REPO / "library"

def clone_repo():
    """Clone or update the repository."""
    if not LOCAL_REPO.exists():
        print(f"Cloning {REPO_URL} into {LOCAL_REPO}...")
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(LOCAL_REPO)], check=True)
    else:
        print(f"Updating {REPO_URL} in {LOCAL_REPO}...")
        subprocess.run(["git", "pull"], cwd=LOCAL_REPO, check=True)

def list_dirs(path: Path) -> list[dict]:
    """List directories in a local path."""
    if not path.exists():
        return []
    return [{"name": p.name, "path": str(p), "type": "dir"} for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")]

def read_file(path: Path) -> str:
    """Read local file content."""
    return path.read_text()


def neutralize_manifest(manifest: str, suffix: str) -> str:
    """Neutralize resource names, container names, and labels to avoid leaking info."""
    docs = list(yaml.safe_load_all(manifest))
    for doc in docs:
        if not doc or "metadata" not in doc:
            continue
        # Neutralize metadata.name
        doc["metadata"]["name"] = f"resource-{suffix}"
        # Remove usage of specific namespace so we can apply to any namespace
        doc["metadata"].pop("namespace", None)
        # Neutralize app labels if present
        labels = doc["metadata"].get("labels", {})
        if "app" in labels:
            labels["app"] = f"app-{suffix}"
        
        # Track renames for annotation fixes
        renames = {}
        # Neutralize container names
        for key in ["containers", "initContainers"]:
            prefix = "init-container" if key == "initContainers" else "container"
            containers = doc.get("spec", {}).get(key, [])
            if not isinstance(containers, list):
                continue
            for i, c in enumerate(containers):
                old_name = c.get("name", "")
                new_name = f"{prefix}-{suffix}-{i}" if i else f"{prefix}-{suffix}"
                c["name"] = new_name
                if old_name:
                    renames[old_name] = new_name
        
        # Generic Fix: Update annotations that reference container names (e.g. apparmor, seccomp)
        annotations = doc["metadata"].get("annotations", {})
        if annotations and renames:
            new_annotations = {}
            for k, v in annotations.items():
                updated_k = k
                for old, new in renames.items():
                    # Check for container.apparmor.security.beta.kubernetes.io/OLD_NAME
                    if k.endswith("/" + old):
                         updated_k = k.replace("/" + old, "/" + new)
                         break
                new_annotations[updated_k] = v
            doc["metadata"]["annotations"] = new_annotations

    return yaml.dump_all(docs, default_flow_style=False)


def process_sample(policy_path: str, sample_name: str, policy_name: str) -> dict | None:
    """Process a single sample directory."""
    # policy_path is like "library/general/allowedrepos" (string from previous logic)
    # Be careful: policy_path passed from main loop is RELATIVE to LOCAL_REPO
    
    full_sample_path = LOCAL_REPO / policy_path / "samples" / sample_name
    
    if not full_sample_path.exists():
        return None

    files = {p.name: p for p in full_sample_path.iterdir() if p.is_file()}

    # Find constraint and examples
    constraint_file = files.get("constraint.yaml")
    allowed_files = [n for n in files if n.startswith("example_allowed")]
    disallowed_files = [n for n in files if n.startswith("example_disallowed") and "both" not in n]

    if not constraint_file or not allowed_files or not disallowed_files:
        return None

    constraint = read_file(constraint_file)
    allowed = read_file(files[allowed_files[0]])
    disallowed = read_file(files[disallowed_files[0]])
    
    # Fix: Forbidden Sysctls safe values
    if policy_name == "forbidden-sysctls":
         if "kernel.*" in constraint:
             constraint = constraint.replace("kernel.*", "net.ipv4.ping_group_range")
         # Disallowed: Use Safe but Forbidden sysctl
         disallowed = disallowed.replace("net.core.somaxconn", "net.ipv4.ping_group_range")
         disallowed = disallowed.replace("kernel.msgmax", "net.ipv4.ping_group_range")
         # Allowed: Use Safe and Allowed sysctl
         allowed = allowed.replace("net.core.somaxconn", "net.ipv4.ip_local_port_range")

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

    # Prepare wait commands
    wait_cmds = []
    wait_cmds.extend(get_wait_command(sample["allowed"], namespace))
    wait_cmds.extend(get_wait_command(sample["disallowed"], namespace))

    if wait_cmds:
        wait_block = 'echo "Waiting for resources to be ready..."\n' + "\n".join(wait_cmds)
    else:
        wait_block = 'echo "Resources deployed. Waiting for readiness..."\nsleep 3'

    # Add privileged label for PSP tasks
    extra_setup = ""
    if "pod-security-policy" in category:
        extra_setup = f"kubectl label namespace {namespace} pod-security.kubernetes.io/enforce=privileged\n"

    # setup.sh
    setup = f"""#!/usr/bin/env bash
set -e
kubectl delete namespace {namespace} --ignore-not-found --wait=true
kubectl create namespace {namespace}
{extra_setup}kubectl apply -f artifacts/resource-alpha.yaml -n {namespace}
kubectl apply -f artifacts/resource-beta.yaml -n {namespace}
{wait_block}
kubectl get all,ingress,networkpolicy,serviceaccount,role,rolebinding,configmap,secret -n {namespace}
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

    # Generate natural language description
    description = generate_description(sample["constraint"])
    if not description:
        description = "A compliance policy is in effect for this cluster."

    # task.yaml
    task = {
        "setup": "setup.sh",
        "cleanup": "cleanup.sh",
        "difficulty": "medium",
        "expect": [
            {"contains": "VIOLATING:"},
            {"contains": "resource-beta"},
            {"not_contains": "resource-alpha"}
        ],
        "script": [{
            "prompt": f"""{description}
    
Look at the resources that have been deployed in namespace {namespace}. Examine them and determine which resource(s) violate the policy.

After your analysis, output a single line starting with "VIOLATING:" followed by the resource name(s) that violate the policy, or "VIOLATING: none" if all resources comply."""
        }]
    }
    (task_dir / "task.yaml").write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))

    return task_name


def main():
    """Main scraper entry point."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. Clone Repo
    clone_repo()
    
    generated = []
    idx = 0

    # CATEGORIES = ["library/general", "library/pod-security-policy"] 
    # mapped to LOCAL_REPO / "library" / "general"
    
    for category_path in CATEGORIES:
        # category_path is like "library/general"
        category = category_path.split("/")[-1]
        print(f"Processing {category}...")
        
        local_category_path = LOCAL_REPO / category_path
        if not local_category_path.exists():
             print(f"  Category path {local_category_path} not found")
             continue

        for policy_dir in local_category_path.iterdir():
            if not policy_dir.is_dir() or policy_dir.name.startswith("."):
                continue

            policy_name = policy_dir.name
            
            # SKIP excluded policies
            # Check if policy_name is in EXCLUDED_POLICIES or contains any of them
            should_exclude = any(ex in policy_name for ex in EXCLUDED_POLICIES)
            if should_exclude:
                print(f"  Skipping excluded policy: {policy_name}")
                continue

            print(f"  Policy: {policy_name}")
            
            samples_dir = policy_dir / "samples"
            if not samples_dir.exists():
                print(f"    No samples directory")
                continue

            for sample_dir in samples_dir.iterdir():
                if not sample_dir.is_dir() or sample_dir.name.startswith("."):
                    continue

                # Pass relative path for consistency if needed, or adjust process_sample
                # process_sample expects policy_path relative to repo root
                rel_policy_path = policy_dir.relative_to(LOCAL_REPO)
                
                sample = process_sample(str(rel_policy_path), sample_dir.name, policy_name)
                if sample:
                    task_name = generate_benchmark(policy_name, category, sample, idx)
                    generated.append(task_name)
                    print(f"    Generated: {task_name}")
                    idx += 1

    print(f"\\nGenerated {len(generated)} benchmarks in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

