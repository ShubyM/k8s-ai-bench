package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"sigs.k8s.io/yaml"
)

// GenerateManifests processes task cases and generates artifact files
func GenerateManifests(task TaskMetadata, outDir string) (TaskArtifacts, PromptContext, error) {
	os.MkdirAll(filepath.Join(outDir, "artifacts"), 0755)

	defaultNS := "gk-" + task.TaskID
	artifacts := TaskArtifacts{
		CaseFiles:      map[string][]string{},
		InventoryFiles: map[string][]string{},
	}
	alphaIdx, betaIdx, invIdx := 1, 1, 1
	nsSet := map[string]bool{defaultNS: true}

	var templateTitle, templateDesc, templateYAML, constraintYAML string
	var alphaExamples, betaExamples []string

	// Read template metadata
	if docs, _ := readYAMLDocs(task.TemplatePath); len(docs) > 0 {
		if data, err := os.ReadFile(task.TemplatePath); err == nil {
			templateYAML = string(data)
		}
		if meta, ok := docs[0]["metadata"].(map[string]interface{}); ok {
			if ann, ok := meta["annotations"].(map[string]interface{}); ok {
				if v, ok := ann["metadata.gatekeeper.sh/title"].(string); ok {
					templateTitle = v
				}
				if v, ok := ann["description"].(string); ok {
					templateDesc = strings.TrimSpace(v)
				}
			}
		}
	}

	// Read constraint
	if data, err := os.ReadFile(task.ConstraintPath); err == nil {
		// Mirror the on-disk namespace rewrite so the prompt reflects the isolated namespace.
		constraintYAML = rewriteConstraintForPrompt(data, defaultNS)
	}

	for _, c := range task.Cases {
		caseDocs, _ := readYAMLDocs(c.ObjectPath)
		if len(caseDocs) == 0 || isAdmissionReview(caseDocs[0]) || !isDeployable(caseDocs[0]) {
			continue
		}

		// Load inventory docs
		var invDocs []map[string]interface{}
		for _, inv := range c.InventoryPaths {
			if docs, _ := readYAMLDocs(inv); len(docs) > 0 && !isAdmissionReview(docs[0]) {
				invDocs = append(invDocs, docs[0])
			}
		}

		// Build name map and collect docs
		nameMap := newNameMap()
		nameAllocator := newNameAllocator()
		type docInfo struct {
			doc     map[string]interface{}
			newName string
			isInv   bool
		}
		var allDocs []docInfo

		for _, doc := range invDocs {
			kind := getStr(doc, "kind")
			namespace := ""
			if !isClusterScoped(kind) {
				namespace = defaultNS
			}
			origName := getStr(doc, "metadata", "name")
			baseName := origName
			if baseName == "" {
				baseName = fmt.Sprintf("resource-inventory-%02d", invIdx)
			}
			name, _ := nameAllocator.allocate(kind, namespace, baseName)
			nameMap.set(kind, namespace, origName, name)
			invIdx++
			allDocs = append(allDocs, docInfo{doc, name, true})
		}

		for _, doc := range caseDocs[:1] {
			kind := getStr(doc, "kind")
			namespace := ""
			if !isClusterScoped(kind) {
				namespace = defaultNS
			}
			origName := getStr(doc, "metadata", "name")
			var name string
			if c.Expected == "alpha" {
				name = fmt.Sprintf("resource-alpha-%02d", alphaIdx)
				alphaIdx++
			} else {
				name = fmt.Sprintf("resource-beta-%02d", betaIdx)
				betaIdx++
			}
			baseName := origName
			if baseName == "" {
				baseName = name
			}
			name, _ = nameAllocator.allocate(kind, namespace, baseName)
			nameMap.set(kind, namespace, origName, name)
			allDocs = append(allDocs, docInfo{doc, name, false})
		}

		// Rewrite and save
		invFileIdx, caseFileIdx := 1, 1
		for _, d := range allDocs {
			rewriteManifest(d.doc, d.newName, defaultNS, nameMap, task.TaskID, c.Expected, d.isInv)
			kind := getStr(d.doc, "kind")
			ns := getStr(d.doc, "metadata", "namespace")
			if ns != "" {
				nsSet[ns] = true
			}

			var fileName string
			if d.isInv {
				fileName = fmt.Sprintf("inventory-%02d.yaml", invFileIdx)
			} else {
				fileName = fmt.Sprintf("%s-%02d.yaml", c.Expected, caseFileIdx)
			}
			relPath := "artifacts/" + fileName

			data, _ := yaml.Marshal(d.doc)
			os.WriteFile(filepath.Join(outDir, relPath), data, 0644)

			if !d.isInv {
				if c.Expected == "alpha" && len(alphaExamples) < 2 {
					alphaExamples = append(alphaExamples, string(data))
				} else if c.Expected == "beta" && len(betaExamples) < 2 {
					betaExamples = append(betaExamples, string(data))
				}
			}

			artifacts.Manifests = append(artifacts.Manifests, TaskManifest{
				Path:          filepath.Join(outDir, relPath),
				RelPath:       relPath,
				Doc:           d.doc,
				Inventory:     d.isInv,
				CaseName:      c.Name,
				Expected:      c.Expected,
				Kind:          kind,
				Name:          d.newName,
				Namespace:     ns,
				ClusterScoped: isClusterScoped(kind),
			})

			if d.isInv {
				invFileIdx++
				artifacts.InventoryFiles[c.Name] = append(artifacts.InventoryFiles[c.Name], relPath)
			} else {
				caseFileIdx++
				artifacts.CaseFiles[c.Name] = append(artifacts.CaseFiles[c.Name], relPath)
			}

			if isClusterScoped(kind) {
				artifacts.ClusterResources = append(artifacts.ClusterResources, ClusterResource{kind, d.newName})
			}
		}
	}

	artifacts.Namespaces = sortedKeys(nsSet)

	namespacedKindsSet := map[string]bool{}
	clusterKindsSet := map[string]bool{}
	for _, manifest := range artifacts.Manifests {
		if manifest.Inventory {
			continue
		}
		if manifest.ClusterScoped {
			clusterKindsSet[manifest.Kind] = true
		} else {
			namespacedKindsSet[manifest.Kind] = true
		}
	}

	promptCtx := PromptContext{
		TaskID:          task.TaskID,
		Title:           templateTitle,
		Description:     templateDesc,
		TemplateYAML:    templateYAML,
		ConstraintYAML:  constraintYAML,
		AlphaExamples:   alphaExamples,
		BetaExamples:    betaExamples,
		Namespace:       defaultNS,
		NamespacedKinds: sortedKeys(namespacedKindsSet),
		ClusterKinds:    sortedKeys(clusterKindsSet),
	}

	return artifacts, promptCtx, nil
}

