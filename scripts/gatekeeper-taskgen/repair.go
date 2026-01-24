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

	"google.golang.org/genai"
)

const repairModel = "gemini-2.5-flash"

func repairTask(cfg Config, outDir, taskID string) error {
	if cfg.GeminiClient == nil {
		return fmt.Errorf("GEMINI_API_KEY not set")
	}

	alphaPath, betaPath, err := findAlphaBeta(outDir)
	if err != nil {
		return err
	}

	constraintPath := filepath.Join(outDir, "constraint.yaml")
	templatePath := filepath.Join(outDir, "template.yaml")
	constraintYAML, err := os.ReadFile(constraintPath)
	if err != nil {
		return err
	}
	templateYAML, err := os.ReadFile(templatePath)
	if err != nil {
		return err
	}
	alphaYAML, err := os.ReadFile(alphaPath)
	if err != nil {
		return err
	}
	betaYAML, err := os.ReadFile(betaPath)
	if err != nil {
		return err
	}

	prompt := buildRepairPrompt(taskID, betaPath, string(constraintYAML), string(templateYAML), string(alphaYAML), string(betaYAML))
	ctx := context.Background()
	result, err := cfg.GeminiClient.Models.GenerateContent(ctx, repairModel, genai.Text(prompt), nil)
	if err != nil {
		return fmt.Errorf("gemini API error: %w", err)
	}
	text, err := extractGeminiText(result)
	if err != nil {
		return err
	}

	cleaned := stripCodeFences(text)
	if strings.Contains(strings.ToUpper(cleaned), "NO_CHANGES") {
		if cfg.Verbose {
			fmt.Printf("Repair %s: NO_CHANGES\n", taskID)
		}
		return nil
	}

	diff := normalizeDiff(cleaned, filepath.ToSlash(betaPath))
	if cfg.Verbose {
		fmt.Printf("Repair %s: applying diff\n", taskID)
	}
	if err := applyPatch(diff); err != nil {
		return err
	}
	return nil
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

func buildRepairPrompt(taskID, targetPath, constraintYAML, templateYAML, alphaYAML, betaYAML string) string {
	constraint := truncateString(constraintYAML, 3000)
	template := truncateString(templateYAML, 3000)
	alphaBlock := fmt.Sprintf("```yaml\n%s\n```", strings.TrimSpace(alphaYAML))
	betaBlock := fmt.Sprintf("```yaml\n%s\n```", strings.TrimSpace(betaYAML))
	prompt := fmt.Sprintf(
		"You are editing Kubernetes manifests for a Gatekeeper policy benchmark.\n\n"+
			"Task: Modify ONLY the beta manifest so it violates the policy. Do NOT modify the alpha manifest.\n"+
			"Preserve metadata.name, metadata.namespace, and all labels. Return a unified diff ONLY for the beta manifest.\n"+
			"Use the following file path in the diff header: %s\n"+
			"If the beta manifest already violates, respond with NO_CHANGES.\n\n"+
			"Constraint:\n%s\n\n"+
			"Template:\n%s\n\n"+
			"Alpha manifest (do not change):\n%s\n\n"+
			"Beta manifest (modify this only):\n%s\n",
		targetPath,
		constraint,
		template,
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
