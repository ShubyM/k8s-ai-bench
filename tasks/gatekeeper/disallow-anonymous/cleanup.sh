#!/usr/bin/env bash
set -euo pipefail
kubectl delete namespace "gk-disallow-anonymous" --ignore-not-found
kubectl delete ClusterRoleBinding "resource-alpha-01" --ignore-not-found
kubectl delete ClusterRoleBinding "resource-beta-01" --ignore-not-found
