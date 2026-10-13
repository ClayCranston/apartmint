# Local model decision

Research checked October 9, 2026. This document records a deployment choice, not an apartment-extraction accuracy benchmark.

## Decision for this machine

Use **`Qwen/Qwen3-8B-MLX-4bit`** with native **vllm-metal** for the first local proof. The inspected host is an Apple M4 Mac mini with **24 GB unified memory**, not an NVIDIA CUDA machine. Its available disk space was approximately 24 GiB at inspection, and existing applications were already using substantial memory. The configured `clay-personal` SSH destination timed out, so its GPU, VRAM, drivers and Docker runtime remain unverified.

The official Qwen MLX checkpoint is already cached at revision **`383413e909f3bc5303ce195ebbdf0339c5a1a2a3`**. Reuse that snapshot rather than downloading a duplicate or substituting an incompatible NVFP4 checkpoint. The existing research environment has vllm-metal `0.3.0.dev20260723` and vLLM core `0.25.1`; flags must be checked against that installed runtime. A previous local secret-policy evaluation demonstrates only the behavior it tested. It does **not** establish reliable apartment extraction, listing freshness, resistance to every prompt injection, or suitability for unattended decisions.

The [official Qwen MLX model](https://huggingface.co/Qwen/Qwen3-8B-MLX-4bit) is Apache-2.0 licensed. [vllm-metal's supported-model matrix](https://github.com/vllm-project/vllm-metal/blob/main/docs/supported_models.md) lists Qwen3 support on Apple Silicon. Its [installation guide](https://github.com/vllm-project/vllm-metal/blob/main/docs/installation.md) requires native Apple Silicon/macOS 15+ and provides prebuilt installation methods; plain `pip install vllm-metal` is explicitly unsupported.

## NVFP4 and the NVIDIA Docker target

**`nvidia/Qwen3-8B-NVFP4` is the preferred checkpoint when a compatible Blackwell host is available and tested.** NVIDIA describes it as a ModelOpt quantization of Qwen3-8B with FP4 weights and activations and identifies Blackwell as its supported native hardware. Its card's example targets TensorRT-LLM; vLLM compatibility is established separately by vLLM's ModelOpt support. [NVIDIA model card](https://huggingface.co/nvidia/Qwen3-8B-NVFP4), [checkpoint configuration](https://huggingface.co/nvidia/Qwen3-8B-NVFP4/blob/main/config.json)

NVFP4 is not exclusively executable on Blackwell. vLLM **0.31.0** supports ModelOpt NVFP4 with `--quantization modelopt_fp4` and can fall back to **Marlin W4A16**, using compressed FP4 weights with 16-bit activations, when native FP4 kernels are unavailable. This changes the execution path and can reduce throughput for compute-heavy workloads. Automatic selection is preferred initially; `--linear-backend marlin` is an explicit fallback override. The old `VLLM_NVFP4_GEMM_BACKEND` variable is deprecated. [vLLM ModelOpt documentation](https://docs.vllm.ai/en/v0.31.0/features/quantization/modelopt/)

The pinned vLLM 0.31.0 FP4 Marlin support check requires **CUDA and compute capability at least 7.5**. That code-level check is not a guarantee that every driver, image, dtype and checkpoint combination works. Startup and representative inference must pass on the actual GPU. Older devices may require FP16 rather than BF16 activations. [Pinned Marlin source](https://raw.githubusercontent.com/vllm-project/vllm/v0.31.0/vllm/model_executor/layers/quantization/utils/marlin_utils_fp4.py)

The verified official release provides these image tags:

- `vllm/vllm-openai:v0.31.0`: CUDA 13.0.
- `vllm/vllm-openai:v0.31.0-cu129`: CUDA 12.9.

Choose the image against the target driver's compatibility and record the pulled image digest for reproducibility. [Official vLLM 0.31.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.31.0)

The standard NVIDIA Docker configuration is a prepared deployment target, **not a verified running service on this Mac**. A CUDA Linux container cannot use the M4's Metal GPU through `--gpus`. Docker Desktop documents that this Linux-container GPU passthrough is available on Windows/WSL2; its other model-running mechanisms are separate products and runtimes. [Docker GPU documentation](https://docs.docker.com/desktop/features/gpu/)

## Alternatives and memory planning

| Checkpoint | Tensor payload | Intended target |
| --- | ---: | --- |
| `Qwen/Qwen3-8B-MLX-4bit` | 4.35 GB / 4.05 GiB | This Mac through native Metal |
| `nvidia/Qwen3-8B-NVFP4` | 6.40 GB / 5.96 GiB | Blackwell native FP4; tested CUDA fallback where suitable |
| `Qwen/Qwen3-8B-AWQ` | 6.10 GB / 5.68 GiB | Conservative quantized fallback for older NVIDIA GPUs |
| `Qwen/Qwen3-8B` | 16.38 GB / 15.26 GiB | BF16 on a mostly free 24 GiB CUDA GPU, with bounded context/concurrency |

Payload sizes are recorded in the official safetensors indexes: [MLX](https://huggingface.co/Qwen/Qwen3-8B-MLX-4bit/blob/main/model.safetensors.index.json), [NVFP4](https://huggingface.co/nvidia/Qwen3-8B-NVFP4/blob/main/model.safetensors.index.json), [AWQ](https://huggingface.co/Qwen/Qwen3-8B-AWQ/blob/main/model.safetensors.index.json), [BF16](https://huggingface.co/Qwen/Qwen3-8B/blob/main/model.safetensors.index.json). These are not total RAM/VRAM requirements.

An estimate derived from Qwen3-8B's 36 layers, eight KV heads and 128-dimensional heads gives **144 KiB per token** for a 16-bit K/V cache, before allocator overhead. One fully occupied 4,096-token sequence therefore needs approximately **0.56 GiB** of KV storage; 8,192 tokens need **1.125 GiB**. Runtime buffers and loading peaks need additional room. The initial Mac profile should use one sequence, a short context and small prefill batches, then measure memory pressure and latency. Do not treat all 24 GB of unified memory as free model memory. [Model dimensions](https://huggingface.co/Qwen/Qwen3-8B-AWQ/blob/main/config.json)

Current Metal stable release **0.30.0** pairs with vLLM core **0.30.0**, uses Python 3.12 and MLX **0.32.1**, and replaces `VLLM_METAL_MEMORY_FRACTION` with `--gpu-memory-utilization`. Its normal paged KV cache uses the model dtype; quantized KV settings such as `fp8` or `nvfp4` are rejected. These newer semantics must not be assumed for the existing July build. [Official Metal 0.30.0 release](https://github.com/vllm-project/vllm-metal/releases/tag/v0.30.0)

## Serving and extraction settings

Use the local snapshot path as the model argument, a stable served name such as `apartmint-local`, and an explicit local endpoint. Suggested initial bounds are:

```text
--served-model-name apartmint-local
--max-model-len 4096
--max-num-seqs 1
--max-num-batched-tokens 1024
--reasoning-parser qwen3
--enable-auto-tool-choice
--tool-call-parser hermes
```

For a native host service, bind `--host 127.0.0.1`. For the NVIDIA container, bind its service to the container interface while limiting published access to loopback or a private application network. Supply memory limits according to the actual runtime and available GPU memory. NVIDIA NVFP4 additionally uses `--quantization modelopt_fp4`; the MLX checkpoint does not use that CUDA quantization flag.

The **`hermes` tool-call parser is not the Hermes agent harness** and does not require installing that harness. Qwen's deployment documentation explicitly selects it for Qwen3-8B. For routine listing extraction, disable thinking in the request with `chat_template_kwargs: {"enable_thinking": false}`. Qwen recommends nonthinking sampling parameters of temperature 0.7, top_p 0.8 and top_k 20; task-specific quality still needs evaluation. [Qwen vLLM guide](https://qwen.readthedocs.io/en/latest/deployment/vllm.html)

Use the current structured-output API:

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "apartment_listings",
      "schema": "<the actual JSON Schema object>"
    }
  },
  "chat_template_kwargs": {"enable_thinking": false}
}
```

The placeholder above must be replaced with an object, not sent literally. Validate the response again in application code. Constrained JSON guarantees neither factual accuracy nor correct interpretation of availability. Preserve source URLs and evidence, represent missing values explicitly, and evaluate available, unavailable, ambiguous, stale and adversarial listing examples before unattended use. [vLLM structured-output documentation](https://docs.vllm.ai/en/v0.31.0/features/structured_outputs/)

## Local operation and distribution

The intended boundary is local inference with downloaded assets and no cloud-model fallback. A process that polls apartment websites necessarily has network access, so the whole monitor is not literally air-gapped. The inference service and secret-bearing application components need explicit network and data boundaries; offline download settings alone do not enforce an air gap. Keep credentials out of model prompts whenever normal application code can use them, and never include `.env` files, local databases, caches or logs in a downloadable release.

The listed Qwen and NVIDIA checkpoints are marked **Apache-2.0**. Commercial redistribution is permitted subject to its conditions: include the license, preserve applicable attribution/NOTICE material, identify modified files and avoid implying trademark endorsement. Dependencies and container components have their own licenses. A practical release can ship application code, configuration examples and a pinned model manifest, with an optional fully offline bundle containing separately acquired model/container assets. [Apache-2.0 license](https://www.apache.org/licenses/LICENSE-2.0)

Before claiming a supported release, record the actual runtime, model revision, image digest where applicable, host hardware, successful startup, structured extraction results, failure handling and measured memory. No CUDA-host execution or apartment-quality benchmark is established by this research note.
