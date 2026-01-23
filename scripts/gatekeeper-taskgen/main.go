package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"google.golang.org/genai"
	"sigs.k8s.io/yaml"
)

var defaultSkipList = []string{
	// Name-sensitive or deprecated policies
	"block-endpoint-default-role",
	"noupdateserviceaccount",
	"verifydeprecatedapi",
	// Tasks with non-deployable resources (fake images, deprecated registries)
	// These can't be fixed without breaking alpha/beta distinction
	"allowed-reposv2",
	"disallowed-tags",
	"repo-must-not-be-k8s-gcr-io",
	// Tasks with high resource requests that won't schedule on small clusters
	// Capping resources would make both alpha and beta pass
	"container-cpu-requests-memory-limits-and-requests",
	"container-limits",
	"container-limits-and-requests",
	"container-limits-ignore-cpu",
	"container-requests",
	"ephemeral-storage-limit",
	"memory-and-cpu-ratios",
	"memory-ratio-only",
	// Tasks with PVC issues
	"storageclass",
	"storageclass-allowlist",
	// Tasks with complex runtime issues that need manual fixes
	"container-image-must-have-digest", // OPA init container
	"required-probes",                  // readiness probe port mismatches
}

func main() {
	cfg := Config{}
	flag.StringVar(&cfg.LibraryRoot, "library-root", ".gatekeeper-library/library/general", "Path to gatekeeper-library general directory")
	flag.StringVar(&cfg.OutputDir, "output-dir", "tasks/gatekeeper", "Directory to write tasks")
	flag.Var(&stringSliceFlag{&cfg.SkipList}, "skip", "Patterns to skip (can be repeated)")
	flag.BoolVar(&cfg.Verbose, "verbose", false, "Enable verbose logging")
	flag.Parse()

	cfg.SkipList = append(cfg.SkipList, defaultSkipList...)

	// Initialize Gemini client if API key is available
	if apiKey := os.Getenv("GEMINI_API_KEY"); apiKey != "" {
		ctx := context.Background()
		client, err := genai.NewClient(ctx, &genai.ClientConfig{
			APIKey:  apiKey,
			Backend: genai.BackendGeminiAPI,
		})
		if err != nil {
			fmt.Fprintf(os.Stderr, "Warning: Failed to initialize Gemini client: %v\n", err)
		} else {
			cfg.GeminiClient = client
			fmt.Println("Gemini client initialized - will generate prompts using AI")
		}
	} else {
		fmt.Println("GEMINI_API_KEY not set - using template-based prompts")
	}

	if err := run(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "%v\n", err)
		os.Exit(1)
	}
}

func run(cfg Config) error {
	taskMap, err := ParseSuites(cfg.LibraryRoot)
	if err != nil {
		return err
	}
	if len(taskMap) == 0 {
		return fmt.Errorf("no suite.yaml files found under %s", cfg.LibraryRoot)
	}

	os.MkdirAll(cfg.OutputDir, 0755)

	var generated, skipped int
	for _, id := range sortedKeys(taskMap) {
		task := taskMap[id]
		if skip, reason := shouldSkip(cfg, task); skip {
			fmt.Printf("Skipped %s: %s\n", id, reason)
			skipped++
			continue
		}
		if err := generateTask(cfg, task); err != nil {
			fmt.Printf("Skipped %s: %v\n", id, err)
			skipped++
		} else {
			if cfg.Verbose {
				fmt.Printf("Generated task %s\n", id)
			}
			generated++
		}
	}
	fmt.Printf("Generated tasks: %d (skipped %d)\n", generated, skipped)
	return nil
}

func shouldSkip(cfg Config, task TaskMetadata) (bool, string) {
	for _, skip := range cfg.SkipList {
		if skip == task.TestName || skip == task.SuiteName || strings.Contains(task.TestName, skip) {
			return true, "skip list"
		}
	}
	alpha, beta := 0, 0
	for _, c := range task.Cases {
		if c.Expected == "alpha" {
			alpha++
		} else {
			beta++
		}
	}
	if alpha == 0 || beta == 0 {
		return true, fmt.Sprintf("missing alpha or beta cases (alpha=%d beta=%d)", alpha, beta)
	}
	return false, ""
}

func generateTask(cfg Config, task TaskMetadata) error {
	outDir := filepath.Join(cfg.OutputDir, task.TaskID)

	// Generate manifests and collect prompt context
	artifacts, promptCtx, err := GenerateManifests(task, outDir)
	if err != nil {
		return err
	}

	// Generate prompt
	prompt := BuildPrompt(cfg, promptCtx)

	// Write task.yaml
	taskYAML := fmt.Sprintf(`script:
- prompt: |
%s
expect:
- contains: "VIOLATING: resource-beta-\\d+"
- notContains: "VIOLATING: resource-alpha-\\d+"
isolation: cluster
timeout: 5m
`, indent(prompt, "    "))
	os.WriteFile(filepath.Join(outDir, "task.yaml"), []byte(taskYAML), 0644)

	// Write suite.yaml
	writeSuite(outDir, task, artifacts)

	// Rewrite constraint
	rewriteConstraint(task.ConstraintPath, filepath.Join(outDir, "constraint.yaml"), "gk-"+task.TaskID)
	copyFile(task.TemplatePath, filepath.Join(outDir, "template.yaml"))

	// Write setup/cleanup scripts
	writeScripts(outDir, task.TaskID, artifacts)

	return nil
}

