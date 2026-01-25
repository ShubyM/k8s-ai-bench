package main

import (
	"fmt"
	"strings"
)

// Manifest rewrites are grouped to make intent explicit:
// 1) identity: isolate the task with stable names/namespaces/labels
// 2) references: keep object references consistent with renamed resources
// 3) deployability: safe tweaks that avoid stuck pods or image pull failures
type manifestRewriteContext struct {
	name     string
	ns       string
	nameMap  *nameMap
	taskID   string
	expected string
	isInv    bool
}

func rewriteManifest(doc map[string]interface{}, name, ns string, nameMap *nameMap, taskID, expected string, isInv bool) {
	ctx := manifestRewriteContext{
		name:     name,
		ns:       ns,
		nameMap:  nameMap,
		taskID:   taskID,
		expected: expected,
		isInv:    isInv,
	}

	applyIdentity(doc, ctx)
	rewriteReferences(doc, ctx)
	applyDeployabilityFixes(doc)
}

func applyIdentity(doc map[string]interface{}, ctx manifestRewriteContext) {
	meta := ensureMap(doc, "metadata")
	meta["name"] = ctx.name
	if !isClusterScoped(getStr(doc, "kind")) {
		meta["namespace"] = ctx.ns
	}

	labels := ensureMap(meta, "labels")
	labels["k8s-ai-bench/task"] = ctx.taskID
	labels["k8s-ai-bench/expected"] = ctx.expected
	labels["k8s-ai-bench/inventory"] = fmt.Sprintf("%t", ctx.isInv)
}

func rewriteReferences(doc map[string]interface{}, ctx manifestRewriteContext) {
	kind := getStr(doc, "kind")
	spec, _ := doc["spec"].(map[string]interface{})

	switch kind {
	case "HorizontalPodAutoscaler":
		if ref, ok := spec["scaleTargetRef"].(map[string]interface{}); ok {
			refKind, _ := ref["kind"].(string)
			if n, ok := ref["name"].(string); ok && refKind != "" {
				ref["name"] = ctx.nameMap.mapName(refKind, ctx.ns, n)
			}
		}
	case "PersistentVolumeClaim":
		if sc, ok := spec["storageClassName"].(string); ok {
			spec["storageClassName"] = ctx.nameMap.mapName("StorageClass", "", sc)
		}
	case "StatefulSet":
		rewriteVolumeClaimTemplates(spec, ctx.nameMap)
		rewritePodTemplateRefs(spec, ctx.nameMap, ctx.ns)
	case "Deployment", "ReplicaSet", "DaemonSet":
		rewritePodTemplateRefs(spec, ctx.nameMap, ctx.ns)
		fixReplicaCount(spec, ctx.expected)
	case "Pod":
		rewritePodSpecRefs(spec, ctx.nameMap, ctx.ns)
	case "RoleBinding", "ClusterRoleBinding":
		rewriteRoleBindingRefs(doc, ctx.nameMap, ctx.ns)
	}
}

func rewriteVolumeClaimTemplates(spec map[string]interface{}, nameMap *nameMap) {
	templates, _ := spec["volumeClaimTemplates"].([]interface{})
	for _, t := range templates {
		if claim, ok := t.(map[string]interface{}); ok {
			if cs, ok := claim["spec"].(map[string]interface{}); ok {
				if sc, ok := cs["storageClassName"].(string); ok {
					cs["storageClassName"] = nameMap.mapName("StorageClass", "", sc)
				}
			}
		}
	}
}

func rewritePodTemplateRefs(spec map[string]interface{}, nameMap *nameMap, ns string) {
	if t, ok := spec["template"].(map[string]interface{}); ok {
		if ps, ok := t["spec"].(map[string]interface{}); ok {
			rewritePodSpecRefs(ps, nameMap, ns)
		}
	}
}

func rewritePodSpecRefs(spec map[string]interface{}, nameMap *nameMap, ns string) {
	if sa, ok := spec["serviceAccountName"].(string); ok {
		spec["serviceAccountName"] = nameMap.mapName("ServiceAccount", ns, sa)
	}
	if vols, ok := spec["volumes"].([]interface{}); ok {
		for _, v := range vols {
			if vm, ok := v.(map[string]interface{}); ok {
				if pvc, ok := vm["persistentVolumeClaim"].(map[string]interface{}); ok {
					if cn, ok := pvc["claimName"].(string); ok {
						pvc["claimName"] = nameMap.mapName("PersistentVolumeClaim", ns, cn)
					}
				}
			}
		}
	}
}

