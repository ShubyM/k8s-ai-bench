# Gatekeeper Task Generator - Task Analysis

This document details the tasks generated from the [Gatekeeper OPA Library](https://github.com/open-policy-agent/gatekeeper-library) and explains which tasks were skipped and why.

## Summary

| Status | Count |
|--------|-------|
| Working Tasks | 17 |
| Skipped Tasks | 26 |
| **Total in Library** | **43** |

## Working Tasks (17)

These tasks deploy successfully and can be used for benchmarking:

| Task ID | Policy | Description |
|---------|--------|-------------|
| `allowed-ip` | External IPs | Restricts Service externalIPs to an allowed list |
| `allowed-repos` | Allowed Repositories | Requires containers to use images from approved repositories |
| `automount-serviceaccount-token` | Automount SA Token | Controls automountServiceAccountToken on Pods |
| `block-loadbalancer-services` | Block LoadBalancer | Disallows Services with type LoadBalancer |
| `block-wildcard-ingress` | Block Wildcard Ingress | Blocks Ingresses with blank or wildcard hostnames |
| `disallow-anonymous` | Disallow Anonymous | Prevents ClusterRoleBindings to system:anonymous |
| `disallow-interactive` | Disallow Interactive | Blocks containers with stdin/tty enabled |
| `horizontal-pod-autoscaler` | HPA Requirements | Requires HPAs to target valid deployments |
| `must-have-key` | Required Labels | Requires specific labels/annotations on resources |
| `must-have-owner` | Owner Required | Requires owner annotation on resources |
| `must-have-set-of-annotations` | Required Annotations | Requires a specific set of annotations |
| `pod-disruption-budget` | PDB Requirements | Validates PodDisruptionBudget configurations |
| `replica-limit` | Replica Limits | Enforces maximum replica count on Deployments |
| `tls-optional` | TLS Optional | Validates optional TLS configuration on Ingresses |
| `tls-required` | TLS Required | Requires TLS configuration on Ingresses |
| `unique-ingress-host` | Unique Ingress Host | Ensures Ingress hostnames are unique |
| `unique-service-selector` | Unique Service Selector | Ensures Service selectors are unique |

---

## Skipped Tasks (26)

### Category 1: Name-Sensitive Policies (2 tasks)

These policies depend on specific Kubernetes resource names that cannot be renamed without breaking the policy test.

| Task ID | Policy | Reason |
|---------|--------|--------|
| `block-endpoint-default-role` | Block Endpoint Edit Default Role | Policy checks for `system:aggregate-to-edit` ClusterRole, which is a system resource name |
| `noupdateserviceaccount` | Block ServiceAccount Updates | Policy depends on specific ServiceAccount names that cannot be changed |

---

### Category 2: Deprecated API Checks (6 tasks)

These tasks test for usage of deprecated Kubernetes API versions. They use AdmissionReview objects or reference API versions that no longer exist in current clusters.

| Task ID | K8s Version | Reason |
|---------|-------------|--------|
| `verifydeprecatedapi-1.16` | 1.16 | Uses AdmissionReview objects (not deployable resources) |
| `verifydeprecatedapi-1.22` | 1.22 | Same |
| `verifydeprecatedapi-1.25` | 1.25 | Same |
| `verifydeprecatedapi-1.26` | 1.26 | Same |
| `verifydeprecatedapi-1.27` | 1.27 | Same |
| `verifydeprecatedapi-1.29` | 1.29 | Same |

---

### Category 3: Missing Alpha/Beta Cases (3 tasks)

These tasks lack either compliant (alpha) or violating (beta) test cases, making them unsuitable for benchmarking.

| Task ID | Policy | Issue |
|---------|--------|-------|
| `block-nodeport-services` | Block NodePort Services | No compliant cases (alpha=0, beta=1) |
| `disallow-authenticated` | Disallow Authenticated Users | No compliant cases (alpha=0, beta=1) |
| `no-enforcements` | No External Enforcement | No violating cases (alpha=4, beta=0) |

---

### Category 4: Non-Pullable Images (4 tasks)

These tasks use intentionally fake or non-existent container images as part of their test cases. Replacing these images with real ones would defeat the purpose of the policy test.

| Task ID | Policy | Problematic Images |
|---------|--------|-------------------|
| `allowed-reposv2` | Allowed Images v2 | `ubuntumalicious`, `123456789123.dkr.ecr.../postgres`, `ubuntu:20.14` |
| `disallowed-tags` | Disallow Tags | `openpolicyagent/opa-exp:latest`, `opa-exp2:latest`, `init:latest`, `monitor:latest` |
| `repo-must-not-be-k8s-gcr-io` | Block k8s.gcr.io | `k8s.gcr.io/kustomize/kustomize:v3.8.9` (deprecated registry) |
| `required-probes` | Required Probes | `tomcat` image with port mismatches; `readinessProbe: null` causes issues |

**Why not fixable:** The policy being tested checks the image repository or tag. Replacing fake images with real ones would change whether the resource passes or fails the policy.

---

### Category 5: High Resource Requests (8 tasks)

These tasks test resource limit and request policies. The test cases use intentionally high values (1-2Gi memory, up to 4 CPU cores) that won't schedule on small test clusters like kind.

| Task ID | Policy | Resource Values |
|---------|--------|-----------------|
| `container-cpu-requests-memory-limits-and-requests` | CPU Requests + Memory Limits | 1-2Gi memory, 100m-4 CPU |
| `container-limits` | Container Limits | Alpha: 1Gi, Beta: 2Gi memory |
| `container-limits-and-requests` | Limits and Requests | 1-2Gi memory |
| `container-limits-ignore-cpu` | Limits (CPU optional) | 1-2Gi memory |
| `container-requests` | Container Requests | 1-2Gi memory |
| `ephemeral-storage-limit` | Ephemeral Storage Limit | 1Gi memory + storage limits |
| `memory-and-cpu-ratios` | Memory/CPU Ratios | 4 CPU, 2Gi memory |
| `memory-ratio-only` | Memory Ratio | 2Gi memory |

**Why not fixable:** These policies check if resource values are within allowed thresholds:
- **Alpha case:** Resources within limit (e.g., 1Gi memory when limit is 1Gi) → passes
- **Beta case:** Resources exceed limit (e.g., 2Gi memory when limit is 1Gi) → fails

If we cap both to small values (e.g., 128Mi), both would pass the policy, breaking the alpha/beta distinction.

---

### Category 6: PVC/Storage Issues (2 tasks)

These tasks use StatefulSets with PersistentVolumeClaims that reference custom StorageClasses. The PVCs fail to bind because the StorageClasses don't actually provision volumes.

| Task ID | Policy | Issue |
|---------|--------|-------|
| `storageclass` | StorageClass | PVCs reference `resource-inventory-XX` StorageClass that doesn't provision |
| `storageclass-allowlist` | StorageClass Allowlist | Same PVC binding issues |

**Why not fixable:** The policy tests check the `storageClassName` field. Removing PVCs or changing to `emptyDir` would defeat the policy test.

---

### Category 7: Runtime Issues (1 task)

| Task ID | Policy | Issue |
|---------|--------|-------|
| `container-image-must-have-digest` | Image Digests | Init container uses OPA image which runs a server by default (never exits). OPA image doesn't have `sh` shell, making it difficult to override the entrypoint. |

---

## Transformations Applied

For the 17 working tasks, the following transformations are applied during generation:

### 1. Resource Naming
- All resources renamed to `resource-alpha-XX` (compliant) or `resource-beta-XX` (violating)
- Inventory/dependency resources renamed to `resource-inventory-XX`
- Cross-references (HPA targets, PVC claims, RoleBinding subjects) updated accordingly

### 2. Replica Count Fix (`replica-limit` task)
- Original: Alpha=3 replicas, Beta=100 replicas
- Fixed: Alpha=3 replicas, Beta=5 replicas
- Beta still exceeds the policy limit (3), so test semantics preserved

### 3. Init Container Fix (`allowed-repos` task)
- Init containers using `nginx` or `opa` images get `command: [sh, -c, exit 0]` added
- Prevents init containers from running forever (nginx/opa default to running servers)

### 4. Namespace Isolation
- Each task gets its own namespace: `gk-{task-id}`
- Prevents resource conflicts between tasks

---

## Future Improvements

Tasks that could potentially be fixed with more complex transformations:

1. **High resource tasks (8):** Could work on clusters with more resources, or if the benchmark harness supports resource quotas
2. **StorageClass tasks (2):** Could work if the cluster has a default StorageClass that provisions volumes
3. **container-image-must-have-digest:** Could work with a newer OPA image that supports better entrypoint override

---

## Generation Command

```bash
go run ./scripts/gatekeeper-taskgen --output-dir tasks/gatekeeper --verbose
```

Set `GEMINI_API_KEY` environment variable to generate AI-powered task prompts instead of template-based ones.
