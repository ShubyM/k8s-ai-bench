#!/usr/bin/env bash
set -e
kubectl delete namespace gk-test-010 --ignore-not-found --wait=true
kubectl create namespace gk-test-010
sleep 2
kubectl apply -f artifacts/resource-alpha.yaml -n gk-test-010
kubectl apply -f artifacts/resource-beta.yaml -n gk-test-010
sleep 3
echo "Resources deployed. Waiting for stability..."
sleep 5
