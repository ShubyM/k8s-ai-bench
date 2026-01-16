#!/usr/bin/env python3
"""Validate generated Gatekeeper tasks using vCluster for fast, isolated validation.

Since Gatekeeper policies (ConstraintTemplates) are cluster-scoped CRDs, each task
gets its own vCluster to ensure complete isolation. vClusters are much faster to
create than full Kind clusters, enabling parallel validation.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

# === Constants ===

GATEKEEPER_VERSION = "3.14.0"
HOST_CLUSTER_NAME = "gatekeeper-host"
VCLUSTER_NAMESPACE_PREFIX = "vcluster-gk"
GATEKEEPER_MANIFEST = f"https://raw.githubusercontent.com/open-policy-agent/gatekeeper/v{GATEKEEPER_VERSION}/deploy/gatekeeper.yaml"

# === Dataclasses ===


@dataclass
class ValidationResult:
    task_name: str
    passed: bool
    expected_violated: list[str] = field(default_factory=list)
    actual_violated: list[str] = field(default_factory=list)
    error: str | None = None
    duration: float = 0.0


# === Logging ===

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s]: %(message)s"
)
log = logging.getLogger(__name__)
print_lock = Lock()


# === Command Execution ===


def run_cmd(
    cmd: list[str],
    check: bool = True,
    capture: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    cmd_env = os.environ.copy()
    if env:
        cmd_env.update(env)
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=cmd_env)


def run_kubectl_vcluster(
    cmd: list[str],
    kubeconfig: str,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run kubectl command against a specific vCluster."""
    full_cmd = ["kubectl", "--kubeconfig", kubeconfig] + cmd
    return run_cmd(full_cmd, check=check, capture=capture)


# === Host Cluster Management ===


def ensure_host_cluster(use_existing: bool = False) -> bool:
    """Ensure a host Kubernetes cluster is available.

    Args:
        use_existing: If True, use current kubectl context instead of creating Kind.

    Returns:
        True if a host cluster is ready, False otherwise.
    """
    if use_existing:
        log.info("Using existing Kubernetes cluster from current context")
        result = run_cmd(["kubectl", "cluster-info"], check=False)
        if result.returncode != 0:
            log.error("No existing cluster found. Please configure kubectl or remove --use-existing-cluster")
            return False
        return True

    log.info(f"Creating Kind host cluster: {HOST_CLUSTER_NAME}")

    # Delete existing cluster if present
    run_cmd(["kind", "delete", "cluster", "--name", HOST_CLUSTER_NAME], check=False)

    # Create new cluster
    result = run_cmd(["kind", "create", "cluster", "--name", HOST_CLUSTER_NAME], check=False)
    if result.returncode != 0:
        log.error(f"Failed to create host cluster: {result.stderr}")
        return False

    return True


def delete_host_cluster(use_existing: bool = False):
    """Delete the host Kind cluster (only if we created it)."""
    if use_existing:
        log.info("Keeping existing cluster (not deleting)")
        return

    log.info(f"Deleting Kind host cluster: {HOST_CLUSTER_NAME}")
    run_cmd(["kind", "delete", "cluster", "--name", HOST_CLUSTER_NAME], check=False)


# === vCluster Management ===


def create_vcluster(name: str, namespace: str) -> tuple[bool, str | None]:
    """Create a vCluster and return (success, kubeconfig_path).

    Args:
        name: Name of the vCluster
        namespace: Namespace to create the vCluster in

    Returns:
        Tuple of (success, kubeconfig_path or error_message)
    """
    log.info(f"Creating vCluster: {name} in namespace {namespace}")

    # Create namespace for vCluster
    run_cmd(["kubectl", "create", "namespace", namespace], check=False)

    # Create vCluster with minimal resources for faster startup
    result = run_cmd(
        [
            "vcluster",
            "create",
            name,
            "-n",
            namespace,
            "--connect=false",
            "--update-current=false",
        ],
        check=False,
    )

    if result.returncode != 0:
        return False, f"Failed to create vCluster: {result.stderr}"

    # Wait for vCluster to be ready
    if not wait_for_vcluster_ready(name, namespace):
        return False, "vCluster failed to become ready"

    # Get kubeconfig
    kubeconfig_path = f"/tmp/vcluster-{name}-kubeconfig"
    result = run_cmd(
        [
            "vcluster",
            "connect",
            name,
            "-n",
            namespace,
            "--update-current=false",
            "--kube-config",
            kubeconfig_path,
        ],
        check=False,
    )

    if result.returncode != 0:
        return False, f"Failed to get vCluster kubeconfig: {result.stderr}"

    return True, kubeconfig_path


