---
status: superseded by ADR-0017
---

# Evaluate a Search-first Grounded Judge in Shadow Mode

Tag classification keeps Gemini with Google Search Grounding as the production Grounded Judge while Brave LLM Context with Tavily Advanced fallback and DeepSeek Flash is evaluated only in a bounded, non-persisting shadow workflow. Search credentials belong to independently billed Quota Pools and are consumed quota-first, with retries staying on their selected pool; a pool moves aside only after its configured free allowance or a provider quota response, preserving the existing single-decision and human-override guarantees without silently changing production classification.
