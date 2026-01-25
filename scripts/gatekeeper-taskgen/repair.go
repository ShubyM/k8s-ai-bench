package main

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"google.golang.org/genai"
)

const repairModel = "gemini-2.5-flash"

func repairTask(cfg Config, outDir, taskID string) RepairResult {
	if cfg.GeminiClient == nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: "GEMINI_API_KEY not set"}
	}

	alphaPath, betaPath, err := findAlphaBeta(outDir)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: err.Error()}
	}

	// Find inventory files
	inventoryPaths, _ := findInventory(outDir)

	constraintPath := filepath.Join(outDir, "constraint.yaml")
	templatePath := filepath.Join(outDir, "template.yaml")
	constraintYAML, err := os.ReadFile(constraintPath)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: err.Error()}
	}
	templateYAML, err := os.ReadFile(templatePath)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: err.Error()}
	}
	alphaYAML, err := os.ReadFile(alphaPath)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: err.Error()}
	}
	betaYAML, err := os.ReadFile(betaPath)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: err.Error()}
	}

	// Read inventory files
	var inventoryYAMLs []string
	for _, invPath := range inventoryPaths {
		if data, err := os.ReadFile(invPath); err == nil {
			inventoryYAMLs = append(inventoryYAMLs, string(data))
		}
	}

	prompt := buildRepairPrompt(taskID, betaPath, string(constraintYAML), string(templateYAML), string(alphaYAML), string(betaYAML), inventoryYAMLs)
	ctx := context.Background()
	result, err := cfg.GeminiClient.Models.GenerateContent(ctx, repairModel, genai.Text(prompt), nil)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: fmt.Sprintf("gemini API error: %v", err)}
	}
	text, err := extractGeminiText(result)
	if err != nil {
		return RepairResult{TaskID: taskID, Status: "error", Error: err.Error()}
	}

	cleaned := stripCodeFences(text)
	if strings.Contains(strings.ToUpper(cleaned), "NO_CHANGES") {
		if cfg.Verbose {
			fmt.Printf("Repair %s: NO_CHANGES\n", taskID)
		}
		return RepairResult{TaskID: taskID, Status: "no_changes", FilePath: betaPath}
	}

	diff := normalizeDiff(cleaned, filepath.ToSlash(betaPath))
	if cfg.Verbose {
		fmt.Printf("Repair %s: applying diff\n", taskID)
	}
	if err := applyPatch(diff); err != nil {
		return RepairResult{TaskID: taskID, Status: "error", FilePath: betaPath, Diff: diff, Error: err.Error()}
	}
	return RepairResult{TaskID: taskID, Status: "repaired", FilePath: betaPath, Diff: diff}
}

func findAlphaBeta(outDir string) (string, string, error) {
	artifactsDir := filepath.Join(outDir, "artifacts")
	alphaMatches, _ := filepath.Glob(filepath.Join(artifactsDir, "alpha-*.yaml"))
	betaMatches, _ := filepath.Glob(filepath.Join(artifactsDir, "beta-*.yaml"))
	sort.Strings(alphaMatches)
	sort.Strings(betaMatches)
	if len(alphaMatches) == 0 || len(betaMatches) == 0 {
		return "", "", fmt.Errorf("missing alpha or beta artifacts")
	}
	return alphaMatches[0], betaMatches[0], nil
}

func findInventory(outDir string) ([]string, error) {
	artifactsDir := filepath.Join(outDir, "artifacts")
	matches, _ := filepath.Glob(filepath.Join(artifactsDir, "inventory-*.yaml"))
	sort.Strings(matches)
	return matches, nil
}

func buildRepairPrompt(taskID, targetPath, constraintYAML, templateYAML, alphaYAML, betaYAML string, inventoryYAMLs []string) string {
	constraint := truncateString(constraintYAML, 3000)
	template := truncateString(templateYAML, 3000)
	alphaBlock := fmt.Sprintf("```yaml\n%s\n```", strings.TrimSpace(alphaYAML))
	betaBlock := fmt.Sprintf("```yaml\n%s\n```", strings.TrimSpace(betaYAML))

	var inventorySection string
	if len(inventoryYAMLs) > 0 {
		var invBlocks []string
		for i, inv := range inventoryYAMLs {
			if i >= 3 { // Limit to 3 inventory files to avoid token limits
				break
			}
			invBlocks = append(invBlocks, fmt.Sprintf("```yaml\n%s\n```", strings.TrimSpace(inv)))
		}
		inventorySection = fmt.Sprintf("\nInventory (existing resources in cluster that beta may need to conflict with):\n%s\n", strings.Join(invBlocks, "\n"))
	}

	prompt := fmt.Sprintf(
		"You are editing Kubernetes manifests for a Gatekeeper policy benchmark.\n\n"+
			"Task: Modify ONLY the beta manifest so it VIOLATES the policy. The alpha manifest is COMPLIANT and must stay unchanged.\n\n"+
			"IMPORTANT:\n"+
			"- Beta should FAIL policy validation (it's the bad example)\n"+
			"- Alpha should PASS policy validation (it's the good example)\n"+
			"- For policies about uniqueness/duplicates: beta should CREATE a conflict with existing resources\n"+
			"- For policies about required fields: beta should be MISSING the required fields\n"+
			"- Do NOT make beta compliant - it must violate the policy!\n\n"+
			"Preserve metadata.name, metadata.namespace, and all labels.\n"+
			"Return a unified diff ONLY for the beta manifest.\n"+
			"Use the following file path in the diff header: %s\n"+
			"If the beta manifest already violates the policy, respond with NO_CHANGES.\n\n"+
			"Constraint:\n%s\n\n"+
			"Template:\n%s\n"+
			"%s\n"+
			"Alpha manifest (COMPLIANT - do not change):\n%s\n\n"+
			"Beta manifest (must VIOLATE policy - modify this only):\n%s\n",
		targetPath,
		constraint,
		template,
		inventorySection,
		alphaBlock,
		betaBlock,
	)
	return strings.TrimSpace(prompt)
}

