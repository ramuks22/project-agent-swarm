.PHONY: install install-dev test test-unit test-http lint typecheck format clean analyze validate docs-check

# --- Installation ---

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

# --- Testing ---

test:
	pytest tests/ -v

test-unit:
	pytest tests/ -v -k "not test_driver_http"

test-http:
	pytest tests/test_driver_http.py -v

test-cov:
	pytest tests/ --cov=agent_core --cov-report=term-missing --cov-report=html

# --- Code quality ---

lint:
	ruff check src/agent_core/ tests/

lint-fix:
	ruff check --fix src/agent_core/ tests/

format:
	ruff format src/agent_core/ tests/

typecheck:
	mypy src/agent_core/

# --- Documentation ---

docs-check:
	@echo "Checking documentation for absolute paths and local file links..."
	@! grep -r "file:///" . --exclude-dir=.venv --exclude-dir=.git --exclude=Makefile
	@! grep -r "/Users/" docs/
	@echo "✅ Documentation is clean."

# --- CLI shortcuts ---

analyze:
	agent-core analyze --root .

validate:
	agent-core validate --config swarm.yaml

init:
	agent-core init

optimizer-verify:
	agent-core optimizer-verify "$(TASK)" $(BUDGET)

# --- Cleanup ---

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	rm -rf .swarm/outputs/

# --- Help ---

help:
	@echo "Available targets:"
	@echo "  install      Install package (production)"
	@echo "  install-dev  Install package + dev dependencies"
	@echo "  test         Run full test suite"
	@echo "  test-unit    Run tests excluding HTTP driver tests"
	@echo "  test-http    Run HTTP driver integration tests only"
	@echo "  test-cov     Run tests with coverage report"
	@echo "  lint         Check code style (ruff)"
	@echo "  lint-fix     Auto-fix lint issues"
	@echo "  format       Format code (ruff format)"
	@echo "  typecheck    Run mypy type checking"
	@echo "  analyze      Run agent-core analyze on current directory"
	@echo "  validate     Validate swarm.yaml"
	@echo "  init         Create starter swarm.yaml"
	@echo "  optimizer-verify Run the context optimizer verifier (TASK=\"...\" BUDGET=...)"
	@echo "  docs-check   Verify documentation for broken local links"
	@echo "  clean        Remove build artifacts and caches"
