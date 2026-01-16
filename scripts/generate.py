#!/usr/bin/env python3
"""Generate Gatekeeper benchmark tasks from OPA Gatekeeper Library."""

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

# === Constants ===

REPO_URL = "https://github.com/open-policy-agent/gatekeeper-library.git"
LIBRARY_CATEGORY = "library/general"

EXCLUDED_POLICIES = [
    "verifydeprecatedapi",        # deprecated API checks not useful for benchmarks
    "ephemeralstoragelimit",      # ephemeral storage not commonly configured
    "forbidden-sysctls",          # requires complex sysctl values
    "flexvolume-drivers",         # test drivers don't exist on standard clusters
    "proc-mount",                 # requires Kubelet feature gate
    "read-only-root-filesystem",  # requires specific image or complex patching
    "containerresourceratios",    # can produce invalid manifests (requests > limits)
    "allowedrepos",               # users preferred v2
]

IMAGE_FIXES = {
    "openpolicyagent/opa": "nginx:latest",
}

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# === Dataclasses ===

@dataclass
class ConstraintTemplate:
    name: str
    path: Path

@dataclass
class Sample:
    name: str
    constraint_yaml: str
    allowed_manifests: list[str]
    disallowed_manifests: list[str]

@dataclass
class GenerationResult:
    task_name: str
    success: bool
    skip_reason: str | None = None

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# === Policy Extraction ===

def clone_gatekeeper_library(dest: Path) -> Path:
    """Clone or update the gatekeeper-library repository."""
    if dest.exists():
        log.info(f"Updating existing repo at {dest}")
        subprocess.run(["git", "pull"], cwd=dest, check=True, capture_output=True)
    else:
        log.info(f"Cloning {REPO_URL} to {dest}")
        subprocess.run(
            ["git", "clone", "--depth", "1", REPO_URL, str(dest)],
            check=True,
            capture_output=True
        )
    return dest


def find_constraint_templates(library_path: Path) -> list[ConstraintTemplate]:
    """Find all constraint template directories with samples."""
    category_path = library_path / LIBRARY_CATEGORY
    if not category_path.exists():
        log.error(f"Category path not found: {category_path}")
        return []

    templates = []
    for policy_dir in sorted(category_path.iterdir()):
        if not policy_dir.is_dir() or policy_dir.name.startswith("."):
            continue

        # Check if excluded
        if any(excluded in policy_dir.name for excluded in EXCLUDED_POLICIES):
            log.info(f"Skipping excluded policy: {policy_dir.name}")
            continue

        # Must have samples directory
        samples_dir = policy_dir / "samples"
        if not samples_dir.exists():
            log.debug(f"No samples directory for {policy_dir.name}")
            continue

        templates.append(ConstraintTemplate(name=policy_dir.name, path=policy_dir))

    log.info(f"Found {len(templates)} constraint templates")
    return templates


def load_samples(template: ConstraintTemplate) -> list[Sample]:
    """Load test samples for a constraint template."""
    samples_dir = template.path / "samples"
    samples = []

    for sample_dir in sorted(samples_dir.iterdir()):
        if not sample_dir.is_dir() or sample_dir.name.startswith("."):
            continue

        # Required: constraint.yaml
        constraint_file = sample_dir / "constraint.yaml"
        if not constraint_file.exists():
            continue

        # Find allowed and disallowed examples
        allowed = []
        disallowed = []
        for f in sample_dir.iterdir():
            if not f.is_file():
                continue
            if f.name.startswith("example_allowed"):
                allowed.append(f.read_text())
            elif f.name.startswith("example_disallowed") and "both" not in f.name:
                disallowed.append(f.read_text())

        if not allowed or not disallowed:
            log.debug(f"Sample {sample_dir.name} missing allowed/disallowed examples")
            continue

        samples.append(Sample(
            name=sample_dir.name,
            constraint_yaml=constraint_file.read_text(),
            allowed_manifests=allowed,
            disallowed_manifests=disallowed,
        ))

    return samples


# === AI Generation ===

def generate_policy_description(constraint_yaml: str) -> str | None:
    """Use Gemini to generate a natural language description of the constraint."""
    if not GEMINI_API_KEY:
        log.warning("No GEMINI_API_KEY set, skipping description generation")
        return None

    prompt = f"""Describe this Gatekeeper constraint policy in plain English. Be concise (2-3 sentences). Focus on what the policy requires/forbids. Don't mention Gatekeeper or Kubernetes jargon.

{constraint_yaml}"""

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        if resp.ok:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        log.warning(f"Gemini API error: {resp.status_code}")
    except Exception as e:
        log.warning(f"Gemini API exception: {e}")

    return None


