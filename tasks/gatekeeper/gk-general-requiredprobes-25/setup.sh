#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-025 --ignore-not-found --wait=true
kubectl create namespace gk-test-025
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-025
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-025
echo "Waiting for resources to be ready..."\nkubectl wait --for=condition=Ready pod/resource-alpha -n gk-test-025 --timeout=120s\nkubectl wait --for=condition=Ready pod/resource-beta -n gk-test-025 --timeout=120s
kubectl get all,ingress,networkpolicy,serviceaccount,role,rolebinding,configmap,secret -n gk-test-025
