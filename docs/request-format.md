# Supported requests

The policy accepts this bounded grammar, case-insensitively:

```text
Create an inspection ticket for <asset> [with low|normal|high priority] [and reserve <one|a|1..10> <part> if available].
```

Examples:

- `Create an inspection ticket for P-17`
- `Create an inspection ticket for pump P-17 and reserve one seal kit if available.`
- `Create an inspection ticket for B-22 with high priority and reserve 2 bearing kits if available`

Tenant A owns P-17 and SEAL-1. Tenant B owns B-22 and BEARING-2. Aliases come from the configured read-only catalog. Requests outside the grammar require clarification. Submit clarification as a new request with a new admission key.

The fixture adapter returns only predefined fixture proposals, not generated answers. The model adapter extracts proposals; deterministic policy independently checks asset, part, quantity, priority and reservation intent. Prompt text never grants authorization.
