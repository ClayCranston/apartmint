#!/usr/bin/env python3
"""Online provisioning only. Pin a snapshot, verify it, then serve offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PRESETS = {
    "mlx": ("Qwen/Qwen3-8B-MLX-4bit", "383413e909f3bc5303ce195ebbdf0339c5a1a2a3", "qwen3-8b-mlx-4bit"),
    "nvfp4": ("nvidia/Qwen3-8B-NVFP4", "ccd10a893cbca613259517c3efe08e151ddf2b8e", "qwen3-8b-nvfp4"),
    "awq": ("Qwen/Qwen3-8B-AWQ", "4da05a8edb55c6046cce958586c33b61da07bb79", "qwen3-8b-awq"),
}


def digest(path, algorithm):
    h = hashlib.new(algorithm)
    if algorithm == "sha1":
        h.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def matches(path, entry):
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    if "lfs" in entry:
        return digest(path, "sha256") == entry["lfs"]["sha256"]
    return digest(path, "sha1") == entry["blobId"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", choices=PRESETS)
    parser.add_argument("--reuse", type=Path, help="Verify and clone an existing model snapshot")
    parser.add_argument("--verify-only", action="store_true", help="No network; verify saved manifest")
    args = parser.parse_args()
    repo, revision, folder = PRESETS[args.preset]
    target = ROOT / "models" / folder
    manifest = target / "apartmint-model.json"
    if args.verify_only:
        data = json.loads(manifest.read_text())
        for entry in data["files"]:
            if not matches(target / entry["rfilename"], entry):
                raise SystemExit("Model verification failed")
        print(f"Verified {data['repository']} at {data['revision']}")
        return
    if manifest.exists():
        revision = json.loads(manifest.read_text())["revision"]
    url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
    data = json.loads(subprocess.check_output(["curl", "--fail", "--silent", "--show-error", url]))
    revision = data["sha"]
    entries = [e for e in data["siblings"] if e["rfilename"].endswith((".json", ".safetensors", ".txt", ".jinja", ".md", ".model")) or e["rfilename"] == "LICENSE"]
    for entry in entries:
        name = Path(entry["rfilename"])
        if name.is_absolute() or ".." in name.parts:
            raise SystemExit("Unsafe model path")
    if target.is_symlink() or target.parent.is_symlink():
        raise SystemExit("Model destination must not be a symbolic link")
    created_target = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if created_target and (ROOT / ".apartmint-install.json").is_file():
        subprocess.run([sys.executable, str(ROOT / "scripts/manage.py"), "own", str(target.relative_to(ROOT))], check=True)
    required = sum(e["size"] for e in entries if not (target / e["rfilename"]).exists())
    # A clone can share disk blocks, but reserve enough room for the portable copy fallback.
    if shutil.disk_usage(target).free < required + 2 * 1024**3:
        raise SystemExit("Insufficient disk space; retain at least 2 GiB after download")
    for entry in entries:
        name = entry["rfilename"]
        out = target / name
        out.parent.mkdir(parents=True, exist_ok=True)
        if matches(out, entry):
            continue
        cached = args.reuse / name if args.reuse else None
        partial = out.with_name(out.name + ".partial")
        if cached and matches(cached, entry):
            if sys.platform == "darwin":
                subprocess.run(["cp", "-c", str(cached), str(partial)], check=True)
            else:
                shutil.copyfile(cached, partial)
        else:
            subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--silent", "--show-error", "--output", str(partial), f"https://huggingface.co/{repo}/resolve/{revision}/{quote(name)}"], check=True)
        if not matches(partial, entry):
            partial.unlink(missing_ok=True)
            raise SystemExit(f"Integrity verification failed: {name}")
        os.replace(partial, out)
        print(f"Verified {name}", flush=True)
    manifest.write_text(json.dumps({"repository": repo, "revision": revision, "files": entries}, indent=2) + "\n")
    print(f"Ready for offline inference: {target}")


if __name__ == "__main__":
    main()
