// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"maps"
	"slices"
	"strings"
)

func indent(text, prefix string) string {
	lines := strings.Split(text, "\n")
	for i := range lines {
		lines[i] = prefix + lines[i]
	}
	return strings.Join(lines, "\n")
}

func sortedKeys[T any](m map[string]T) []string {
	keys := slices.Collect(maps.Keys(m))
	slices.Sort(keys)
	return keys
}

func getStr(m map[string]any, keys ...string) string {
	for i, k := range keys {
		if i == len(keys)-1 {
			if v, ok := m[k].(string); ok {
				return v
			}
			return ""
		}
		if next, ok := m[k].(map[string]any); ok {
			m = next
		} else {
			return ""
		}
	}
	return ""
}

func ensureMap(parent map[string]any, key string) map[string]any {
	if v, ok := parent[key].(map[string]any); ok {
		return v
	}
	m := map[string]any{}
	parent[key] = m
	return m
}

var clusterScopedKinds = []string{
	"APIService",
	"ClusterRole",
	"ClusterRoleBinding",
	"CustomResourceDefinition",
	"CSIDriver",
	"CSINode",
	"FlowSchema",
	"MutatingWebhookConfiguration",
	"Namespace",
	"Node",
	"PersistentVolume",
	"PodSecurityPolicy",
	"PriorityClass",
	"RuntimeClass",
	"StorageClass",
	"ValidatingWebhookConfiguration",
	"VolumeAttachment",
}

func isClusterScopedKind(kind string) bool {
	return slices.Contains(clusterScopedKinds, kind)
}