func rewriteConstraint(src, dst, ns string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	var doc map[string]interface{}
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return err
	}

	// Update namespaces in match criteria
	if spec, ok := doc["spec"].(map[string]interface{}); ok {
		if match, ok := spec["match"].(map[string]interface{}); ok {
			if namespaces, ok := match["namespaces"].([]interface{}); ok {
				fmt.Printf("Rewriting constraint %s: namespaces %v -> %s\n", dst, namespaces, ns)
				// if namespaces filter is present, replace it with our task namespace
				// otherwise checking specific namespace is not required (cluster-wide)
				if len(namespaces) > 0 {
					match["namespaces"] = []string{ns}
				}
			} else {
				fmt.Printf("Constraint %s match['namespaces'] not found or valid type\n", dst)
			}
		} else {
			fmt.Printf("Constraint %s match not found\n", dst)
		}
	} else {
		fmt.Printf("Constraint %s spec not found\n", dst)
	}

	out, err := yaml.Marshal(doc)
	if err != nil {
		return err
	}
	return os.WriteFile(dst, out, 0644)
}

func writeSuite(outDir string, task TaskMetadata, artifacts TaskArtifacts) {
	var cases []map[string]interface{}
	for _, c := range task.Cases {
		for _, cf := range artifacts.CaseFiles[c.Name] {
			violations := "no"
			if c.Expected == "beta" {
				violations = "yes"
			}
			cases = append(cases, map[string]interface{}{
				"name":       c.Name,
				"object":     cf,
				"inventory":  artifacts.InventoryFiles[c.Name],
				"assertions": []map[string]interface{}{{"violations": violations}},
			})
		}
	}
	suite := map[string]interface{}{
		"kind":       "Suite",
		"apiVersion": "test.gatekeeper.sh/v1alpha1",
		"metadata":   map[string]interface{}{"name": task.TaskID},
		"tests": []map[string]interface{}{{
			"name":       task.TestName,
			"template":   "template.yaml",
			"constraint": "constraint.yaml",
			"cases":      cases,
		}},
	}
	data, _ := yaml.Marshal(suite)
	os.WriteFile(filepath.Join(outDir, "suite.yaml"), data, 0644)
}

func writeScripts(outDir, taskID string, artifacts TaskArtifacts) {
	ns := "gk-" + taskID
	var nsSetup, nsCleanup strings.Builder
	for _, n := range artifacts.Namespaces {
		if n == "default" || n == "kube-system" {
			continue
		}
		fmt.Fprintf(&nsSetup, "kubectl delete namespace %q --ignore-not-found\n", n)
		fmt.Fprintf(&nsSetup, "kubectl create namespace %q\n", n)
		fmt.Fprintf(&nsSetup, "kubectl wait --for=jsonpath='{.status.phase}'=Active --timeout=120s namespace %q\n", n)
		fmt.Fprintf(&nsCleanup, "kubectl delete namespace %q --ignore-not-found\n", n)
	}

	var resCleanup strings.Builder
	for _, r := range artifacts.ClusterResources {
		fmt.Fprintf(&resCleanup, "kubectl delete %s %q --ignore-not-found\n", r.Kind, r.Name)
	}

	setup := fmt.Sprintf(`#!/usr/bin/env bash
set -euo pipefail
TASK_NAMESPACE=%q
%s
ARTIFACTS_DIR="$(dirname "$0")/artifacts"
# Apply inventory first (dependencies), then alpha/beta resources
kubectl apply -f "$ARTIFACTS_DIR"/inventory-*.yaml
kubectl apply -f "$ARTIFACTS_DIR"/alpha-*.yaml
kubectl apply -f "$ARTIFACTS_DIR"/beta-*.yaml
`, ns, strings.TrimSpace(nsSetup.String()))

	cleanup := fmt.Sprintf("#!/usr/bin/env bash\nset -euo pipefail\n%s%s", nsCleanup.String(), resCleanup.String())

	os.WriteFile(filepath.Join(outDir, "setup.sh"), []byte(setup), 0755)
	os.WriteFile(filepath.Join(outDir, "cleanup.sh"), []byte(cleanup), 0755)
}

// Helpers

func copyFile(src, dst string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return os.WriteFile(dst, data, 0644)
}

func indent(text, prefix string) string {
	lines := strings.Split(text, "\n")
	for i := range lines {
		lines[i] = prefix + lines[i]
	}
	return strings.Join(lines, "\n")
}

func sortedKeys[T any](m map[string]T) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// stringSliceFlag allows repeated -skip flags
type stringSliceFlag struct {
	values *[]string
}

func (f *stringSliceFlag) String() string {
	if f.values == nil {
		return ""
	}
	return strings.Join(*f.values, ",")
}

func (f *stringSliceFlag) Set(value string) error {
	*f.values = append(*f.values, value)
	return nil
}
