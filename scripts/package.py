#!/usr/bin/env python3
"""Build a source tarball from an allowlist, never local secrets or runtime state."""
import hashlib
from pathlib import Path
import tarfile
import tomllib

root = Path(__file__).resolve().parents[1]
dest = root / "dist"
dest.mkdir(exist_ok=True)
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
out = dest / f"apartmint-{version}.tar.gz"
files = ["README.md", "pyproject.toml", "uv.lock", "requirements.lock", "Dockerfile", ".dockerignore", ".gitignore", ".env.example", "compose.yaml", "compose.local.yaml", "config/local-only.sb", "config/sources.example.json", "config/tour-offer.example.json", "config/tour-contact.example.json"]
for directory in ["src", "tests", "scripts", "docs"]:
    files.extend(str(p.relative_to(root)) for p in (root / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
with tarfile.open(out, "w:gz") as archive:
    for rel in sorted(set(files)):
        p = root / rel
        if p.is_symlink() or not p.is_file() or p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example":
            raise SystemExit(f"Unsafe or missing package input: {rel}")
        archive.add(p, arcname=f"apartmint-{version}/{rel}", recursive=False)
digest = hashlib.sha256(out.read_bytes()).hexdigest()
out.with_suffix(out.suffix + ".sha256").write_text(f"{digest}  {out.name}\n")
print(f"Created {out.name}: {out.stat().st_size} bytes; {len(set(files))} files")
