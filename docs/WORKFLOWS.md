# Workflow Guide

Workflows are pre-defined sequences of agent roles designed to solve specific types of technical tasks. The **Agent Swarm** orchestrator executes these chains sequentially, passing the cumulative state (diffs, findings, summaries) from one agent to the next.

## Standard Workflows

The framework currently supports the following built-in workflows:

| Workflow Name | Role Sequence | Use Case |
| :--- | :--- | :--- |
| `feature-dev` | Architect → Implementer → QA Engineer → Reviewer | Implementing new functionality from scratch. |
| `bug-fix` | Debugger → Reviewer | Diagnosing a defect and applying a fix. |
| `code-review` | Reviewer | Passive architectural and security audit of changes. |
| `test-generation` | QA Engineer → Reviewer | Adding unit or integration tests to existing code. |

## How it Works

When you run `agent-core run <workflow>`, the orchestrator:

1.  **Validates Roles**: It checks your `swarm.yaml` (or auto-discovered metadata) to ensure the required agents are defined.
2.  **Filters the Chain**: If a role in the sequence is missing from your configuration, it is skipped.
3.  **Executes Sequentially**: Each agent is called with the current `SwarmContext`.
4.  **Enforces Quality Gates**: If an agent's quality gate fails (and strict mode is enabled), the entire workflow halts and escalates to the user.

## Customizing Workflows

### 1. Manual Chains
You can trigger a custom sequence directly via the Python API without using the CLI workflow names:

```python
results = await agent_core.run_sequential(
    task_description="Refactor the auth logic",
    agent_chain=[
        (architect_spec, [Path("src/auth.py")]),
        (reviewer_spec, [Path("src/auth.py")]),
    ],
    config=config,
    api_key=api_key,
    repo_metadata=metadata,
)
```

### 2. Defining New Templates
Workflows often rely on the `AgentSpec` templates located in `workflows/` (for guidance) and `agents/` (for role definitions). Adding a new `.yaml` file to your local `agents/` directory allows the orchestrator to resolve that role name during execution.