def wait_for_vcluster_ready(name: str, namespace: str, timeout: int = 120) -> bool:
    """Wait for vCluster to be ready."""
    log.debug(f"Waiting for vCluster {name} to be ready...")

    start = time.time()
    while time.time() - start < timeout:
        # Check if vCluster pod is running
        result = run_cmd(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app=vcluster,release={name}",
                "-o",
                "jsonpath={.items[0].status.phase}",
            ],
            check=False,
        )

        if result.returncode == 0 and result.stdout.strip() == "Running":
            log.debug(f"vCluster {name} is ready")
            time.sleep(2)  # Brief buffer for API server
            return True

        time.sleep(3)

    log.error(f"Timed out waiting for vCluster {name}")
    return False


def delete_vcluster(name: str, namespace: str):
    """Delete a vCluster and its namespace."""
    log.debug(f"Deleting vCluster: {name}")

    run_cmd(["vcluster", "delete", name, "-n", namespace], check=False)
    run_cmd(["kubectl", "delete", "namespace", namespace, "--wait=false"], check=False)

    # Clean up kubeconfig file
    kubeconfig_path = f"/tmp/vcluster-{name}-kubeconfig"
    if os.path.exists(kubeconfig_path):
        os.remove(kubeconfig_path)


# === Gatekeeper Installation ===


def install_gatekeeper_in_vcluster(kubeconfig: str) -> bool:
    """Install Gatekeeper in a vCluster."""
    log.debug("Installing Gatekeeper in vCluster...")

    result = run_kubectl_vcluster(
        ["apply", "-f", GATEKEEPER_MANIFEST],
        kubeconfig,
        check=False,
    )

    if result.returncode != 0:
        log.error(f"Failed to install Gatekeeper: {result.stderr}")
        return False

    return True


def wait_for_gatekeeper_ready_in_vcluster(kubeconfig: str, timeout: int = 120) -> bool:
    """Wait for Gatekeeper to be ready in a vCluster."""
    log.debug("Waiting for Gatekeeper to be ready...")

    start = time.time()
    while time.time() - start < timeout:
        result = run_kubectl_vcluster(
            [
                "wait",
                "--for=condition=available",
                "deployment/gatekeeper-controller-manager",
                "-n",
                "gatekeeper-system",
                "--timeout=10s",
            ],
            kubeconfig,
            check=False,
        )

        if result.returncode == 0:
            log.debug("Gatekeeper is ready")
            time.sleep(3)  # Buffer for webhook registration
            return True

        time.sleep(5)

    log.error("Timed out waiting for Gatekeeper")
    return False


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
        general_dir = library_dir / "library" / "general"
        if general_dir.exists():
            for candidate in general_dir.iterdir():
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

    return (template_file if template_file.exists() else None, constraint_file)


def deploy_resources_in_vcluster(
    task_dir: Path,
    library_dir: Path,
    kubeconfig: str,
) -> tuple[bool, str | None]:
    """Deploy task resources in a vCluster and return (success, error)."""
    artifacts = task_dir / "artifacts"
    alpha = artifacts / "resource-alpha.yaml"
    beta = artifacts / "resource-beta.yaml"

    # Find and deploy ConstraintTemplate and Constraint
    template_file, constraint_file = find_constraint_files(task_dir, library_dir)

    if template_file:
        result = run_kubectl_vcluster(
            ["apply", "-f", str(template_file)],
            kubeconfig,
            check=False,
        )
        if result.returncode != 0:
            return False, f"Failed to deploy template: {result.stderr}"
        time.sleep(2)  # Wait for CRD to be ready
    else:
        log.warning(f"No ConstraintTemplate found for {task_dir.name}")

    if constraint_file:
        result = run_kubectl_vcluster(
            ["apply", "-f", str(constraint_file)],
            kubeconfig,
            check=False,
        )
        if result.returncode != 0:
            return False, f"Failed to deploy constraint: {result.stderr}"
        time.sleep(2)
    else:
        log.warning(f"No Constraint found for {task_dir.name}")

    # Create test namespace
    namespace = "gk-test"
    run_kubectl_vcluster(["create", "namespace", namespace], kubeconfig, check=False)

    # Deploy test resources
    for manifest in [alpha, beta]:
        if manifest.exists():
            result = run_kubectl_vcluster(
                ["apply", "-f", str(manifest), "-n", namespace],
                kubeconfig,
                check=False,
            )
            if result.returncode != 0:
                return False, f"Failed to deploy {manifest.name}: {result.stderr}"

    return True, None


