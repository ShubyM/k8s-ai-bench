#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-031 --ignore-not-found --wait=true
kubectl create namespace gk-test-031
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-031
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-031
echo "Resources deployed. Waiting for readiness..."
sleep 3
kubectl get all -n gk-test-031