func truncateString(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	return s[:limit] + "\n... (truncated)"
}

func stripCodeFences(text string) string {
	text = strings.TrimSpace(text)
	if !strings.HasPrefix(text, "```") {
		return text
	}
	lines := strings.Split(text, "\n")
	if len(lines) == 0 {
		return text
	}
	if strings.HasPrefix(lines[0], "```") {
		lines = lines[1:]
	}
	if len(lines) > 0 && strings.HasPrefix(lines[len(lines)-1], "```") {
		lines = lines[:len(lines)-1]
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

func normalizeDiff(diffText, targetPath string) string {
	lines := strings.Split(diffText, "\n")
	replaced := false
	for i, line := range lines {
		if strings.HasPrefix(line, "--- ") {
			lines[i] = "--- " + targetPath
			replaced = true
		} else if strings.HasPrefix(line, "+++ ") {
			lines[i] = "+++ " + targetPath
			replaced = true
		}
	}
	if !replaced {
		lines = append([]string{"--- " + targetPath, "+++ " + targetPath}, lines...)
	}
	return strings.Join(lines, "\n") + "\n"
}

func applyPatch(diff string) error {
	cmd := exec.Command("patch", "-p0", "-u", "-i", "-")
	cmd.Stdin = strings.NewReader(diff)
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("patch failed: %s", strings.TrimSpace(output.String()))
	}
	return nil
}

func extractGeminiText(result *genai.GenerateContentResponse) (string, error) {
	if result == nil || len(result.Candidates) == 0 {
		return "", fmt.Errorf("empty response from Gemini")
	}
	content := result.Candidates[0].Content
	if content == nil || len(content.Parts) == 0 {
		return "", fmt.Errorf("empty response from Gemini")
	}
	text := content.Parts[0].Text
	if strings.TrimSpace(text) == "" {
		return "", fmt.Errorf("empty response from Gemini")
	}
	return strings.TrimSpace(text), nil
}

func writeRepairReport(outputDir string, results []RepairResult) error {
	var b strings.Builder

	// Header
	b.WriteString("# Gatekeeper Task Repair Report\n\n")
	b.WriteString(fmt.Sprintf("Generated: %s\n\n", time.Now().Format("2006-01-02 15:04:05")))

	// Count stats
	var repaired, noChanges, errors int
	for _, r := range results {
		switch r.Status {
		case "repaired":
			repaired++
		case "no_changes":
			noChanges++
		case "error":
			errors++
		}
	}

	// Summary table
	b.WriteString("## Summary\n\n")
	b.WriteString("| Status | Count |\n")
	b.WriteString("|--------|-------|\n")
	b.WriteString(fmt.Sprintf("| Repaired | %d |\n", repaired))
	b.WriteString(fmt.Sprintf("| No Changes | %d |\n", noChanges))
	b.WriteString(fmt.Sprintf("| Errors | %d |\n", errors))
	b.WriteString("\n---\n\n")

	// Repaired tasks with diffs
	if repaired > 0 {
		b.WriteString("## Repaired Tasks\n\n")
		for _, r := range results {
			if r.Status == "repaired" {
				b.WriteString(fmt.Sprintf("### %s\n\n", r.TaskID))
				b.WriteString(fmt.Sprintf("**File:** `%s`\n\n", r.FilePath))
				b.WriteString("```diff\n")
				b.WriteString(r.Diff)
				if !strings.HasSuffix(r.Diff, "\n") {
					b.WriteString("\n")
				}
				b.WriteString("```\n\n")
			}
		}
		b.WriteString("---\n\n")
	}

	// No changes list
	if noChanges > 0 {
		b.WriteString("## No Changes Needed\n\n")
		for _, r := range results {
			if r.Status == "no_changes" {
				b.WriteString(fmt.Sprintf("- %s\n", r.TaskID))
			}
		}
		b.WriteString("\n---\n\n")
	}

	// Errors
	if errors > 0 {
		b.WriteString("## Errors\n\n")
		for _, r := range results {
			if r.Status == "error" {
				b.WriteString(fmt.Sprintf("### %s\n\n", r.TaskID))
				b.WriteString("```\n")
				b.WriteString(r.Error)
				if !strings.HasSuffix(r.Error, "\n") {
					b.WriteString("\n")
				}
				b.WriteString("```\n\n")
			}
		}
	}

	reportPath := filepath.Join(outputDir, "repair-report.md")
	return os.WriteFile(reportPath, []byte(b.String()), 0644)
}
