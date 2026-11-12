.PHONY: install install-dev test lint format validate build deploy clean help diagram

PYTHON ?= python3
VENV := .venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-dev: install ## Install dev + test dependencies
	$(PIP) install -r requirements-dev.txt
	$(PIP) install ruff cfn-lint

test: ## Run tests with coverage (fails if <80%)
	$(PYTEST) --cov=src --cov-report=term-missing --cov-fail-under=80

test-verbose: ## Run tests with verbose output
	$(PYTEST) -v --cov=src --cov-report=term-missing

llm-regression: ## Run LLM regression suite against real Bedrock Haiku 4.5 (~$0.10/run)
	@echo "⚠️  Running LLM regression suite against REAL Bedrock Haiku 4.5."
	@echo "   Expected cost: ~\$$0.10 per full run (8 scenarios × ~6K tokens each)."
	@echo "   Uses the deploy-account AWS credentials in your current shell."
	@echo ""
	RUN_LLM_REGRESSION=1 $(PYTEST) tests/test_llm_regression.py -v -m llm_regression --no-cov

lint: ## Run linter checks
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/

format: ## Auto-format code
	$(RUFF) format src/ tests/
	$(RUFF) check --fix src/ tests/

validate: ## Validate SAM template
	# sam validate + cfn-lint. E3003 suppression lives in .cfnlintrc
	# (with a documented reason), verified in a security review.
	sam validate --lint

diagram: ## Render the architecture diagram-as-code draft to PNG (requires awsdac: brew install awsdac)
	awsdac -f docs/architecture/architecture-draft.dac.yaml -o docs/architecture/architecture-draft.png
	@echo "Rendered docs/architecture/architecture-draft.png"
	@echo "Note: the authoritative diagram is the PowerPoint docs/architecture/guidance-architecture.pptx,"
	@echo "      exported to assets/images/guidance-architecture.png (the image the README embeds)."

build: ## Build SAM application with dependencies
	sam build
	pip install -r requirements.txt -t .aws-sam/build/CapacityAgentFunction/ --quiet

deploy: ## Deploy with saved config (run 'make build' first)
	sam deploy

clean: ## Remove build artifacts and caches
	rm -rf .aws-sam/
	rm -rf .pytest_cache/
	rm -rf __pycache__/
	rm -rf src/__pycache__/
	rm -rf tests/__pycache__/
	rm -f .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

ci: lint test validate ## Run full CI pipeline locally
