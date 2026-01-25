#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob
TASK_NAMESPACE="gk-must-have-key"
kubectl delete namespace "gk-must-have-key" --ignore-not-found
kubectl create namespace "gk-must-have-key"
kubectl wait --for=jsonpath='{.status.phase}'=Active --timeout=120s namespace "gk-must-have-key"
ARTIFACTS_DIR="$(dirname "$0")/artifacts"
# Apply inventory first (dependencies), then alpha/beta resources
for file in "$ARTIFACTS_DIR"/inventory-*.yaml; do
  kubectl apply -f "$file"
done
for file in "$ARTIFACTS_DIR"/alpha-*.yaml; do
  kubectl apply -f "$file"
done
for file in "$ARTIFACTS_DIR"/beta-*.yaml; do
  kubectl apply -f "$file"
done
for file in "$ARTIFACTS_DIR"/inventory-*.yaml "$ARTIFACTS_DIR"/alpha-*.yaml "$ARTIFACTS_DIR"/beta-*.yaml; do
  kind="$(kubectl get -f "$file" -o jsonpath='{.kind}')"
  case "$kind" in
    Deployment|StatefulSet|DaemonSet)
      kubectl rollout status -f "$file" --timeout=120s
      ;;
    ReplicaSet)
      kubectl wait --for=condition=Available --timeout=120s -f "$file"
      ;;
    Pod)
      kubectl wait --for=condition=Ready --timeout=120s -f "$file"
      ;;
    Job)
      kubectl wait --for=condition=Complete --timeout=120s -f "$file"
      ;;
  esac
done
# Show deployed resources for debugging
kubectl get all -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get ingress -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get hpa -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get pdb -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get clusterrolebinding 2>/dev/null | head -n 20 || true
