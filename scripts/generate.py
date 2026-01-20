#!/usr/bin/env python3
"""Generate Gatekeeper benchmark tasks from OPA Gatekeeper Library."""

import logging
import os
import shutil
import subprocess

import time
import copy
from dataclasses import dataclass
from pathlib import Path
import google.genai as genai  # type: ignore
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

# Resources that should NOT be renamed by neutralize_manifest
PRESERVED_NAMES = {
    "system:aggregate-to-edit",
}

IMAGE_FIXES = {
    "openpolicyagent/opa": "nginx:latest",
    "nginx:latest": "nginx:1.14.2",
}

CLUSTER_SCOPED_KINDS = {
    "APIService",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "MutatingWebhookConfiguration",
    "Namespace",
    "Node",
    "PersistentVolume",
    "PodSecurityPolicy",
    "StorageClass",
    "ValidatingWebhookConfiguration",
}



GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# === Dataclasses ===

@dataclass
class ConstraintTemplate:
    name: str
    path: Path
    template_yaml: str

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

        template_yaml_path = policy_dir / "template.yaml"
        if not template_yaml_path.exists():
            log.warning(f"No template.yaml for {policy_dir.name}")
            continue

        templates.append(ConstraintTemplate(
            name=policy_dir.name,
            path=policy_dir,
            template_yaml=template_yaml_path.read_text(),
        ))

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


