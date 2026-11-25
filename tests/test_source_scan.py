import json
from types import SimpleNamespace

import pytest

from scripts import scan_source, source_files


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("print('hello')\n")
    monkeypatch.setattr(scan_source, "ROOT", root)
    monkeypatch.setattr(scan_source, "source_files", lambda: [scan_source.Path("source.py")])
    output = tmp_path / "output"
    (output / "reports").mkdir(parents=True)
    report = output / "reports/source-secrets.json"
    report.write_text('{"findings": []}')
    return output, report


def fake_scanner(monkeypatch, data, returncode):
    def run(command, **kwargs):
        mount = next(value for value in command if value.startswith("type=bind,src=") and value.endswith("dst=/private"))
        private = scan_source.Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/private"))
        if data is not None:
            (private / "raw.json").write_text(json.dumps(data))
        return SimpleNamespace(returncode=returncode, stdout=b"secret", stderr=b"secret")

    monkeypatch.setattr(scan_source.subprocess, "run", run)


def test_secret_scan_failure_retains_locations_without_leaking_matches(scanner, monkeypatch):
    output, report = scanner
    fake_scanner(monkeypatch, {"SchemaVersion": 2, "ArtifactType": "filesystem", "Results": [{"Target": "source.py", "Secrets": [{"RuleID": "token", "Severity": "HIGH", "StartLine": 3, "Match": "do-not-retain-this", "Code": {"Lines": [{"Content": "private"}]}}]}]}, 1)
    with pytest.raises(RuntimeError, match="Potential credentials"):
        scan_source.scan_source(output)
    assert json.loads(report.read_text())["findings"][0]["start_line"] == 3
    assert "do-not-retain-this" not in report.read_text() and "private" not in report.read_text()
    assert not list(output.glob("source-scan-*"))


@pytest.mark.parametrize(("data", "code"), [(None, 2), ({}, 0), ({"SchemaVersion": 2, "ArtifactType": "container_image", "Results": []}, 0)])
def test_incomplete_secret_scan_never_leaves_an_old_success_report(scanner, monkeypatch, data, code):
    output, report = scanner
    fake_scanner(monkeypatch, data, code)
    with pytest.raises(RuntimeError):
        scan_source.scan_source(output)
    assert not report.exists()


@pytest.mark.parametrize("filename", [".env.production", "customers.db-wal", "customers.sqlite3", ".local/password"])
def test_forced_tracked_private_files_cannot_enter_the_upload_snapshot(tmp_path, monkeypatch, filename):
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private content")
    monkeypatch.setattr(source_files.subprocess, "check_output", lambda *args, **kwargs: filename.encode() + b"\0")
    with pytest.raises(RuntimeError, match="Private or generated"):
        source_files.source_files(tmp_path)
