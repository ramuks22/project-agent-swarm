<!-- GEMINI-OVERLAY -->
## Gemini Integration: Routing & Conflict Resolution

This overlay applies when the swarm is operating under a Gemini model, augmenting the universal orchestrator guidelines.

### Resolution Protocol (Gemini 3.1 Pro)

If parallel agents reach contradictory conclusions or a sub-task enters a failure loop, do not break ties arbitrarily.
1. Capture the exact rationale and outputs from the conflicting agents.
2. Escalate the conflict locally. Evaluate the opposing `findings[]` using Gemini 3.1 Pro's deep reasoning.
3. Perform a logical trace comparing both agent claims against the original user specs.
4. If one path is explicitly correct, proceed and provide a definitive explanation to the conflicting agents.
5. If both approaches are valid trade-offs, surface the dichotomy to the user immediately requesting a decision.

### Multimodal Task Decomposition

When breaking down tasks that include images, video, or audio (e.g., UI bug reported via screen recording):
- Assign clear, focused verification goals for multimodal analysis.
- Provide the multimodal context to the downstream specialist (e.g., UI implementer) without summarizing away critical details.
