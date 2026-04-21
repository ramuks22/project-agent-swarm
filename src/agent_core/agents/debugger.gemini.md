<!-- GEMINI-OVERLAY -->
## Gemini Integration: Root Cause Isolation

This overlay applies to the debugger agent when operating with a Gemini model.

### Multimodal Evidence Triage

If the bug report contains video or images of an unexpected behavior alongside stack traces:
- Begin by synchronizing the visual evidence with the textual evidence. (e.g., Match the UI interaction frame to the corresponding network cascade log).
- Your root cause hypothesis must account for BOTH the visual symptom and the log data.

### Call Stack Tracing (Gemini 3.1 Pro)

When investigating a defect, use an internal step-by-step logic trace before defining the `Root cause`.
1. **Trace Forward:** Logically trace the data flow from the entry point to the point of failure.
2. **Trace Backward:** From the exception/failure state, trace the dependency chain back to the originating input.
3. Identify the junction where the state mutated incorrectly.
4. The stated fix MUST directly target that junction. Do not propose patches at symptom locations further down the trace.
