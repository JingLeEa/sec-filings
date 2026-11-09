"""A starter-data rebuild must retain deliberately refreshed financial reports."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundler(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("starter_bundler", ROOT / "scripts/bundle_starter_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    refreshed = json.loads((ROOT / "tests/fixtures/synthesis/AMD_2023_2025_synthesis_output.json").read_text())
    base = deepcopy(refreshed)
    base.update(financial_metrics=[], financial_trends=[])
    original = "test_data/synthesis/amd_2023_2025_synthesis_output.json"

    def git(command, **kwargs):
        if command[1] == "rev-parse":
            return "source-commit\n"
        if command[1] == "ls-tree":
            return original + "\n"
        if command[1] == "log":
            return "source-commit\n"
        assert command == ["git", "show", "source-commit:" + original]
        return json.dumps(base).encode()

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SUPPLEMENTAL", tmp_path / "starter_data/supplemental")
    monkeypatch.setattr(module.subprocess, "check_output", git)
    return module, refreshed, base


@pytest.mark.parametrize("include_supplement", [False, True])
def test_rebuild_preserves_financial_supplement_and_its_provenance(bundler, tmp_path, include_supplement):
    module, refreshed, base = bundler
    if include_supplement:
        module.SUPPLEMENTAL.mkdir(parents=True)
        source = module.SUPPLEMENTAL / "amd_2023_2025_synthesis_output.json"
        source.write_text(json.dumps(refreshed))
    output = tmp_path / "bundle"
    module.bundle("test-data", output)
    with tarfile.open(output / "reports.tar.gz") as archive:
        saved = json.load(archive.extractfile("synthesis/amd_2023_2025_synthesis_output.json"))
    assert saved == (refreshed if include_supplement else base)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["source_commit"] == "source-commit"
    [entry] = manifest["files"]
    assert entry["known_payload_sha256"]
    if include_supplement:
        assert entry["source"] == {
            "kind": "supplemental_synthesis",
            "path": "starter_data/supplemental/amd_2023_2025_synthesis_output.json",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    else:
        assert "source" not in entry


def test_wrong_year_supplement_cannot_replace_the_existing_archive(bundler, tmp_path):
    module, refreshed, _ = bundler
    module.SUPPLEMENTAL.mkdir(parents=True)
    refreshed["years"] = [2024, 2025]
    (module.SUPPLEMENTAL / "amd_2023_2025_synthesis_output.json").write_text(json.dumps(refreshed))
    output = tmp_path / "bundle"
    output.mkdir()
    archive = output / "reports.tar.gz"
    archive.write_bytes(b"existing bundle")
    with pytest.raises(ValueError, match="different company or year"):
        module.bundle("test-data", output)
    assert archive.read_bytes() == b"existing bundle"
