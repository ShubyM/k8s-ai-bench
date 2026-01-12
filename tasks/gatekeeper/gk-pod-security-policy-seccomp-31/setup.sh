#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-031 --ignore-not-found --wait=true
kubectl create namespace gk-test-031
kubectl label namespace gk-test-031 pod-security.kubernetes.io/enforce=privileged
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-031
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-031
echo "Waiting for resources to be ready..."
kubectl wait --for=condition=Ready pod/resource-alpha -n gk-test-031 --timeout=120s
kubectl wait --for=condition=Ready pod/resource-beta -n gk-test-031 --timeout=120s
