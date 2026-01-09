#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-004 --ignore-not-found --wait=true
kubectl create namespace gk-test-004
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-004
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-004
echo "Resources deployed. Waiting for readiness..."\nsleep 3
kubectl get all,ingress,networkpolicy,serviceaccount,role,rolebinding,configmap,secret -n gk-test-004
