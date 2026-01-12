#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-003 --ignore-not-found --wait=true
kubectl create namespace gk-test-003
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-003
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-003
echo "Waiting for resources to be ready..."
kubectl wait --for=condition=Ready pod/resource-alpha -n gk-test-003 --timeout=120s
kubectl wait --for=condition=Ready pod/resource-beta -n gk-test-003 --timeout=120s
