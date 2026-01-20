// Copyright 2025 Google LLC
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
	"bytes"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"sigs.k8s.io/yaml"
)

// ValidateConfig holds configuration for the validate command.
type ValidateConfig struct {
	TasksDir    string
	LibraryDir  string
	TaskPattern string
	Parallel    int
	Verbose     bool
}

// ValidationResult holds the result of validating a single task.
type ValidationResult struct {
	TaskName        string
	Passed          bool
	AlphaViolations []string
	BetaViolations  []string
	Error           string
}

func main() {
	config := ValidateConfig{
		TasksDir:   "./tasks",
		LibraryDir: "./.gatekeeper-library",
		Parallel:   8,
	}

	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: %s [options]\n\n", os.Args[0])
		fmt.Fprintf(os.Stderr, "Validate Gatekeeper tasks using gator CLI.\n\n")
		fmt.Fprintf(os.Stderr, "Options:\n")
		flag.PrintDefaults()
	}

	flag.StringVar(&config.TasksDir, "tasks-dir", config.TasksDir, "Directory containing evaluation tasks")
	flag.StringVar(&config.LibraryDir, "library-dir", config.LibraryDir, "Directory containing gatekeeper-library")
	flag.StringVar(&config.TaskPattern, "task", config.TaskPattern, "Pattern to filter tasks (e.g. 'allowedrepos')")
	flag.IntVar(&config.Parallel, "parallel", config.Parallel, "Number of parallel validations (use 1 for sequential)")
	flag.BoolVar(&config.Verbose, "verbose", config.Verbose, "Enable verbose output")
	flag.Parse()

	if err := run(config); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}

func run(config ValidateConfig) error {
	// Check gator is available
	if err := checkGatorInstalled(); err != nil {
		return err
	}

	// Validate library exists
	if _, err := os.Stat(config.LibraryDir); os.IsNotExist(err) {
		return fmt.Errorf("gatekeeper library not found at %s\nRun generate.py first to clone the library", config.LibraryDir)
	}

	// Find tasks
	tasks, err := findGatekeeperTasks(config.TasksDir)
	if err != nil {
		return fmt.Errorf("finding tasks: %w", err)
	}

	if len(tasks) == 0 {
		return fmt.Errorf("no gatekeeper tasks found in %s", config.TasksDir)
	}

	// Filter tasks if pattern provided
	if config.TaskPattern != "" {
		var filtered []string
		for _, t := range tasks {
			if strings.Contains(filepath.Base(t), config.TaskPattern) {
				filtered = append(filtered, t)
			}
		}
		if len(filtered) == 0 {
			return fmt.Errorf("no tasks matching pattern %q found", config.TaskPattern)
		}
		tasks = filtered
	}

	fmt.Printf("Found %d tasks to validate\n", len(tasks))

	// Run validation
	var results []ValidationResult
	if config.Parallel > 1 {
		results = validateTasksParallel(tasks, config)
	} else {
		results = validateTasksSequential(tasks, config)
	}

	// Print summary
	printValidationSummary(results)

	// Return error if any failed
	for _, r := range results {
		if !r.Passed {
			return fmt.Errorf("validation failed: %d/%d tasks passed", countPassed(results), len(results))
		}
	}

	return nil
}

func checkGatorInstalled() error {
	cmd := exec.Command("gator", "version")
	output, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("gator CLI not found. Install it from: https://open-policy-agent.github.io/gatekeeper/website/docs/gator/")
	}
	fmt.Printf("Using gator: %s\n", strings.TrimSpace(string(output)))
	return nil
}

func findGatekeeperTasks(tasksDir string) ([]string, error) {
	gkDir := filepath.Join(tasksDir, "gatekeeper")
	if _, err := os.Stat(gkDir); os.IsNotExist(err) {
		return nil, nil
	}

	entries, err := os.ReadDir(gkDir)
	if err != nil {
		return nil, err
	}

	var tasks []string
	for _, entry := range entries {
		if !entry.IsDir() || !strings.HasPrefix(entry.Name(), "gk-") {
			continue
		}
		taskYAML := filepath.Join(gkDir, entry.Name(), "task.yaml")
		if _, err := os.Stat(taskYAML); err == nil {
			tasks = append(tasks, filepath.Join(gkDir, entry.Name()))
		}
	}

	sort.Strings(tasks)
	return tasks, nil
}

