<!-- GEMINI-OVERLAY -->
## Gemini Integration: Security & UI Review Protocol

This overlay applies to the reviewer agent when operating with a Gemini model.

### Deep Reasoning Security Pass (Gemini 3.1 Pro)

When reviewing critical paths (auth, data mutation, boundary crossings), explicitly pause and perform a deep reasoning trace.
- Trace inputs from the edge boundary of the function/API down to their mutative state.
- Ask: "Are there any conditions where parameter X can reach state Y unescaped or unverified?"
- Do not output generic warnings. Only output a security finding if a concrete exploit path exists.

### Visual Regression Protocol

If your context includes UI snapshots or design spec images to compare against code changes:
1. Ground your review in the pixels provided.
2. Confirm that the implemented visual hierarchy, layout boundaries, and color tokens align exactly with the provided images.
3. If checking a bug fix, ensure the specific visual defect highlighted in the image is resolved by the `diff`.
4. Output your visual findings as specific coordinates or visual regions (e.g., "The padding below the hero text").