def fix_manifest_for_kind(manifest: str, policy_desc: str, policy_name: str, is_allowed: bool) -> str | None:
    """Use Gemini to fix manifest to be deployable on Kind while preserving test logic."""
    if not GEMINI_API_KEY:
        return manifest

    test_type = "ALLOWED (should pass)" if is_allowed else "DISALLOWED (should violate)"
    preserve_instr = (
        "Ensure the resource remains COMPLIANT with the policy."
        if is_allowed else
        "Ensure the resource remains NON-COMPLIANT (violating) the policy."
    )

    prompt = f"""You are an expert Kubernetes engineer.
I have a Kubernetes manifest that is used as a test case for a Gatekeeper policy.

Policy Name: {policy_name}
Policy Description: {policy_desc}
Test Type: {test_type}

Manifest:
```yaml
{manifest}
```

Your Task:
1. **PRIMARY GOAL**: {preserve_instr}
2. **SECONDARY GOAL**: Fix "noise" to make it deployable on Kind:
   - Replace obscure images (openpolicyagent/opa, foo, ubuntu) with nginx or busybox
   - Ensure resources.requests <= resources.limits
   - Remove invalid securityContext settings
   - Fix any invalid fields

3. Return ONLY the cleaned, valid YAML block. No explanations."""

    try:
        time.sleep(1)  # Rate limit
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        if resp.ok:
            content = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Strip markdown code blocks
            if content.startswith("```yaml"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            return content.strip()
        log.warning(f"Gemini API error: {resp.status_code}")
    except Exception as e:
        log.warning(f"Gemini API exception: {e}")

    return None


# === Task Creation ===

def neutralize_manifest(manifest: str, suffix: str, index: int) -> str:
    """Neutralize manifest names to ensure uniqueness across tests."""
    docs = list(yaml.safe_load_all(manifest))
    for doc in docs:
        if not doc or "metadata" not in doc:
            continue
        doc["metadata"]["name"] = f"resource-{suffix}-{index}"
        doc["metadata"].pop("namespace", None)

        # Update app labels consistently
        new_app_label = f"app-{suffix}"
        if "labels" in doc.get("metadata", {}):
            if "app" in doc["metadata"]["labels"]:
                doc["metadata"]["labels"]["app"] = new_app_label

        # Update selector labels in spec
        spec = doc.get("spec", {})
        if "selector" in spec and "matchLabels" in spec["selector"]:
            if "app" in spec["selector"]["matchLabels"]:
                spec["selector"]["matchLabels"]["app"] = new_app_label
        if "template" in spec:
            tmpl_meta = spec["template"].get("metadata", {})
            if "labels" in tmpl_meta and "app" in tmpl_meta["labels"]:
                tmpl_meta["labels"]["app"] = new_app_label

    return yaml.dump_all(docs, default_flow_style=False)


def create_task_directory(
    output_dir: Path,
    task_name: str,
    namespace: str,
    description: str,
    alpha_manifest: str,
    beta_manifest: str,
) -> Path:
    """Create a complete task directory with all required files."""
    task_dir = output_dir / task_name
    artifacts_dir = task_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Write manifests
    (artifacts_dir / "resource-alpha.yaml").write_text(alpha_manifest)
    (artifacts_dir / "resource-beta.yaml").write_text(beta_manifest)

    # Write setup.sh
    setup_script = f"""#!/usr/bin/env bash
set -e
kubectl delete namespace {namespace} --ignore-not-found --wait=true
kubectl create namespace {namespace}
sleep 2
kubectl apply -f artifacts/resource-alpha.yaml -n {namespace}
kubectl apply -f artifacts/resource-beta.yaml -n {namespace}
sleep 3
echo "Resources deployed. Waiting for stability..."
sleep 5
"""
    (task_dir / "setup.sh").write_text(setup_script)
    os.chmod(task_dir / "setup.sh", 0o755)

    # Write cleanup.sh
    cleanup_script = f"""#!/usr/bin/env bash
kubectl delete namespace {namespace} --ignore-not-found
exit 0
"""
    (task_dir / "cleanup.sh").write_text(cleanup_script)
    os.chmod(task_dir / "cleanup.sh", 0o755)

    # Write task.yaml
    if not description:
        description = "A compliance policy is in effect for this cluster."

    task_config = {
        "setup": "setup.sh",
        "cleanup": "cleanup.sh",
        "difficulty": "medium",
        "expect": [{
            "contains": "VIOLATING:.*resource-beta.*",
            "notContains": "resource-alpha"
        }],
        "script": [{
            "prompt": f"""{description}

Look at the resources that have been deployed in namespace {namespace}. Examine them and determine which resource(s) violate the policy.

After your analysis, output a single line starting with "VIOLATING:" followed by the resource name(s) that violate the policy, or "VIOLATING: none" if all resources comply.
The output format must be EXACTLY as follows, with no leading whitespace or extra text:
VIOLATING: resource-name-1, resource-name-2

IMPORTANT: You must NOT output anything other than the XML block below in your final response.
<answer>VIOLATING: your-answer-here</answer>"""
        }]
    }
    (task_dir / "task.yaml").write_text(yaml.dump(task_config, default_flow_style=False, sort_keys=False))

    return task_dir


# === Main Orchestration ===

def process_template(
    template: ConstraintTemplate,
    output_dir: Path,
    task_index: int,
) -> list[GenerationResult]:
    """Process a single constraint template and generate tasks for all samples."""
    results = []
    samples = load_samples(template)

    if not samples:
        results.append(GenerationResult(
            task_name=template.name,
            success=False,
            skip_reason="No valid samples found"
        ))
        return results

    for sample_idx, sample in enumerate(samples):
        task_name = f"gk-general-{template.name}-{task_index + sample_idx:02d}"
        namespace = f"gk-test-{task_index + sample_idx:03d}"

        # Generate description
        description = generate_policy_description(sample.constraint_yaml)
        if not description:
            description = "A compliance policy is in effect for this cluster."

        # Neutralize and fix manifests
        allowed_parts = []
        for i, manifest in enumerate(sample.allowed_manifests):
            neutralized = neutralize_manifest(manifest, "alpha", i)
            fixed = fix_manifest_for_kind(neutralized, description, template.name, is_allowed=True)
            if fixed:
                allowed_parts.append(fixed)
            else:
                allowed_parts.append(neutralized)

        disallowed_parts = []
        for i, manifest in enumerate(sample.disallowed_manifests):
            neutralized = neutralize_manifest(manifest, "beta", i)
            fixed = fix_manifest_for_kind(neutralized, description, template.name, is_allowed=False)
            if fixed:
                disallowed_parts.append(fixed)
            else:
                disallowed_parts.append(neutralized)

        alpha_manifest = "\n---\n".join(allowed_parts)
        beta_manifest = "\n---\n".join(disallowed_parts)

        try:
            create_task_directory(
                output_dir=output_dir,
                task_name=task_name,
                namespace=namespace,
                description=description,
                alpha_manifest=alpha_manifest,
                beta_manifest=beta_manifest,
            )
            results.append(GenerationResult(task_name=task_name, success=True))
            log.info(f"Generated: {task_name}")
        except Exception as e:
            results.append(GenerationResult(
                task_name=task_name,
                success=False,
                skip_reason=str(e)
            ))
            log.error(f"Failed to generate {task_name}: {e}")

    return results


def main():
    """Main entry point for task generation."""
    script_dir = Path(__file__).parent
    repo_dir = script_dir.parent
    library_dir = repo_dir / ".gatekeeper-library"
    output_dir = repo_dir / "tasks" / "gatekeeper"

    # Clean output directory
    if output_dir.exists():
        log.info(f"Cleaning existing output directory: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clone/update library
    clone_gatekeeper_library(library_dir)

    # Find and process templates
    templates = find_constraint_templates(library_dir)

    all_results: list[GenerationResult] = []
    task_index = 0

    for template in templates:
        results = process_template(template, output_dir, task_index)
        all_results.extend(results)
        task_index += len(results)

    # Print summary
    successful = [r for r in all_results if r.success]
    skipped = [r for r in all_results if not r.success]

    print(f"\n{'='*50}")
    print(f"Generation complete: {len(successful)}/{len(all_results)} tasks")
    print(f"Output: {output_dir}")

    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for r in skipped:
            print(f"  - {r.task_name}: {r.skip_reason}")


if __name__ == "__main__":
    main()