func isAdmissionReview(doc map[string]interface{}) bool {
	return getStr(doc, "kind") == "AdmissionReview"
}

func isDeployable(doc map[string]interface{}) bool {
	if getStr(doc, "kind") != "Pod" {
		return true
	}
	spec, ok := doc["spec"].(map[string]interface{})
	if !ok {
		return true
	}
	if _, hasEphemeral := spec["ephemeralContainers"]; hasEphemeral {
		return false
	}
	names := map[string]bool{}
	for _, key := range []string{"containers", "initContainers"} {
		if containers, ok := spec[key].([]interface{}); ok {
			for _, c := range containers {
				if cm, ok := c.(map[string]interface{}); ok {
					if name, ok := cm["name"].(string); ok {
						if names[name] {
							return false
						}
						names[name] = true
					}
				}
			}
		}
	}
	return true
}

func isClusterScoped(kind string) bool {
	return kind == "Namespace" || kind == "ClusterRole" || kind == "ClusterRoleBinding" || kind == "StorageClass"
}

// YAML helpers

func readYAMLDocs(path string) ([]map[string]interface{}, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var results []map[string]interface{}
	for _, doc := range bytes.Split(data, []byte("---")) {
		if len(bytes.TrimSpace(doc)) == 0 {
			continue
		}
		var obj map[string]interface{}
		if yaml.Unmarshal(doc, &obj) == nil {
			results = append(results, obj)
		}
	}
	return results, nil
}

func getStr(m map[string]interface{}, keys ...string) string {
	for i, k := range keys {
		if i == len(keys)-1 {
			if v, ok := m[k].(string); ok {
				return v
			}
			return ""
		}
		if next, ok := m[k].(map[string]interface{}); ok {
			m = next
		} else {
			return ""
		}
	}
	return ""
}

func ensureMap(parent map[string]interface{}, key string) map[string]interface{} {
	if v, ok := parent[key].(map[string]interface{}); ok {
		return v
	}
	m := map[string]interface{}{}
	parent[key] = m
	return m
}
