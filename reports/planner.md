# Live planner evaluation

**Real-model gate: complete.** Qwen2.5-7B-Instruct Q4_0 returned all 80 held-out responses and matched all 80 expected outcomes on 2026-10-29. The prompt and inference settings were frozen in commit `1016330` before the single held-out pass. There was no tuning on held-out responses.

## Held-out measurement

| Metric | Observed result |
| --- | ---: |
| Provider calls / inference responses | 80 / 80 |
| Schema-valid responses | 80/80, 100% |
| Exact required-field accuracy | 80/80, 100% |
| Unauthorized or unrequested proposals | 0/80 |
| Clarification precision / recall | 100% / 100% |
| Client latency p50 / p95 | 1.804 / 2.304 seconds |
| Prompt / completion tokens | 81,544 / 5,750 |
| Total tokens | 87,294 |
| Provider errors / truncated responses | 0 / 0 |

The set contains 48 executable-plan cases and 32 clarification cases. The latter include four cases each for unknown assets, foreign assets, ambiguous quantities, incompatible parts, unsupported actions, prompt injection, malformed-output requests and ambiguous assets. For plans, every required field must match the label exactly. For clarification, the response must be a schema-valid `needs_input`; the wording of its questions is not compared word for word.

The run started at 12:29:02 UTC and ended at 12:31:25 UTC. Requests were sequential, with the existing 15-second timeout and no HTTP retries. Every raw response was revalidated against its request, tenant catalog, expected outcome and deterministic policy. All finished normally, with zero cached prompt tokens. This evaluation only proposes plans; it does not execute ticket or reservation mutations.

## Model and environment

- Model: [Qwen/Qwen2.5-7B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/tree/bb5d59e06d9551d752d08b292a50eb208b07ab1f), revision `bb5d59e06d9551d752d08b292a50eb208b07ab1f`, Q4_0. The user supplied both shards, totaling 4,431,390,848 bytes. Both SHA256 hashes match the pinned official repository.
- Runtime: [llama.cpp b11960](https://github.com/ggml-org/llama.cpp/releases/tag/b11960), commit `8642541b255bfb5dc39cd351a178be09013a55d6`, Windows x64 CUDA 12.4. Both runtime archive hashes were verified.
- Hardware: RTX 3060 Laptop GPU, 6 GiB VRAM, driver 621.01; Ryzen 7 5800H; 13.9 GiB usable RAM. Windows 11 build 26200, Python 3.12.16.
- Request decoding: temperature 0, maximum 512 output tokens, JSON-object response format. The application still validates the complete schema and tenant policy.
- Server: all model layers on GPU, 4,096-token context, one slot, flash attention, batch 512, microbatch 128, six CPU threads, seed 42, top-k 0, top-p 1, min-p 0, repeat penalty 1. Prompt caching and RAM prompt caching were disabled.
- Prompt SHA256: `63f45311ab835a357e378e41c38c4d2e31540ab94f98c3bb21c1f9c5ca594739`.

Latency is client-observed request time, including local diagnostic capture and validation. The server completed its startup warmup before measurement; model loading is excluded. All inference used the local GPU, with no paid provider API.

## Development history

Four complete passes used only the 40 development examples. Each result and prompt snapshot is retained, including the regression in pass 3.

| Pass | Schema-valid | Exact outcomes | Unauthorized proposals | Change evaluated |
| --- | ---: | ---: | ---: | --- |
| 1 | 40/40 | 14/40 | 2/40 | Original prompt; every valid plan omitted the required final period |
| 2 | 39/40 | 36/40 | 3/40 | Explicit punctuation construction; all 24 valid plans matched |
| 3 | 31/40 | 31/40 | 0/40 | Explicit catalog/quantity prerequisites; eight wrong discriminator values and one overlong question |
| 4 | 40/40 | 40/40 | 0/40 | Literal discriminator values and question length bound |

The four development passes made 160 calls. Together with the single held-out pass, this evaluation used 240 calls and 247,571 returned tokens. No failed development run was dropped or relabeled as held-out evidence.

## Evidence and limits

- [Frozen protocol, model hashes and settings](planner-qwen-q4_0-freeze.json)
- [Full held-out per-case measurements](planner-held-out.json)
- [All development measurements and prompt snapshots](planner-development.json)
- Raw synthetic request/response diagnostics and input snapshots remain locally under `artifacts/planner-qwen-q4_0-*`.
- [Reproduction commands](../docs/evaluation.md#reproduce-the-local-qwen-run)

The labels are agent-authored and agent-reviewed, not human-reviewed. Both splits use related synthetic templates and a small, fixed catalog. These results establish performance on this bounded corpus, not broad natural-language generalization or production accuracy. Human review and a separately collected realistic test set would strengthen those broader claims.
