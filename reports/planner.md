# Planner evaluation status

**Real-model gate: incomplete. No live inference has been run.**

Implemented: OpenAI-compatible asynchronous adapter, strict schema validation, tenant/request policy, 40 development and 80 held-out synthetic examples, and measured report generation.

The corpus labels are agent-authored template labels. Human verification and an approved local GPU/provider budget are still required. The checked-in prompt has not been tuned against held-out model outputs.

Offline tests cover valid/invalid structured output, unsupported fields, fenced prose, refusal-like empty output, provider timeout, call accounting, cross-tenant proposals and request-intent enforcement. Fixture report smoke tests cannot establish LLM accuracy.

Run the procedure in [evaluation.md](../docs/evaluation.md) and record a new measured artifact before replacing this status with model-quality claims.