def get_audit_violations_in_vcluster(kubeconfig: str, namespace: str) -> list[str]:
    """Get list of resources with audit violations in the vCluster."""
    violations = []

    # Get all constraints
    result = run_kubectl_vcluster(
        ["get", "constraints", "-o", "json"],
        kubeconfig,
        check=False,
    )

    if result.returncode != 0:
        return violations

    try:
        data = json.loads(result.stdout)
        for item in data.get("items", []):
            status = item.get("status", {})
            for violation in status.get("violations", []):
                v_name = violation.get("name", "")
                v_ns = violation.get("namespace", "")
                if v_ns == namespace and v_name:
                    violations.append(v_name)
    except json.JSONDecodeError:
        pass

    return violations


def wait_for_audit(timeout: int = 30):
    """Wait for Gatekeeper audit to complete."""
    # vCluster is isolated and has less noise, so audit is faster
    time.sleep(min(timeout, 20))


# === Task Validation ===


def validate_task_with_vcluster(
    task_dir: Path,
    library_dir: Path,
) -> ValidationResult:
    """Validate a single task using its own vCluster.

    Each task gets a dedicated vCluster with its own Gatekeeper installation,
    ensuring complete isolation of cluster-scoped CRDs.
    """
    task_name = task_dir.name
    start_time = time.time()

    # Create unique vCluster for this task
    vcluster_name = task_name.replace("_", "-").lower()[:40]  # vCluster name limits
    vcluster_namespace = f"{VCLUSTER_NAMESPACE_PREFIX}-{task_name[-2:]}"

    # Expected: beta should be violated, alpha should not
    expected_violated = ["resource-beta-0"]

    kubeconfig = None
    try:
        # Create vCluster
        success, result = create_vcluster(vcluster_name, vcluster_namespace)
        if not success:
            return ValidationResult(
                task_name=task_name,
                passed=False,
                expected_violated=expected_violated,
                error=result,
                duration=time.time() - start_time,
            )
        kubeconfig = result

        # Install Gatekeeper
        if not install_gatekeeper_in_vcluster(kubeconfig):
            return ValidationResult(
                task_name=task_name,
                passed=False,
                expected_violated=expected_violated,
                error="Failed to install Gatekeeper",
                duration=time.time() - start_time,
            )

        if not wait_for_gatekeeper_ready_in_vcluster(kubeconfig):
            return ValidationResult(
                task_name=task_name,
                passed=False,
                expected_violated=expected_violated,
                error="Gatekeeper failed to become ready",
                duration=time.time() - start_time,
            )

        # Deploy resources
        success, error = deploy_resources_in_vcluster(task_dir, library_dir, kubeconfig)
        if not success:
            return ValidationResult(
                task_name=task_name,
                passed=False,
                expected_violated=expected_violated,
                error=error,
                duration=time.time() - start_time,
            )

        # Wait for audit
        wait_for_audit()

        # Check violations
        actual_violated = get_audit_violations_in_vcluster(kubeconfig, "gk-test")

        # Determine pass/fail
        beta_violated = any("beta" in v for v in actual_violated)
        alpha_violated = any("alpha" in v for v in actual_violated)
        passed = beta_violated and not alpha_violated

        return ValidationResult(
            task_name=task_name,
            passed=passed,
            expected_violated=expected_violated,
            actual_violated=actual_violated,
            duration=time.time() - start_time,
        )

    finally:
        # Always clean up vCluster
        delete_vcluster(vcluster_name, vcluster_namespace)


