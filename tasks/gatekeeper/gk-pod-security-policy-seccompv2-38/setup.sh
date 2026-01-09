#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-038 --ignore-not-found --wait=true
kubectl create namespace gk-test-038
kubectl label namespace gk-test-038 pod-security.kubernetes.io/enforce=privileged
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-038
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-038
echo "Waiting for resources to be ready..."
kubectl wait --for=condition=Ready pod/resource-alpha -n gk-test-038 --timeout=120s
kubectl wait --for=condition=Ready pod/resource-beta -n gk-test-038 --timeout=120s
kubectl get all,ingress,networkpolicy,serviceaccount,role,rolebinding,configmap,secret -n gk-test-038
