#!/usr/bin/env bash
set -euo pipefail
kubectl delete namespace "gk-must-have-owner" --ignore-not-found
kubectl delete Namespace "resource-alpha-01" --ignore-not-found
kubectl delete Namespace "resource-beta-01" --ignore-not-found
kubectl delete Namespace "resource-beta-02" --ignore-not-found
