"""Scan upload sources for credentials, keeping secret matches out of artifacts."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.scan_image import TRIVY_IMAGE
from scripts.source_files import ROOT, source_files


def scan_source(output: Path):
    reports = output.resolve() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report = reports / "source-secrets.json"
    report.unlink(missing_ok=True)
    with TemporaryDirectory(prefix="source-scan-", dir=output.resolve()) as scratch:
        scratch = Path(scratch)
        snapshot, private = scratch / "source", scratch / "private"
        snapshot.mkdir()
        private.mkdir()
        files = source_files()
        for relative in files:
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        result = subprocess.run([
            "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--mount", f"type=bind,src={snapshot},dst=/source,readonly",
            "--mount", f"type=bind,src={private},dst=/private",
            TRIVY_IMAGE, "fs", "--scanners", "secret", "--exit-code", "1",
            "--format", "json", "--output", "/private/raw.json", "--ignorefile", "",
            "--secret-config", "", "--cache-dir", "/tmp/cache", "--skip-version-check", "/source",
        ], check=False, capture_output=True)
        raw = private / "raw.json"
        if result.returncode not in (0, 1) or not raw.is_file():
            raise RuntimeError("Source secret scanning failed to run; upload validation is blocked.")
        data = json.loads(raw.read_text())
        if data.get("SchemaVersion") != 2 or data.get("ArtifactType") != "filesystem" or not isinstance(data.get("Results", []), list):
            raise RuntimeError("Source secret scan returned an invalid report.")
        findings = [
            {"file": entry.get("Target"), "rule": finding.get("RuleID"), "severity": finding.get("Severity"),
             "start_line": finding.get("StartLine"), "end_line": finding.get("EndLine")}
            for entry in data.get("Results", []) for finding in entry.get("Secrets", [])
        ]
        # Never retain Match, Code, or scanner output, which may contain credentials.
        report.write_text(json.dumps({"files_scanned": len(files), "findings": findings}, indent=2) + "\n")
        if result.returncode or findings:
            raise RuntimeError("Potential credentials found. See the sanitized source scan for file and line only.")
    print(f"Source secret scan passed for {len(files)} upload files.", flush=True)
