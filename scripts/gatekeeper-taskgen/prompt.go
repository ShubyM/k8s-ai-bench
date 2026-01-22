package main

import (
	"context"
	"fmt"
	"strings"

	"google.golang.org/genai"
)

// BuildPrompt generates a task prompt, using Gemini if available
func BuildPrompt(cfg Config, ctx PromptContext) string {
	if cfg.GeminiClient != nil {
		if prompt, err := generatePromptWithGemini(cfg.GeminiClient, ctx); err == nil {
			return prompt
		} else if cfg.Verbose {
			fmt.Printf("  Gemini fallback for %s: %v\n", ctx.TaskID, err)
		}
	}
	return buildTemplatePrompt(ctx)
}

func buildTemplatePrompt(ctx PromptContext) string {
	var b strings.Builder
	b.WriteString("You are validating Kubernetes resources against a compliance policy.\n")
	b.WriteString("Some resources are compliant, some are not. The failing resources use names starting with 'resource-beta-'.\n")
	b.WriteString("Identify only the violating resource names.\n\n")
	if ctx.Title != "" {
		fmt.Fprintf(&b, "Policy: %s\n", ctx.Title)
	}
	if ctx.Description != "" {
		fmt.Fprintf(&b, "Details: %s\n", ctx.Description)
	}
	b.WriteString("\nRespond with the names of violating resources only, one per line.")
	return b.String()
}

func generatePromptWithGemini(client *genai.Client, ctx PromptContext) (string, error) {
	geminiCtx := context.Background()

	var metaPrompt strings.Builder
	metaPrompt.WriteString(`You are helping generate a task prompt for a Kubernetes policy compliance benchmark.

The benchmark tests whether an AI can identify policy-violating Kubernetes resources.
Resources named "resource-alpha-XX" are COMPLIANT (pass the policy).
Resources named "resource-beta-XX" are VIOLATING (fail the policy).

Generate a clear, concise task prompt that:
1. Explains what policy is being enforced (without mentioning alpha/beta naming)
2. Describes what makes a resource compliant vs violating
3. Asks the AI to identify only the violating resource names
4. Is 3-6 sentences maximum

Policy Information:
`)

	if ctx.Title != "" {
		fmt.Fprintf(&metaPrompt, "Title: %s\n", ctx.Title)
	}
	if ctx.Description != "" {
		fmt.Fprintf(&metaPrompt, "Description: %s\n", ctx.Description)
	}

	if ctx.ConstraintYAML != "" {
		constraint := ctx.ConstraintYAML
		if len(constraint) > 2000 {
			constraint = constraint[:2000] + "\n... (truncated)"
		}
		fmt.Fprintf(&metaPrompt, "\nConstraint Definition:\n```yaml\n%s\n```\n", constraint)
	}

	if len(ctx.AlphaExamples) > 0 {
		example := ctx.AlphaExamples[0]
		if len(example) > 1500 {
			example = example[:1500] + "\n... (truncated)"
		}
		fmt.Fprintf(&metaPrompt, "\nExample COMPLIANT resource:\n```yaml\n%s\n```\n", example)
	}
	if len(ctx.BetaExamples) > 0 {
		example := ctx.BetaExamples[0]
		if len(example) > 1500 {
			example = example[:1500] + "\n... (truncated)"
		}
		fmt.Fprintf(&metaPrompt, "\nExample VIOLATING resource:\n```yaml\n%s\n```\n", example)
	}

	metaPrompt.WriteString(`
Generate only the task prompt text, nothing else. Do not include markdown formatting.
The prompt should end with: "Respond with the names of violating resources only, one per line."`)

	result, err := client.Models.GenerateContent(geminiCtx, "gemini-2.0-flash", genai.Text(metaPrompt.String()), nil)
	if err != nil {
		return "", fmt.Errorf("gemini API error: %w", err)
	}

	if len(result.Candidates) == 0 || len(result.Candidates[0].Content.Parts) == 0 {
		return "", fmt.Errorf("empty response from Gemini")
	}

	text := result.Candidates[0].Content.Parts[0].Text
	if text == "" {
		return "", fmt.Errorf("empty text in Gemini response")
	}

	return strings.TrimSpace(text), nil
}