func findPolicyFiles(taskDir, libraryDir string) (templatePath, constraintPath string, err error) {
	// Parse task name: gk-general-<policyname>-<index>
	taskName := filepath.Base(taskDir)
	parts := strings.Split(taskName, "-")
	if len(parts) < 4 {
		return "", "", fmt.Errorf("invalid task name format: %s", taskName)
	}

	policyName := strings.Join(parts[2:len(parts)-1], "-")

	// Look in library
	policyDir := filepath.Join(libraryDir, "library", "general", policyName)
	if _, err := os.Stat(policyDir); os.IsNotExist(err) {
		// Try case-insensitive match
		generalDir := filepath.Join(libraryDir, "library", "general")
		entries, err := os.ReadDir(generalDir)
		if err != nil {
			return "", "", err
		}
		for _, entry := range entries {
			if strings.EqualFold(
				strings.ReplaceAll(entry.Name(), "-", ""),
				strings.ReplaceAll(policyName, "-", ""),
			) {
				policyDir = filepath.Join(generalDir, entry.Name())
				break
			}
		}
	}

	if _, err := os.Stat(policyDir); os.IsNotExist(err) {
		return "", "", fmt.Errorf("policy directory not found for %s", policyName)
	}

	// Find template
	templatePath = filepath.Join(policyDir, "template.yaml")
	if _, err := os.Stat(templatePath); os.IsNotExist(err) {
		return "", "", fmt.Errorf("template.yaml not found in %s", policyDir)
	}

	// Find first sample's constraint
	samplesDir := filepath.Join(policyDir, "samples")
	if entries, err := os.ReadDir(samplesDir); err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				cp := filepath.Join(samplesDir, entry.Name(), "constraint.yaml")
				if _, err := os.Stat(cp); err == nil {
					constraintPath = cp
					break
				}
			}
		}
	}

	if constraintPath == "" {
		return "", "", fmt.Errorf("no constraint.yaml found in samples for %s", policyName)
	}

	return templatePath, constraintPath, nil
}

// patchConstraintYAML removes namespace restrictions from a constraint YAML.
func patchConstraintYAML(data []byte) ([]byte, error) {
	var obj map[string]interface{}
	if err := yaml.Unmarshal(data, &obj); err != nil {
		return nil, err
	}

	// Navigate to spec.match and remove namespace restrictions
	if spec, ok := obj["spec"].(map[string]interface{}); ok {
		if match, ok := spec["match"].(map[string]interface{}); ok {
			delete(match, "namespaces")
			delete(match, "excludedNamespaces")
		}
	}

	return yaml.Marshal(obj)
}

// addNamespaceToResourceYAML adds a namespace to resources that don't have one.
func addNamespaceToResourceYAML(data []byte, namespace string) ([]byte, error) {
	// Split by document separator
	docs := bytes.Split(data, []byte("\n---"))
	var results [][]byte

	for _, doc := range docs {
		doc = bytes.TrimSpace(doc)
		if len(doc) == 0 {
			continue
		}

		var obj map[string]interface{}
		if err := yaml.Unmarshal(doc, &obj); err != nil {
			// If we can't parse it, just pass it through
			results = append(results, doc)
			continue
		}

		// Add namespace if not present
		if meta, ok := obj["metadata"].(map[string]interface{}); ok {
			if _, hasNS := meta["namespace"]; !hasNS {
				meta["namespace"] = namespace
			}
		}

		patched, err := yaml.Marshal(obj)
		if err != nil {
			results = append(results, doc)
			continue
		}
		results = append(results, patched)
	}

	return bytes.Join(results, []byte("\n---\n")), nil
}

// runGatorTest runs gator test and returns violation messages.
func runGatorTest(templatePath, constraintData, resourceData []byte) ([]string, error) {
	// Create temp directory for files
	tmpDir, err := os.MkdirTemp("", "gator-test-*")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(tmpDir)

	// Write constraint
	constraintPath := filepath.Join(tmpDir, "constraint.yaml")
	if err := os.WriteFile(constraintPath, constraintData, 0644); err != nil {
		return nil, err
	}

	// Write resources
	resourcePath := filepath.Join(tmpDir, "resources.yaml")
	if err := os.WriteFile(resourcePath, resourceData, 0644); err != nil {
		return nil, err
	}

	// Run gator test
	cmd := exec.Command("gator", "test",
		"-f", string(templatePath),
		"-f", constraintPath,
		"-f", resourcePath,
	)

	output, err := cmd.CombinedOutput()

	// Parse output for violations
	var violations []string
	lines := strings.Split(string(output), "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		// Ignore warnings
		if strings.Contains(line, "WARNING") {
			continue
		}
		violations = append(violations, line)
	}

	// gator returns non-zero if there are violations (or errors)
	// We consider it has violations if exit code != 0 OR we found output lines
	if err != nil || len(violations) > 0 {
		return violations, nil
	}

	return nil, nil
}

