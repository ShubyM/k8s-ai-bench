#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-005 --ignore-not-found --wait=true
kubectl create namespace gk-test-005
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-005
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-005
echo "Resources deployed. Waiting for readiness..."
sleep 3
kubectl get all,ingress,networkpolicy,serviceaccount,role,rolebinding,configmap,secret -n gk-test-005
