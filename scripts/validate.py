#!/usr/bin/env python3
"""Validate generated Gatekeeper tasks against a real Kind cluster."""

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# === Constants ===

GATEKEEPER_VERSION = "3.14.0"
CLUSTER_NAME = "gatekeeper-validate"
GATEKEEPER_MANIFEST = f"https://raw.githubusercontent.com/open-policy-agent/gatekeeper/v{GATEKEEPER_VERSION}/deploy/gatekeeper.yaml"

# === Dataclasses ===

@dataclass
class ValidationResult:
    task_name: str
    passed: bool
    expected_violated: list[str] = field(default_factory=list)
    actual_violated: list[str] = field(default_factory=list)
    error: str | None = None

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# === Cluster Management ===

def run_cmd(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def create_kind_cluster() -> bool:
    """Create a fresh Kind cluster for validation."""
    log.info(f"Creating Kind cluster: {CLUSTER_NAME}")

    # Delete existing cluster if present
    run_cmd(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)

    # Create new cluster
    result = run_cmd(["kind", "create", "cluster", "--name", CLUSTER_NAME], check=False)
    if result.returncode != 0:
        log.error(f"Failed to create cluster: {result.stderr}")
        return False

    return True


def install_gatekeeper() -> bool:
    """Install Gatekeeper on the cluster."""
    log.info(f"Installing Gatekeeper v{GATEKEEPER_VERSION}")

    result = run_cmd(["kubectl", "apply", "-f", GATEKEEPER_MANIFEST], check=False)
    if result.returncode != 0:
        log.error(f"Failed to install Gatekeeper: {result.stderr}")
        return False

    return True


def wait_for_gatekeeper_ready(timeout: int = 120) -> bool:
    """Wait for Gatekeeper to be ready."""
    log.info("Waiting for Gatekeeper to be ready...")

    start = time.time()
    while time.time() - start < timeout:
        result = run_cmd([
            "kubectl", "wait", "--for=condition=available",
            "deployment/gatekeeper-controller-manager",
            "-n", "gatekeeper-system",
            "--timeout=10s"
        ], check=False)

        if result.returncode == 0:
            log.info("Gatekeeper is ready")
            time.sleep(5)  # Extra buffer for webhook registration
            return True

        time.sleep(5)

    log.error("Timed out waiting for Gatekeeper")
    return False


def delete_kind_cluster():
    """Delete the Kind cluster."""
    log.info(f"Deleting Kind cluster: {CLUSTER_NAME}")
    run_cmd(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)


# === Task Discovery ===

def find_gatekeeper_tasks(tasks_dir: Path) -> list[Path]:
    """Find all gatekeeper task directories."""
    gk_dir = tasks_dir / "gatekeeper"
    if not gk_dir.exists():
        return []

    tasks = []
    for task_dir in sorted(gk_dir.iterdir()):
        if task_dir.is_dir() and task_dir.name.startswith("gk-"):
            task_yaml = task_dir / "task.yaml"
            if task_yaml.exists():
                tasks.append(task_dir)

    return tasks


# === Validation Logic ===

def find_constraint_files(task_dir: Path, library_dir: Path) -> tuple[Path | None, Path | None]:
    """Find ConstraintTemplate and Constraint files for a task."""
    # Parse task name: gk-general-<policyname>-<index>
    parts = task_dir.name.split("-")
    if len(parts) < 4:
        return None, None

    policy_name = "-".join(parts[2:-1])  # Handle multi-word policy names

    # Look in library
    policy_dir = library_dir / "library" / "general" / policy_name
    if not policy_dir.exists():
        # Try without dashes
        for candidate in (library_dir / "library" / "general").iterdir():
            if candidate.name.replace("-", "") == policy_name.replace("-", ""):
                policy_dir = candidate
                break

    if not policy_dir.exists():
        return None, None

    template_file = policy_dir / "template.yaml"

    # Find first sample's constraint
    samples_dir = policy_dir / "samples"
    constraint_file = None
    if samples_dir.exists():
        for sample in samples_dir.iterdir():
            c = sample / "constraint.yaml"
            if c.exists():
                constraint_file = c
                break

    return (
        template_file if template_file.exists() else None,
        constraint_file
    )


def deploy_resources(task_dir: Path, library_dir: Path) -> tuple[bool, str | None]:
    """Deploy the task's test resources and return (success, error)."""
    artifacts = task_dir / "artifacts"
    alpha = artifacts / "resource-alpha.yaml"
    beta = artifacts / "resource-beta.yaml"

    # Find and deploy ConstraintTemplate and Constraint
    template_file, constraint_file = find_constraint_files(task_dir, library_dir)

    if template_file:
        result = run_cmd(["kubectl", "apply", "-f", str(template_file)], check=False)
        if result.returncode != 0:
            return False, f"Failed to deploy template: {result.stderr}"
        time.sleep(2)  # Wait for CRD to be ready
    else:
        log.warning(f"No ConstraintTemplate found for {task_dir.name}")

    if constraint_file:
        result = run_cmd(["kubectl", "apply", "-f", str(constraint_file)], check=False)
        if result.returncode != 0:
            return False, f"Failed to deploy constraint: {result.stderr}"
        time.sleep(2)
    else:
        log.warning(f"No Constraint found for {task_dir.name}")

    # Create test namespace
    namespace = f"validate-{task_dir.name[-2:]}"
    run_cmd(["kubectl", "create", "namespace", namespace], check=False)

    # Deploy test resources
    for manifest in [alpha, beta]:
        if manifest.exists():
            result = run_cmd([
                "kubectl", "apply", "-f", str(manifest), "-n", namespace
            ], check=False)
            if result.returncode != 0:
                return False, f"Failed to deploy {manifest.name}: {result.stderr}"

    return True, None


def get_constraint_kind_from_task(task_dir: Path) -> str | None:
    """Infer the constraint kind from the task name."""
    # Task name format: gk-general-<policyname>-<index>
    # We need to find corresponding ConstraintTemplate
    # For now, we'll check audit on all constraints
    return None


def get_audit_violations(namespace: str) -> list[str]:
    """Get list of resources with audit violations in the namespace."""
    violations = []

    # Get all constraints
    result = run_cmd([
        "kubectl", "get", "constraints", "-o", "json"
    ], check=False)

    if result.returncode != 0:
        return violations

    try:
        data = json.loads(result.stdout)
        for item in data.get("items", []):
            status = item.get("status", {})
            for violation in status.get("violations", []):
                v_namespace = violation.get("enforcementAction", "")
                v_name = violation.get("name", "")
                v_ns = violation.get("namespace", "")
                if v_ns == namespace and v_name:
                    violations.append(v_name)
    except json.JSONDecodeError:
        pass

    return violations


def wait_for_audit(timeout: int = 60):
    """Wait for Gatekeeper audit to complete."""
    log.debug("Waiting for audit cycle...")
    time.sleep(min(timeout, 30))  # Audit runs every 60s by default, but partial results appear sooner


# === Task Validation ===

def validate_task(task_dir: Path, library_dir: Path) -> ValidationResult:
    """Validate a single task against Gatekeeper audit."""
    task_name = task_dir.name
    namespace = f"validate-{task_name[-2:]}"

    # Expected: beta should be violated, alpha should not
    expected_violated = ["resource-beta-0"]

    # Deploy resources
    success, error = deploy_resources(task_dir, library_dir)
    if not success:
        return ValidationResult(
            task_name=task_name,
            passed=False,
            expected_violated=expected_violated,
            error=error
        )

    # Wait for audit
    wait_for_audit()

    # Check violations
    actual_violated = get_audit_violations(namespace)

    # Determine pass/fail
    beta_violated = any("beta" in v for v in actual_violated)
    alpha_violated = any("alpha" in v for v in actual_violated)

    passed = beta_violated and not alpha_violated

    # Cleanup namespace
    run_cmd(["kubectl", "delete", "namespace", namespace], check=False)

    return ValidationResult(
        task_name=task_name,
        passed=passed,
        expected_violated=expected_violated,
        actual_violated=actual_violated,
    )


# === Reporting ===

def print_summary(results: list[ValidationResult]):
    """Print validation summary."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    print(f"\n{'='*50}")
    print(f"Validation Results: {len(passed)}/{len(results)} passed")
    print(f"{'='*50}")

    if failed:
        print(f"\nFAILED ({len(failed)}):")
        for r in failed:
            print(f"\n  {r.task_name}")
            if r.error:
                print(f"    Error: {r.error}")
            else:
                print(f"    Expected violations: {r.expected_violated}")
                print(f"    Actual violations:   {r.actual_violated}")
                if not r.actual_violated:
                    print("    ^ No violations found - constraint may not be working")
                elif any("alpha" in v for v in r.actual_violated):
                    print("    ^ Alpha resource was violated - may still be non-compliant")


# === Main ===

def main():
    """Main entry point for validation."""
    script_dir = Path(__file__).parent
    repo_dir = script_dir.parent
    tasks_dir = repo_dir / "tasks"
    library_dir = repo_dir / ".gatekeeper-library"

    if not library_dir.exists():
        log.error(f"Gatekeeper library not found at {library_dir}")
        log.error("Run generate.py first to clone the library")
        sys.exit(1)

    # Find tasks
    tasks = find_gatekeeper_tasks(tasks_dir)
    if not tasks:
        log.error("No gatekeeper tasks found")
        sys.exit(1)

    log.info(f"Found {len(tasks)} tasks to validate")

    # Create cluster
    if not create_kind_cluster():
        sys.exit(1)

    try:
        # Install Gatekeeper
        if not install_gatekeeper():
            sys.exit(1)

        if not wait_for_gatekeeper_ready():
            sys.exit(1)

        # Validate each task
        results = []
        for task_dir in tasks:
            log.info(f"Validating: {task_dir.name}")
            result = validate_task(task_dir, library_dir)
            results.append(result)

            status = "PASS" if result.passed else "FAIL"
            log.info(f"  Result: {status}")

        # Print summary
        print_summary(results)

        # Exit with error if any failed
        if any(not r.passed for r in results):
            sys.exit(1)

    finally:
        delete_kind_cluster()


if __name__ == "__main__":
    main()