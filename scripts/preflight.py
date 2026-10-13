#!/usr/bin/env python3
"""Read hardware capacity without printing process arguments or environment values."""
import csv
import io
import json
import platform
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
info = {"os": platform.system(), "architecture": platform.machine(), "disk_free_gib": round(shutil.disk_usage(root).free / 1024**3, 2)}
if platform.system() == "Darwin":
    info["unified_memory_gib"] = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 1024**3
    info["cuda_supported"] = False
    info["recommendation"] = "Native vllm-metal with Qwen3-8B-MLX-4bit; CUDA Docker cannot use the Apple GPU."
elif shutil.which("nvidia-smi"):
    output = subprocess.check_output(["nvidia-smi", "--query-gpu=index,name,compute_cap,memory.total,memory.used,memory.free,driver_version", "--format=csv,noheader,nounits"], text=True)
    fields = ["index", "name", "compute_capability", "total_mib", "used_mib", "free_mib", "driver"]
    info["gpus"] = [dict(zip(fields, (x.strip() for x in row))) for row in csv.reader(io.StringIO(output))]
    info["cuda_supported"] = True
    info["recommendation"] = "Choose an idle GPU after checking driver/container compatibility. NVFP4 is native on Blackwell; AWQ is the conservative older-GPU fallback."
else:
    info["cuda_supported"] = False
    info["recommendation"] = "No NVIDIA runtime found; provide a reachable local CUDA host."
try:
    info["docker_server"] = subprocess.check_output(["docker", "version", "--format", "{{.Server.Version}}"], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
except (OSError, subprocess.SubprocessError):
    info["docker_server"] = None
(root / ".runtime").mkdir(exist_ok=True)
(root / ".runtime/hardware.json").write_text(json.dumps(info, indent=2) + "\n")
print(json.dumps(info, indent=2))
