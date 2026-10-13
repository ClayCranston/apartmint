"""Execute the package acceptance orchestration with controlled subprocess stubs."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


def _module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "test_tar_deployment.py"
    spec = importlib.util.spec_from_file_location("apartmint_tar_settings_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(tmp_path, member_name="apartmint-fixture/README.md"):
    archive = tmp_path / "apartmint-fixture.tar.gz"
    content = b"Synthetic source fixture; no installation or model calls.\n"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo(member_name)
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    archive.with_suffix(archive.suffix + ".sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    return archive


@pytest.mark.parametrize("private_path", [".env", ".apartmint-install.json", "config/tour-contact.json", "config/tour-offer.json", "models/private/model.bin"])
def test_private_archive_entries_rejected_before_setup(tmp_path, monkeypatch, private_path):
    module = _module()
    archive = _archive(tmp_path, "apartmint-fixture/" + private_path)
    monkeypatch.setattr(sys, "argv", [str(module.__file__), str(archive), "--output", str(tmp_path / "rejected")])
    monkeypatch.setattr(module.shutil, "which", lambda name: "/controlled/bin/uv")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Private archive must not reach setup"))
    with pytest.raises(ValueError, match="Unsafe or private"):
        module.main()


@pytest.mark.parametrize("mode", ["defaults", "environment", "explicit_endpoint", "explicit_all"])
def test_model_settings_reach_extracted_booking_without_inherited_secrets(tmp_path, monkeypatch, capsys, mode):
    module = _module()
    archive = _archive(tmp_path)
    output = tmp_path / "accepted-install"
    for key in ("APARTMINT_MODEL_BASE_URL", "APARTMINT_MODEL", "APARTMINT_MODEL_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PRIVATE_ENV_CANARY", "do-not-forward-private-value")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/import/location")
    monkeypatch.setenv("APARTMINT_DATA_DIR", "/original/customer/data")
    expected = {"APARTMINT_MODEL_BASE_URL": "http://127.0.0.1:11437/v1", "APARTMINT_MODEL": "apartmint-qwen3", "APARTMINT_MODEL_BACKEND": "vllm"}
    if mode != "defaults":
        expected = {"APARTMINT_MODEL_BASE_URL": "http://127.0.0.1:22437/v1", "APARTMINT_MODEL": "custom-housing-alias", "APARTMINT_MODEL_BACKEND": "ollama"}
        for key, value in expected.items():
            monkeypatch.setenv(key, value)
    arguments = [str(module.__file__), str(archive), "--output", str(output), "--city", "Synthetic Settings City"]
    if mode.startswith("explicit"):
        expected["APARTMINT_MODEL_BASE_URL"] = "http://127.0.0.1:33437/v1"
        arguments.extend(["--model-base-url", expected["APARTMINT_MODEL_BASE_URL"]])
    if mode == "explicit_all":
        expected["APARTMINT_MODEL"] = "explicit-model"
        expected["APARTMINT_MODEL_BACKEND"] = "vllm"
        arguments.extend(["--model", expected["APARTMINT_MODEL"], "--model-backend", "vllm"])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/controlled/bin/uv" if name == "uv" else None)
    calls = []

    def assert_environment(kwargs):
        env = kwargs["env"]
        for key, value in expected.items():
            assert env[key] == value
        assert set(env) == {"PATH", "HOME", "PYTHONNOUSERSITE", "APARTMINT_MODEL_BASE_URL", "APARTMINT_MODEL", "APARTMINT_MODEL_BACKEND", "APARTMINT_DATA_DIR"}
        assert env["APARTMINT_DATA_DIR"] == str(kwargs["cwd"] / "data")
        assert "do-not-forward-private-value" not in json.dumps(env)
        assert "/untrusted/import/location" not in json.dumps(env)

    def run(command, **kwargs):
        assert_environment(kwargs)
        calls.append(command)
        if command == ["bash", "scripts/setup.sh"]:
            return subprocess.CompletedProcess(command, 0)
        if command[1:] == ["-m", "pytest", "-q"]:
            assert command[0].endswith("/.venv/bin/python")
            return subprocess.CompletedProcess(command, 0)
        assert command[-3].endswith("/.venv/bin/apartmint")
        assert command[-2:] == ["booking-demo", "Synthetic Settings City"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"real_appointment_booked": False, "restart_check": "passed"}), "")

    def check_output(command, **kwargs):
        assert_environment(kwargs)
        installed = kwargs["cwd"]
        if command[1] == "-c":
            return json.dumps({"prefix": str(installed / ".venv"), "package": str(installed / "src/apartmint/__init__.py")})
        if command == [str(installed / ".venv/bin/apartmint"), "tour", "approval-demo"]:
            return json.dumps({"real_appointment_booked": False, "provider_submissions": 1,
                               "unapproved_submissions": 0, "restart_check": "passed"})
        assert command == [str(installed / ".venv/bin/apartmint"), "demo"]
        return json.dumps({"mode": "offline_fixture"})

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "check_output", check_output)
    module.main()
    assert len(calls) == 3
    report = json.loads((output / "acceptance-report.json").read_text())
    assert report["status"] == "passed"
    assert report["model_base_url"] == expected["APARTMINT_MODEL_BASE_URL"]
    assert report["model"] == expected["APARTMINT_MODEL"]
    assert report["model_backend"] == expected["APARTMINT_MODEL_BACKEND"]
    assert report["offline_tests"] == "passed"
    assert report["approval_demo"]["provider_submissions"] == 1
    assert "do-not-forward-private-value" not in json.dumps(report) + capsys.readouterr().out
