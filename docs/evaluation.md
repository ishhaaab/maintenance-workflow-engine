# Evaluation protocol

Keep model quality, execution reliability and fixture throughput separate. All raw measured artifacts identify their adapter and environment. A rerun gets a new artifact path; it does not replace an earlier failure.

## Planner

`fixtures/planner_dev.jsonl` has 40 synthetic development examples. `fixtures/planner_test.jsonl` has 80 held-out examples. Both are generated from explicit templates, with no duplicated normalized request text or ID across splits. They contain related templates, so they are not independent evidence of broad natural-language generalization.

Labels were authored and reviewed by the implementing agent against explicit catalog/grammar templates. They have not been human-reviewed; this provenance is recorded in every example. Freeze the corpus, prompt, model revision and decoding settings before a held-out real-model run. Do not tune on the held-out results.

The fixture smoke test is useful for checking report machinery only:

```sh
uv run python scripts/evaluate_planner.py --output artifacts/fixture-smoke.json
```

After approval of a local GPU or provider budget, configure `WORKFLOW_MODEL_URL`, `WORKFLOW_MODEL_NAME`, `WORKFLOW_MODEL_REVISION`, and optionally `WORKFLOW_MODEL_KEY`. Then run:

```sh
uv run python scripts/evaluate_planner.py --real --approve-inference --output artifacts/planner-held-out-run-1.json
```

Each case makes one provider request with retries disabled. The runtime runner separately enforces at most two accounted calls per workflow. Reports include schema-valid rate, exact required-field accuracy, unauthorized/unrequested proposal rate, clarification precision/recall, latency and returned token usage. An unsafe proposal blocked by policy is not a correct planner prediction. Invalid output converted to clarification is not counted as a schema-valid success.

### Reproduce the local Qwen run

The measured run is recorded in [the planner report](../reports/planner.md). Model/runtime downloads and SHA256 hashes are pinned in [the frozen protocol](../reports/planner-qwen-q4_0-freeze.json). Download both Q4_0 shards to `model/`, keep their original names, and extract both Windows CUDA 12.4 runtime archives into one directory. See the model's [Apache-2.0 license](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/blob/bb5d59e06d9551d752d08b292a50eb208b07ab1f/LICENSE) and llama.cpp's [MIT license](https://github.com/ggml-org/llama.cpp/blob/b11960/LICENSE). Model weights and runtime binaries are not committed to this repository.

Start the pinned server from PowerShell, replacing the executable path with your extracted runtime location:

```powershell
& 'C:\path\to\llama-server.exe' --model model/qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf --alias Qwen/Qwen2.5-7B-Instruct --host 127.0.0.1 --port 18080 --ctx-size 4096 --parallel 1 --gpu-layers all --fit off --flash-attn on --batch-size 512 --ubatch-size 128 --threads 6 --seed 42 --temp 0 --top-k 0 --top-p 1 --min-p 0 --repeat-penalty 1 --cache-ram 0 --no-cache-prompt --no-webui --no-agent --cors-origins localhost --offline
```

Once `http://127.0.0.1:18080/health` returns 200, configure another PowerShell terminal:

```powershell
$env:WORKFLOW_MODEL_URL = 'http://127.0.0.1:18080/v1'
$env:WORKFLOW_MODEL_NAME = 'Qwen/Qwen2.5-7B-Instruct'
$env:WORKFLOW_MODEL_REVISION = 'Qwen/Qwen2.5-7B-Instruct-GGUF@bb5d59e06d9551d752d08b292a50eb208b07ab1f:Q4_0'
uv run python scripts/evaluate_planner.py --real --approve-inference --cases fixtures/planner_dev.jsonl --output artifacts/planner-dev-reproduction-1.json
uv run python scripts/evaluate_planner.py --real --approve-inference --output artifacts/planner-held-out-reproduction-1.json
```

Stop the server with Ctrl+C afterward. A new run needs a new output filename. These reproduction commands use the same adapter and evaluation function as the recorded run; the recorded run additionally journaled raw synthetic responses for inspection. The original held-out results are now known, so subsequent runs are reproductions, not a new unseen evaluation. Any further tuning should use development data and a newly frozen unseen set for a new quality claim.

## Fault matrix

With isolated MongoDB and Redis available:

```sh
uv run python scripts/benchmark_faults.py --output-dir artifacts/faults-run-1
```

The driver runs all 20 IDs from `fixtures/fault_cases.jsonl` with seeds 0–9. Every case uses a fresh MongoDB database, Redis stream and two simulator subprocesses. Tests use programmatic explicit approval of synthetic fixture plans. Seeds vary worker ordering and short scheduling jitter; failure injection points are deliberate checkpoints rather than random unobserved timing.

Output includes per-case state, action keys, attempt counts, bounded history and logical effect counts, plus pytest/JUnit output and a summary. Missing runs, duplicate records, wrong states, wrong effect counts, substituted adapter labels or nonzero test exit status fail validation. Expected `NEEDS_REVIEW` cases are reported separately from automatic completion.

## Load

Start the fixture-mode Compose stack, set `WORKFLOW_BENCHMARK_API_KEY` to tenant A's demo key, and explicitly approve synthetic benchmark plans:

```sh
uv run python scripts/benchmark_load.py --approve-plans --output artifacts/load-run-1.json
```

The default is 100 workflows at each client concurrency 1, 5 and 20. Reports keep durable admission p50/p95, active duration excluding approval wait, queue delay, approval wait, confirmed effects, completion and errors separate. Active duration includes queue and client observation overhead; queue is also reported independently. UTC event intervals require synchronized clocks across processes. These are observed timings on the recorded machine, not a hardware-independent SLO.

Fixture latency is not model latency. Run real-model latency separately if the approved budget permits. The load driver rejects non-fixture plans and does not silently switch adapters.
