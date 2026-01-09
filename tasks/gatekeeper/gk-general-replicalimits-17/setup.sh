#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-017 --ignore-not-found --wait=true
kubectl create namespace gk-test-017
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-017
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-017
echo "Waiting for resources to be ready..."
kubectl wait --for=condition=Available deployment/resource-alpha -n gk-test-017 --timeout=120s
kubectl wait --for=condition=Available deployment/resource-beta -n gk-test-017 --timeout=120s
kubectl get all,ingress,networkpolicy,serviceaccount,role,rolebinding,configmap,secret -n gk-test-017