def generate_with_gemini(prompt: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    except Exception as exc:
        log.warning(f"Gemini API exception: {exc}")
        return None

    return response.text.strip() if response.text else None


def generate_audit_query(
    constraint_yaml: str,
    template_yaml: str,
    alpha_manifest: str,
    beta_manifest: str,
    namespace: str,
) -> str | None:
    """Use Gemini to generate a natural language audit query based on the policy and resources."""
    if not GEMINI_API_KEY:
        return None

    prompt = f"""You are an expert Kubernetes administrator auditing a cluster.
I have a Gatekeeper policy and some example resources (one allowed, one disallowed).

Constraint Template (Rego Logic):
```yaml
{template_yaml}
```

Constraint (Parameters/Scope):
```yaml
{constraint_yaml}
```

Allowed Resource (Alpha):
```yaml
{alpha_manifest}
```

Disallowed Resource (Beta):
```yaml
{beta_manifest}
```

Your Task:
Write a natural language request that a user would ask an AI agent to check for violations of this policy in the '{namespace}' namespace.
- The request should be a complete, natural sentence(s).
- It MUST explicitly mention the namespace {namespace} (do NOT put quotes around the namespace name).
- It should imply the violation criteria directly (e.g., "Check the gk-test-01 namespace for any pods that don't have an owner label.").
- Do NOT use technical jargon like "Constraint", "OPA", "Rego".
- Do NOT include the "If you find any..." formatting instructions (I will append those later).
- Make it sound like a human asking a helpful assistant.

Return ONLY the request string. No quotes."""

    return generate_with_gemini(prompt)


def fix_manifest_for_kind(
    manifest: str,
    policy_name: str,
    constraint_yaml: str,
    template_yaml: str,
    is_allowed: bool,
) -> str | None:
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
Test Type: {test_type}

Constraint Template (Rego Logic):
```yaml
{template_yaml}
```

Policy Constraint (do not change its intent):
```yaml
{constraint_yaml}
```

Manifest:
```yaml
{manifest}
```

Your Task:
1. **PRIMARY GOAL**: {preserve_instr}
2. **SECONDARY GOAL**: Fix "noise" to make it deployable on Kind.
3. **DO NOT** change any field that could affect policy compliance or matching.
   - Examples: `kind`, labels, selectors, namespace, image references (if policy is about images), ports/hosts if policy is about networking.
4. Only adjust safe fields: image tags when not policy-related, resource limits/requests, invalid fields, or missing required structure.
5. If you cannot safely fix the manifest, return it unchanged.
6. Return ONLY the cleaned, valid YAML block. No explanations."""

    time.sleep(1)
    content = generate_with_gemini(prompt)
    if not content:
        return None

    if content.startswith("```yaml"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]

    return content.strip() if content else None





# === Task Creation ===

def neutralize_manifest(manifest: str, suffix: str, index: int, policy_name: str = "") -> str:
    """Neutralize manifest names to ensure uniqueness across tests."""
    docs = list(yaml.safe_load_all(manifest))
    new_docs = []
    for doc in docs:
        if not doc:
            continue
        
        if "metadata" not in doc:
            new_docs.append(doc)
            continue
        
        name = doc["metadata"].get("name", "")
        if name in PRESERVED_NAMES:
            new_docs.append(doc)
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
        
        new_docs.append(doc)

    return yaml.dump_all(new_docs, default_flow_style=False)

def apply_image_fixes(manifest_str: str, policy_name: str, is_allowed: bool) -> str:
    """Replace problematic images with standard ones."""
    # Don't fix images for disallowedtags beta resources as they rely on bad tags
    if "disallowedtags" in policy_name and not is_allowed:
        return manifest_str
        
    for bad, good in IMAGE_FIXES.items():
        manifest_str = manifest_str.replace(bad, good)
    return manifest_str

def inject_dependencies(manifest_str: str, policy_name: str, is_allowed: bool) -> str:
    """Inject required dependencies or fix fields based on policy knowledge."""
    docs = list(yaml.safe_load_all(manifest_str))
    new_docs = []
    
    # Track existing kinds to avoid duplicates if possible
    existing_kinds = {doc.get("kind") for doc in docs if doc}
    
    for doc in docs:
        if not doc:
            continue
        
        kind = doc.get("kind", "")
        spec = doc.get("spec", {})
        
        # 1. HPA -> Deployment
        if kind == "HorizontalPodAutoscaler":
            scale_target = spec.get("scaleTargetRef", {})
            target_kind = scale_target.get("kind", "Deployment")
            target_name = scale_target.get("name", "nginx-deployment")
            
            # Ensure target name is set if missing
            if "name" not in scale_target:
                scale_target["name"] = target_name
                doc["spec"]["scaleTargetRef"] = scale_target
            
            if target_kind == "Deployment":
                dep_yaml = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{target_name}}
spec:
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:1.14.2
""".format(target_name=target_name)
                new_docs.append(yaml.safe_load(dep_yaml))

        # 2. Ingress HTTPS -> TLS
        if "httpsonly" in policy_name and kind == "Ingress":
            if "tls" not in spec:
                spec["tls"] = [{
                    "hosts": ["example.com"],
                    "secretName": "example-tls"
                }]
                doc["spec"] = spec
        
        # 3. StorageClass Inventory Mocking
        if "storageclass" in policy_name:
            sc_name = ""
            if kind == "PersistentVolumeClaim":
                sc_name = spec.get("storageClassName", "somestorageclass")
            elif kind == "StatefulSet":
                vcts = spec.get("volumeClaimTemplates", [])
                if vcts:
                    sc_name = vcts[0].get("spec", {}).get("storageClassName", "somestorageclass")
            
            if sc_name and sc_name != "manual": # manual usually means no class
                sc_yaml = f"""
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {{sc_name}}
  namespace: ""
provisioner: kubernetes.io/no-provisioner
""".format(sc_name=sc_name)
                new_docs.append(yaml.safe_load(sc_yaml))

        # 5. Force name for block-endpoint-edit-default-role
        if "block-endpoint-edit-default-role" in policy_name and kind == "ClusterRole":
             doc["metadata"]["name"] = "system:aggregate-to-edit"

    # Add original docs
    new_docs.extend(docs)
    return yaml.dump_all(new_docs, default_flow_style=False)

def handle_uniqueness_checks(manifest_str: str, policy_name: str, is_beta: bool) -> str:
    """Duplicate resources to create conflicts for uniqueness policies."""
    if not is_beta:
        return manifest_str
    
    if "unique" not in policy_name:
        return manifest_str

    docs = list(yaml.safe_load_all(manifest_str))
    
    # Check if we need duplication (if checking uniqueness of ingress or service)
    # Even if multiple docs exist, they might not conflict. So we duplicate relevant ones.
    
    docs_to_add = []
    for doc in docs:
        if not doc:
            continue
        kind = doc.get("kind")
        should_duplicate = False
        
        if "ingress" in policy_name and kind == "Ingress":
            should_duplicate = True
        elif "service" in policy_name and kind == "Service":
            should_duplicate = True
            
        if should_duplicate:
            doc2 = copy.deepcopy(doc)
            doc2["metadata"]["name"] = doc["metadata"]["name"] + "-conflict"
            docs_to_add.append(doc2)
    
    docs.extend(docs_to_add)
    return yaml.dump_all(docs, default_flow_style=False)


def build_manifests_for_sample(
    sample: Sample,
    template_name: str,
    template_yaml: str,
) -> tuple[str, str]:
    allowed_parts = []
    for i, manifest in enumerate(sample.allowed_manifests):
        neutralized = neutralize_manifest(manifest, "alpha", i, template_name)
        candidate = neutralized
        fixed = fix_manifest_for_kind(
            neutralized,
            template_name,
            sample.constraint_yaml,
            template_yaml,
            is_allowed=True,
        )
        if fixed:
            candidate = fixed
        processed = inject_dependencies(candidate, template_name, is_allowed=True)
        processed = apply_image_fixes(processed, template_name, is_allowed=True)
        allowed_parts.append(processed)

    disallowed_parts = []
    for i, manifest in enumerate(sample.disallowed_manifests):
        neutralized = neutralize_manifest(manifest, "beta", i, template_name)
        candidate = neutralized
        fixed = fix_manifest_for_kind(
            neutralized,
            template_name,
            sample.constraint_yaml,
            template_yaml,
            is_allowed=False,
        )
        if fixed:
            candidate = fixed
        processed = inject_dependencies(candidate, template_name, is_allowed=False)
        processed = apply_image_fixes(processed, template_name, is_allowed=False)
        processed = handle_uniqueness_checks(processed, template_name, is_beta=True)
        disallowed_parts.append(processed)

    alpha_manifest = "\n---\n".join(allowed_parts)
    beta_manifest = "\n---\n".join(disallowed_parts)

    return alpha_manifest, beta_manifest


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

If you find any, list them in this format:
VIOLATING: resource_name, resource_name

Otherwise, just output:
VIOLATING: none"""
        }]
    }
    (task_dir / "task.yaml").write_text(yaml.dump(task_config, default_flow_style=False, sort_keys=False))

    return task_dir


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

        # 1. Generate Manifests first
        alpha_manifest, beta_manifest = build_manifests_for_sample(
            sample,
            template.name,
            template.template_yaml,
        )

        # 2. Generate Audit Query using everything
        query = generate_audit_query(
            sample.constraint_yaml,
            template.template_yaml,
            alpha_manifest,
            beta_manifest,
            namespace,
        )
        try:
            task_dir = create_task_directory(
                output_dir=output_dir,
                task_name=task_name,
                namespace=namespace,
                description=query,
                alpha_manifest=alpha_manifest,
                beta_manifest=beta_manifest,
            )
            (task_dir / "constraint.yaml").write_text(sample.constraint_yaml)
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
