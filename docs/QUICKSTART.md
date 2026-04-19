# QUICKSTART: Run Your First Swarm in 60 Seconds

Welcome to **Agent Swarm**. This guide will get you up and running with a production-grade agentic workflow immediately.

## 1. Environment Setup

We recommend using `uv` for high-performance dependency management.

```bash
# Install uv (if not already present)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies and the agent-core package
uv pip install -e .
```

## 2. Configure API Keys

Set your environment variables for the models you intend to use:

```bash
export ANTHROPIC_API_KEY="sk-..."
export GOOGLE_API_KEY="AIza..."
# OR
export OPENAI_API_KEY="sk-..."
```

## 3. Initialize Your Repository

Initialize the swarm configuration by analyzing your codebase. This will discover the best roles for your specific tech stack.

```bash
# Create swarm.yaml and discover roles
agent-core init
agent-core analyze --root . --output swarm.yaml
```

## 4. Run a Sequential Swarm

Trigger a multi-agent workflow to implement a small feature or fix a bug.

```bash
agent-core run feature-dev --task "Add a health-check endpoint to apps/api/main.py"
```

## 5. View Traces

Start the observability dashboard to see exactly what the agents are thinking:

```bash
cd infra
docker-compose up -d
# Open http://localhost:6006
```

---

## Next Steps
- Review [DRIVERS.md](./DRIVERS.md) for advanced model configuration.
- Check [WORKFLOWS.md](./WORKFLOWS.md) to learn how to create custom playbooks.
