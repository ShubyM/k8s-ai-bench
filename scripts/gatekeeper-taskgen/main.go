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
	flag.BoolVar(&cfg.Repair, "repair", false, "Repair beta manifests via Gemini after generation")
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
		fmt.Fprintln(os.Stderr, "GEMINI_API_KEY not set - Gemini is required for prompt generation")
		os.Exit(1)
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
	var repairResults []RepairResult
	for _, id := range sortedKeys(taskMap) {
		task := taskMap[id]
		if skip, reason := shouldSkip(cfg, task); skip {
			fmt.Printf("Skipped %s: %s\n", id, reason)
			skipped++
			continue
		}
		repairResult, err := generateTask(cfg, task)
		if err != nil {
			fmt.Printf("Skipped %s: %v\n", id, err)
			skipped++
			// Still collect the repair result for the report even if it errored
			if repairResult != nil {
				repairResults = append(repairResults, *repairResult)
			}
		} else {
			if cfg.Verbose {
				fmt.Printf("Generated task %s\n", id)
			}
			generated++
			if repairResult != nil {
				repairResults = append(repairResults, *repairResult)
			}
		}
	}
	fmt.Printf("Generated tasks: %d (skipped %d)\n", generated, skipped)

	// Write repair report if repairs were attempted
	if cfg.Repair && len(repairResults) > 0 {
		if err := writeRepairReport(cfg.OutputDir, repairResults); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: failed to write repair report: %v\n", err)
		} else {
			fmt.Printf("Repair report written to %s/repair-report.md\n", cfg.OutputDir)
		}
	}

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

func generateTask(cfg Config, task TaskMetadata) (*RepairResult, error) {
	outDir := filepath.Join(cfg.OutputDir, task.TaskID)

	// Generate manifests and collect prompt context
	artifacts, promptCtx, err := GenerateManifests(task, outDir)
	if err != nil {
		return nil, err
	}

	// Generate prompt
	prompt, err := BuildPrompt(cfg, promptCtx)
	if err != nil {
		return nil, err
	}

	// Write task.yaml
	taskYAML := fmt.Sprintf(`script:
- prompt: |
%s
setup: setup.sh
cleanup: cleanup.sh
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

	if cfg.Repair {
		result := repairTask(cfg, outDir, task.TaskID)
		if result.Status == "error" {
			return &result, fmt.Errorf("repair %s: %s", task.TaskID, result.Error)
		}
		return &result, nil
	}

	return nil, nil
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
shopt -s nullglob
TASK_NAMESPACE=%q
%s
ARTIFACTS_DIR="$(dirname "$0")/artifacts"
# Apply inventory first (dependencies), then alpha/beta resources
for file in "$ARTIFACTS_DIR"/inventory-*.yaml; do
  kubectl apply -f "$file"
done
for file in "$ARTIFACTS_DIR"/alpha-*.yaml; do
  kubectl apply -f "$file"
done
for file in "$ARTIFACTS_DIR"/beta-*.yaml; do
  kubectl apply -f "$file"
done
for file in "$ARTIFACTS_DIR"/inventory-*.yaml "$ARTIFACTS_DIR"/alpha-*.yaml "$ARTIFACTS_DIR"/beta-*.yaml; do
  kind="$(kubectl get -f "$file" -o jsonpath='{.kind}')"
  case "$kind" in
    Deployment|StatefulSet|DaemonSet)
      kubectl rollout status -f "$file" --timeout=120s
      ;;
    ReplicaSet)
      kubectl wait --for=condition=Available --timeout=120s -f "$file"
      ;;
    Pod)
      kubectl wait --for=condition=Ready --timeout=120s -f "$file"
      ;;
    Job)
      kubectl wait --for=condition=Complete --timeout=120s -f "$file"
      ;;
  esac
done
# Show deployed resources for debugging
kubectl get all -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get ingress -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get hpa -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get pdb -n "$TASK_NAMESPACE" 2>/dev/null || true
kubectl get clusterrolebinding 2>/dev/null | head -n 20 || true
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
