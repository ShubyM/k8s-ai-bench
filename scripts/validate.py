#!/usr/bin/env python3
"""Validate generated Gatekeeper tasks using gator CLI.

Uses the gator CLI tool to validate resources against constraints without
requiring a running Kubernetes cluster. Constraints are patched to remove
namespace restrictions so they apply to our generated test resources.
"""

import argparse
import logging
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import yaml

# === Constants ===

# Default namespace to use for test resources (will be injected into resources)
TEST_NAMESPACE = "default"

# === Dataclasses ===


@dataclass
class ValidationResult:
    task_name: str
    passed: bool
    alpha_violations: list[str] = field(default_factory=list)
    beta_violations: list[str] = field(default_factory=list)
    error: str | None = None


# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)
print_lock = Lock()


# === Command Execution ===


def run_cmd(
    cmd: list[str],
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


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


# === Constraint/Template Discovery ===


def find_policy_files(task_dir: Path, library_dir: Path) -> tuple[Path | None, Path | None]:
    """Find ConstraintTemplate and Constraint files for a task.

    Returns:
        Tuple of (template_path, constraint_path) or (None, None) if not found.
    """
    # Parse task name: gk-general-<policyname>-<index>
    parts = task_dir.name.split("-")
    if len(parts) < 4:
        return None, None

    policy_name = "-".join(parts[2:-1])  # Handle multi-word policy names

    # Look in library
    policy_dir = library_dir / "library" / "general" / policy_name
    if not policy_dir.exists():
        # Try case-insensitive match or without dashes
        general_dir = library_dir / "library" / "general"
        if general_dir.exists():
            for candidate in general_dir.iterdir():
                if candidate.name.lower().replace("-", "") == policy_name.lower().replace("-", ""):
                    policy_dir = candidate
                    break

    if not policy_dir.exists():
        return None, None

    template_file = policy_dir / "template.yaml"

    # Find first sample's constraint
    samples_dir = policy_dir / "samples"
    constraint_file = None
    if samples_dir.exists():
        for sample in sorted(samples_dir.iterdir()):
            if sample.is_dir():
                c = sample / "constraint.yaml"
                if c.exists():
                    constraint_file = c
                    break

    return (
        template_file if template_file.exists() else None,
        constraint_file,
    )


# === Constraint Patching ===


def patch_constraint_for_validation(constraint_yaml: str) -> str:
    """Patch a constraint to work with our test resources.

    Modifications:
    - Remove namespace restrictions from match section
    - Keep kind restrictions intact
    - Ensure the constraint will match our test resources
    """
    docs = list(yaml.safe_load_all(constraint_yaml))
    patched_docs = []

    for doc in docs:
        if not doc:
            continue

        # Remove namespace restrictions from match
        if "spec" in doc and "match" in doc["spec"]:
            match = doc["spec"]["match"]
            # Remove namespace restrictions
            match.pop("namespaces", None)
            match.pop("excludedNamespaces", None)

        patched_docs.append(doc)

    return yaml.dump_all(patched_docs, default_flow_style=False)


def add_namespace_to_resources(resources_yaml: str, namespace: str) -> str:
    """Add namespace to resources that don't have one."""
    docs = list(yaml.safe_load_all(resources_yaml))
    patched_docs = []

    for doc in docs:
        if not doc:
            continue

        # Add namespace to metadata if not present
        if "metadata" in doc:
            if "namespace" not in doc["metadata"]:
                doc["metadata"]["namespace"] = namespace

        patched_docs.append(doc)

    return yaml.dump_all(patched_docs, default_flow_style=False)


# === Gator Validation ===


def run_gator_test(
    template_path: Path,
    constraint_yaml: str,
    resources_yaml: str,
) -> tuple[bool, list[str]]:
    """Run gator test and return (has_violations, violation_messages).

    Args:
        template_path: Path to the ConstraintTemplate YAML
        constraint_yaml: Patched constraint YAML content
        resources_yaml: Resources to validate

    Returns:
        Tuple of (has_violations, list of violation messages)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Write patched constraint
        constraint_path = tmpdir_path / "constraint.yaml"
        constraint_path.write_text(constraint_yaml)

        # Write resources with namespace
        resources_path = tmpdir_path / "resources.yaml"
        resources_with_ns = add_namespace_to_resources(resources_yaml, TEST_NAMESPACE)
        resources_path.write_text(resources_with_ns)

        # Run gator test
        result = run_cmd(
            [
                "gator",
                "test",
                "-f",
                str(template_path),
                "-f",
                str(constraint_path),
                "-f",
                str(resources_path),
            ],
            check=False,
        )

        # Parse output for violations
        violations = []
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    violations.append(line.strip())

        # gator returns non-zero if there are violations
        has_violations = result.returncode != 0 or len(violations) > 0

        return has_violations, violations


def extract_violated_resources(violations: list[str]) -> list[str]:
    """Extract resource names from gator violation output."""
    resources = []
    for violation in violations:
        # Gator output format varies, try to extract resource names
        # Common patterns: "resource-alpha-0", "resource-beta-0", etc.
        if "resource-alpha" in violation:
            # Extract the resource name
            for part in violation.split():
                if "resource-alpha" in part:
                    # Clean up the name
                    name = part.strip("\"'[](),")
                    if name.startswith("resource-alpha"):
                        resources.append(name.split("/")[-1])
                        break
        if "resource-beta" in violation:
            for part in violation.split():
                if "resource-beta" in part:
                    name = part.strip("\"'[](),")
                    if name.startswith("resource-beta"):
                        resources.append(name.split("/")[-1])
                        break
    return list(set(resources))


# === Task Validation ===


def validate_task(task_dir: Path, library_dir: Path) -> ValidationResult:
    """Validate a single task using gator CLI.

    Expected behavior:
    - Alpha resources should NOT violate the constraint (compliant)
    - Beta resources SHOULD violate the constraint (non-compliant)
    """
    task_name = task_dir.name
    artifacts_dir = task_dir / "artifacts"

    alpha_path = artifacts_dir / "resource-alpha.yaml"
    beta_path = artifacts_dir / "resource-beta.yaml"

    # Find policy files
    template_path, constraint_path = find_policy_files(task_dir, library_dir)

    if not template_path:
        return ValidationResult(
            task_name=task_name,
            passed=False,
            error=f"ConstraintTemplate not found for {task_name}",
        )

    if not constraint_path:
        return ValidationResult(
            task_name=task_name,
            passed=False,
            error=f"Constraint not found for {task_name}",
        )

    if not alpha_path.exists() or not beta_path.exists():
        return ValidationResult(
            task_name=task_name,
            passed=False,
            error="Missing resource-alpha.yaml or resource-beta.yaml",
        )

    # Patch constraint for validation
    try:
        constraint_yaml = constraint_path.read_text()
        patched_constraint = patch_constraint_for_validation(constraint_yaml)
    except Exception as e:
        return ValidationResult(
            task_name=task_name,
            passed=False,
            error=f"Failed to patch constraint: {e}",
        )

    # Test alpha resources (should NOT violate)
    try:
        alpha_yaml = alpha_path.read_text()
        alpha_has_violations, alpha_violations = run_gator_test(
            template_path, patched_constraint, alpha_yaml
        )
    except Exception as e:
        return ValidationResult(
            task_name=task_name,
            passed=False,
            error=f"Failed to test alpha resources: {e}",
        )

    # Test beta resources (SHOULD violate)
    try:
        beta_yaml = beta_path.read_text()
        beta_has_violations, beta_violations = run_gator_test(
            template_path, patched_constraint, beta_yaml
        )
    except Exception as e:
        return ValidationResult(
            task_name=task_name,
            passed=False,
            error=f"Failed to test beta resources: {e}",
        )

    # Determine pass/fail
    # PASS = alpha has NO violations AND beta HAS violations
    alpha_ok = not alpha_has_violations
    beta_ok = beta_has_violations
    passed = alpha_ok and beta_ok

    return ValidationResult(
        task_name=task_name,
        passed=passed,
        alpha_violations=alpha_violations,
        beta_violations=beta_violations,
    )


# === Parallel Validation ===


def validate_tasks_parallel(
    tasks: list[Path],
    library_dir: Path,
    max_workers: int = 8,
) -> list[ValidationResult]:
    """Validate multiple tasks in parallel."""
    results = []

    log.info(f"Validating {len(tasks)} tasks with {max_workers} parallel workers")

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="validator") as executor:
        future_to_task = {
            executor.submit(validate_task, task, library_dir): task for task in tasks
        }

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                results.append(result)

                status = "PASS" if result.passed else "FAIL"
                with print_lock:
                    log.info(f"{result.task_name}: {status}")

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
        result = validate_task(task_dir, library_dir)
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        log.info(f"  Result: {status}")

    return results


# === Reporting ===


def print_summary(results: list[ValidationResult]):
    """Print validation summary."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    print(f"\n{'='*60}")
    print(f"Validation Results: {len(passed)}/{len(results)} passed")
    print(f"{'='*60}")

    if passed:
        print(f"\nPASSED ({len(passed)}):")
        for r in sorted(passed, key=lambda x: x.task_name):
            print(f"  {r.task_name}")

    if failed:
        print(f"\nFAILED ({len(failed)}):")
        for r in sorted(failed, key=lambda x: x.task_name):
            print(f"\n  {r.task_name}")
            if r.error:
                print(f"    Error: {r.error}")
            else:
                if r.alpha_violations:
                    print(f"    Alpha violations (should be none): {len(r.alpha_violations)}")
                    for v in r.alpha_violations[:3]:  # Show first 3
                        print(f"      - {v[:80]}...")
                else:
                    print("    Alpha: OK (no violations)")

                if r.beta_violations:
                    print(f"    Beta violations (expected): {len(r.beta_violations)}")
                else:
                    print("    Beta: PROBLEM (no violations, but expected some)")


# === CLI ===


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate Gatekeeper tasks using gator CLI."
    )
    parser.add_argument(
        "--parallel",
        "-p",
        type=int,
        default=8,
        metavar="N",
        help="Number of parallel validations (default: 8, use 1 for sequential)",
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

    # Check gator is available
    result = run_cmd(["gator", "version"], check=False)
    if result.returncode != 0:
        log.error("gator CLI not found. Install it from: https://open-policy-agent.github.io/gatekeeper/website/docs/gator/")
        sys.exit(1)
    log.info(f"Using gator: {result.stdout.strip()}")

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


if __name__ == "__main__":
    main()
