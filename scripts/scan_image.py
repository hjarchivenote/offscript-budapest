"""Scan an image archive with a pinned Trivy image, without a Docker socket."""

import argparse
import json
import os
import subprocess
from pathlib import Path

TRIVY_IMAGE = "aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"


def scan_image(image: str, output: Path):
    output = output.resolve()
    reports = output / "reports"
    cache = output.parent / "trivy-cache"
    reports.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    archive = output / "image.tar"
    report = reports / "security.json"
    report.unlink(missing_ok=True)
    subprocess.run(["docker", "image", "save", "--output", str(archive), image], check=True)
    result = subprocess.run([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=2g",
        "--mount", f"type=bind,src={archive},dst=/scan/image.tar,readonly",
        "--mount", f"type=bind,src={reports},dst=/reports",
        "--mount", f"type=bind,src={cache},dst=/cache",
        TRIVY_IMAGE, "image", "--input", "/scan/image.tar", "--cache-dir", "/cache",
        "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--exit-code", "1",
        "--format", "json", "--output", "/reports/security.json", "--ignorefile", "",
        "--no-progress", "--skip-version-check", "--timeout", "5m",
    ], check=False)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, ["trivy", "image", image])
    data = json.loads(report.read_text())
    classes = {r.get("Class") for r in data.get("Results", [])}
    if not {"os-pkgs", "lang-pkgs"} <= classes:
        raise RuntimeError("The security scan did not cover both OS and application packages.")
    findings = [v for r in data["Results"] for v in r.get("Vulnerabilities", []) if v.get("Severity") in {"HIGH", "CRITICAL"}]
    if findings:
        raise RuntimeError("High or critical vulnerabilities block the image.")
    print("Security scan passed for OS and application packages.", flush=True)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, default=Path(".local/ci"))
    args = parser.parse_args()
    scan_image(args.image, args.output)