# === Parallel Validation ===


def validate_tasks_parallel(
    tasks: list[Path],
    library_dir: Path,
    max_workers: int = 4,
) -> list[ValidationResult]:
    """Validate multiple tasks in parallel using vClusters."""
    results = []

    log.info(f"Validating {len(tasks)} tasks with {max_workers} parallel workers")

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="validator") as executor:
        # Submit all tasks
        future_to_task = {
            executor.submit(validate_task_with_vcluster, task, library_dir): task
            for task in tasks
        }

        # Collect results as they complete
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                results.append(result)

                status = "PASS" if result.passed else "FAIL"
                with print_lock:
                    log.info(f"{result.task_name}: {status} ({result.duration:.1f}s)")

            except Exception as e:
                results.append(
                    ValidationResult(
                        task_name=task.name,
                        passed=False,
                        error=str(e),
                    )
                )
                with print_lock:
                    log.error(f"{task.name}: ERROR - {e}")

    return results


def validate_tasks_sequential(
    tasks: list[Path],
    library_dir: Path,
) -> list[ValidationResult]:
    """Validate tasks sequentially (for debugging)."""
    results = []

    for task_dir in tasks:
        log.info(f"Validating: {task_dir.name}")
        result = validate_task_with_vcluster(task_dir, library_dir)
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        log.info(f"  Result: {status} ({result.duration:.1f}s)")

    return results


# === Reporting ===


def print_summary(results: list[ValidationResult]):
    """Print validation summary."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    total_duration = sum(r.duration for r in results)

    print(f"\n{'='*60}")
    print(f"Validation Results: {len(passed)}/{len(results)} passed")
    print(f"Total time: {total_duration:.1f}s")
    print(f"{'='*60}")

    if passed:
        print(f"\nPASSED ({len(passed)}):")
        for r in sorted(passed, key=lambda x: x.task_name):
            print(f"  {r.task_name} ({r.duration:.1f}s)")

    if failed:
        print(f"\nFAILED ({len(failed)}):")
        for r in sorted(failed, key=lambda x: x.task_name):
            print(f"\n  {r.task_name} ({r.duration:.1f}s)")
            if r.error:
                print(f"    Error: {r.error}")
            else:
                print(f"    Expected violations: {r.expected_violated}")
                print(f"    Actual violations:   {r.actual_violated}")
                if not r.actual_violated:
                    print("    ^ No violations found - constraint may not be working")
                elif any("alpha" in v for v in r.actual_violated):
                    print("    ^ Alpha resource was violated - may still be non-compliant")


# === CLI ===


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate Gatekeeper tasks using vCluster for fast, isolated validation."
    )
    parser.add_argument(
        "--parallel",
        "-p",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel validations (default: 4, use 1 for sequential)",
    )
    parser.add_argument(
        "--use-existing-cluster",
        action="store_true",
        help="Use existing Kubernetes cluster instead of creating a Kind cluster",
    )
    parser.add_argument(
        "--keep-host-cluster",
        action="store_true",
        help="Don't delete the host Kind cluster after validation",
    )
    parser.add_argument(
        "--task",
        "-t",
        type=str,
        metavar="NAME",
        help="Validate only the specified task (by name or partial match)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


# === Main ===


def main():
    """Main entry point for validation."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

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

    # Filter to specific task if requested
    if args.task:
        tasks = [t for t in tasks if args.task in t.name]
        if not tasks:
            log.error(f"No tasks matching '{args.task}' found")
            sys.exit(1)

    log.info(f"Found {len(tasks)} tasks to validate")

    # Ensure host cluster
    if not ensure_host_cluster(use_existing=args.use_existing_cluster):
        sys.exit(1)

    try:
        # Validate tasks
        if args.parallel > 1:
            results = validate_tasks_parallel(tasks, library_dir, max_workers=args.parallel)
        else:
            results = validate_tasks_sequential(tasks, library_dir)

        # Print summary
        print_summary(results)

        # Exit with error if any failed
        if any(not r.passed for r in results):
            sys.exit(1)

    finally:
        if not args.keep_host_cluster:
            delete_host_cluster(use_existing=args.use_existing_cluster)


if __name__ == "__main__":
    main()