func validateTask(taskDir string, config ValidateConfig) ValidationResult {
	taskName := filepath.Base(taskDir)
	result := ValidationResult{TaskName: taskName}

	// Find policy files
	templatePath, constraintPath, err := findPolicyFiles(taskDir, config.LibraryDir)
	if err != nil {
		result.Error = err.Error()
		return result
	}

	// Prefer constraint.yaml from artifacts if it exists
	// Prefer constraint.yaml from task dir or artifacts
	taskConstraint := filepath.Join(taskDir, "constraint.yaml")
	artifactConstraint := filepath.Join(taskDir, "artifacts", "constraint.yaml")

	if _, err := os.Stat(taskConstraint); err == nil {
		constraintPath = taskConstraint
	} else if _, err := os.Stat(artifactConstraint); err == nil {
		constraintPath = artifactConstraint
	}

	// Read and patch constraint
	constraintData, err := os.ReadFile(constraintPath)
	if err != nil {
		result.Error = fmt.Sprintf("reading constraint: %v", err)
		return result
	}
	patchedConstraint, err := patchConstraintYAML(constraintData)
	if err != nil {
		result.Error = fmt.Sprintf("patching constraint: %v", err)
		return result
	}

	// Read and patch alpha resources
	alphaPath := filepath.Join(taskDir, "artifacts", "resource-alpha.yaml")
	alphaData, err := os.ReadFile(alphaPath)
	if err != nil {
		result.Error = fmt.Sprintf("reading alpha resources: %v", err)
		return result
	}
	patchedAlpha, err := addNamespaceToResourceYAML(alphaData, "default")
	if err != nil {
		result.Error = fmt.Sprintf("patching alpha resources: %v", err)
		return result
	}

	// Read and patch beta resources
	betaPath := filepath.Join(taskDir, "artifacts", "resource-beta.yaml")
	betaData, err := os.ReadFile(betaPath)
	if err != nil {
		result.Error = fmt.Sprintf("reading beta resources: %v", err)
		return result
	}
	patchedBeta, err := addNamespaceToResourceYAML(betaData, "default")
	if err != nil {
		result.Error = fmt.Sprintf("patching beta resources: %v", err)
		return result
	}

	// Test alpha (should have NO violations)
	alphaViolations, err := runGatorTest([]byte(templatePath), patchedConstraint, patchedAlpha)
	if err != nil {
		result.Error = fmt.Sprintf("testing alpha: %v", err)
		return result
	}
	result.AlphaViolations = alphaViolations

	// Test beta (SHOULD have violations)
	betaViolations, err := runGatorTest([]byte(templatePath), patchedConstraint, patchedBeta)
	if err != nil {
		result.Error = fmt.Sprintf("testing beta: %v", err)
		return result
	}
	result.BetaViolations = betaViolations

	// Pass = alpha has NO violations AND beta HAS violations
	alphaOK := len(alphaViolations) == 0
	betaOK := len(betaViolations) > 0
	result.Passed = alphaOK && betaOK

	return result
}

func validateTasksSequential(tasks []string, config ValidateConfig) []ValidationResult {
	var results []ValidationResult

	for _, taskDir := range tasks {
		taskName := filepath.Base(taskDir)
		if config.Verbose {
			fmt.Printf("Validating: %s\n", taskName)
		}

		result := validateTask(taskDir, config)
		results = append(results, result)

		status := "FAIL"
		if result.Passed {
			status = "PASS"
		}
		fmt.Printf("  %s: %s\n", taskName, status)
	}

	return results
}

func validateTasksParallel(tasks []string, config ValidateConfig) []ValidationResult {
	results := make([]ValidationResult, len(tasks))
	var wg sync.WaitGroup
	sem := make(chan struct{}, config.Parallel)
	var mu sync.Mutex

	for i, taskDir := range tasks {
		wg.Add(1)
		go func(idx int, td string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			result := validateTask(td, config)

			mu.Lock()
			results[idx] = result
			status := "FAIL"
			if result.Passed {
				status = "PASS"
			}
			fmt.Printf("  %s: %s\n", result.TaskName, status)
			mu.Unlock()
		}(i, taskDir)
	}

	wg.Wait()
	return results
}

func printValidationSummary(results []ValidationResult) {
	passed := countPassed(results)
	failed := len(results) - passed

	fmt.Printf("\n%s\n", strings.Repeat("=", 60))
	fmt.Printf("Validation Results: %d/%d passed\n", passed, len(results))
	fmt.Printf("%s\n", strings.Repeat("=", 60))

	if passed > 0 {
		fmt.Printf("\nPASSED (%d):\n", passed)
		for _, r := range results {
			if r.Passed {
				fmt.Printf("  %s\n", r.TaskName)
			}
		}
	}

	if failed > 0 {
		fmt.Printf("\nFAILED (%d):\n", failed)
		for _, r := range results {
			if !r.Passed {
				fmt.Printf("\n  %s\n", r.TaskName)
				if r.Error != "" {
					fmt.Printf("    Error: %s\n", r.Error)
				} else {
					if len(r.AlphaViolations) > 0 {
						fmt.Printf("    Alpha violations (should be none): %d\n", len(r.AlphaViolations))
						for i, v := range r.AlphaViolations {
							if i >= 3 {
								fmt.Printf("      ... and %d more\n", len(r.AlphaViolations)-3)
								break
							}
							fmt.Printf("      - %s\n", truncate(v, 80))
						}
					} else {
						fmt.Printf("    Alpha: OK (no violations)\n")
					}

					if len(r.BetaViolations) > 0 {
						fmt.Printf("    Beta violations (expected): %d\n", len(r.BetaViolations))
					} else {
						fmt.Printf("    Beta: PROBLEM (no violations, but expected some)\n")
					}
				}
			}
		}
	}
}

func countPassed(results []ValidationResult) int {
	count := 0
	for _, r := range results {
		if r.Passed {
			count++
		}
	}
	return count
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen-3] + "..."
}
