package main

import "google.golang.org/genai"

// Config holds generator configuration
type Config struct {
	LibraryRoot  string
	OutputDir    string
	SkipList     []string
	Verbose      bool
	GeminiClient *genai.Client
}

// Suite represents a gatekeeper suite.yaml file
type Suite struct {
	Metadata struct{ Name string }
	Tests    []SuiteTest
}

// SuiteTest represents a test within a suite
type SuiteTest struct {
	Name       string
	Template   string
	Constraint string
	Cases      []SuiteCase
}

// SuiteCase represents a test case
type SuiteCase struct {
	Name       string
	Object     string
	Inventory  []string
	Assertions []SuiteAssertion
}

// SuiteAssertion represents a violation assertion
type SuiteAssertion struct {
	Violations any
}

// TaskCase represents a processed test case for task generation
type TaskCase struct {
	Name           string
	Expected       string // "alpha" (compliant) or "beta" (violating)
	ObjectPath     string
	InventoryPaths []string
}

// TaskMetadata holds all info needed to generate a task
type TaskMetadata struct {
	TaskID         string
	SuiteName      string
	TestName       string
	TemplatePath   string
	ConstraintPath string
	Cases          []TaskCase
}

// TaskManifest represents a generated manifest file
type TaskManifest struct {
	Path          string
	RelPath       string
	CaseName      string
	Expected      string
	Kind          string
	Name          string
	Namespace     string
	Doc           map[string]interface{}
	Inventory     bool
	ClusterScoped bool
}

// ClusterResource tracks cluster-scoped resources for cleanup
type ClusterResource struct {
	Kind string
	Name string
}

// TaskArtifacts holds all generated artifacts for a task
type TaskArtifacts struct {
	Manifests        []TaskManifest
	CaseFiles        map[string][]string
	InventoryFiles   map[string][]string
	Namespaces       []string
	ClusterResources []ClusterResource
}

// PromptContext holds all context needed to generate a prompt
type PromptContext struct {
	TaskID         string
	Title          string
	Description    string
	TemplateYAML   string
	ConstraintYAML string
	AlphaExamples  []string
	BetaExamples   []string
}
