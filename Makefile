.PHONY: install install-dev test test-unit test-http lint typecheck format clean analyze validate

# --- Installation ---

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"
	pip install respx  # HTTP mocking for driver tests

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
	ruff check agent_core/ tests/

lint-fix:
	ruff check --fix agent_core/ tests/

format:
	ruff format agent_core/ tests/

typecheck:
	mypy agent_core/

# --- CLI shortcuts ---

analyze:
	agent-core analyze --root .

validate:
	agent-core validate --config swarm.yaml

init:
	agent-core init

# --- Cleanup ---

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	rm -rf .agent-swarm/outputs/

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
	@echo "  clean        Remove build artifacts and caches"