func rewriteRoleBindingRefs(doc map[string]interface{}, nameMap *nameMap, ns string) {
	if subjects, ok := doc["subjects"].([]interface{}); ok {
		for _, s := range subjects {
			if sm, ok := s.(map[string]interface{}); ok {
				if sm["kind"] == "ServiceAccount" {
					if n, ok := sm["name"].(string); ok {
						subjectNS, _ := sm["namespace"].(string)
						if subjectNS == "" {
							subjectNS = ns
						}
						sm["name"] = nameMap.mapName("ServiceAccount", subjectNS, n)
					}
					if sm["namespace"] == nil {
						sm["namespace"] = ns
					}
				}
			}
		}
	}
	if ref, ok := doc["roleRef"].(map[string]interface{}); ok {
		if n, ok := ref["name"].(string); ok {
			refKind, _ := ref["kind"].(string)
			if refKind == "" {
				refKind = "Role"
			}
			refNS := ""
			if refKind == "Role" {
				refNS = ns
			}
			ref["name"] = nameMap.mapName(refKind, refNS, n)
		}
	}
}

// Deployment fixes - make manifests deployable without breaking test semantics

// fixReplicaCount caps excessive replica counts while preserving alpha/beta distinction
// Alpha stays at original (e.g., 3), Beta gets capped to 5 (still > limit, so still fails)
func fixReplicaCount(spec map[string]interface{}, expected string) {
	if expected != "beta" {
		return
	}
	const maxBetaReplicas = 5
	if replicas, ok := spec["replicas"].(int); ok && replicas > maxBetaReplicas {
		spec["replicas"] = maxBetaReplicas
	}
	if replicas, ok := spec["replicas"].(float64); ok && int(replicas) > maxBetaReplicas {
		spec["replicas"] = maxBetaReplicas
	}
}

func applyDeployabilityFixes(doc map[string]interface{}) {
	fixInitContainers(doc)
	fixBadImages(doc)
}

// fixInitContainers adds exit command to init containers that would run forever
func fixInitContainers(doc map[string]interface{}) {
	podSpec := podSpecForWorkload(doc)
	if podSpec == nil {
		return
	}

	initContainers, ok := podSpec["initContainers"].([]interface{})
	if !ok {
		return
	}

	for _, c := range initContainers {
		container, ok := c.(map[string]interface{})
		if !ok {
			continue
		}
		// Init containers need to exit for the pod to start.
		// Override command/args with a simple exit for images that run servers.
		image, _ := container["image"].(string)
		if strings.Contains(image, "nginx") {
			container["command"] = []interface{}{"sh", "-c", "exit 0"}
			delete(container, "args")
		} else if strings.Contains(image, "opa") {
			// OPA image doesn't have sh, use built-in eval that exits
			container["command"] = []interface{}{"opa", "eval", "true"}
			delete(container, "args")
		}
	}
}

// fixBadImages replaces images that fail to pull with working alternatives
// Only for images where the replacement doesn't affect the policy test
func fixBadImages(doc map[string]interface{}) {
	podSpec := podSpecForWorkload(doc)
	if podSpec == nil {
		return
	}

	// Only fix specific images where replacement doesn't break test semantics
	replacements := map[string]string{
		"tomcat":      "nginx",      // required-probes: policy checks probes, not image
		"nginx:1.7.9": "nginx:1.25", // old nginx tag doesn't exist
	}

	for _, key := range []string{"containers", "initContainers"} {
		containers, ok := podSpec[key].([]interface{})
		if !ok {
			continue
		}
		for _, c := range containers {
			container, ok := c.(map[string]interface{})
			if !ok {
				continue
			}
			image, ok := container["image"].(string)
			if !ok {
				continue
			}
			for bad, good := range replacements {
				if image == bad {
					container["image"] = good
				}
			}
		}
	}
}

func podSpecForWorkload(doc map[string]interface{}) map[string]interface{} {
	kind := getStr(doc, "kind")
	switch kind {
	case "Pod":
		podSpec, _ := doc["spec"].(map[string]interface{})
		return podSpec
	case "Deployment", "ReplicaSet", "DaemonSet", "StatefulSet":
		if spec, ok := doc["spec"].(map[string]interface{}); ok {
			if template, ok := spec["template"].(map[string]interface{}); ok {
				podSpec, _ := template["spec"].(map[string]interface{})
				return podSpec
			}
		}
	}
	return nil
}
