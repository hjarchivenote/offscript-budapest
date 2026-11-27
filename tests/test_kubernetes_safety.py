import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import kubernetes_local as local
from scripts.kubernetes_local import ensure_credentials, require_local_server
from scripts.prepare_local import prepare


@pytest.mark.parametrize("server", ["https://cluster.example.com:6443", "https://127.0.0.1.example.com", "http://127.0.0.1:6443"])
def test_local_setup_rejects_remote_or_unsecured_targets(server):
    with pytest.raises(RuntimeError, match="dedicated loopback"):
        require_local_server(server)


def test_existing_secret_mismatch_cannot_overwrite_database_credentials(tmp_path, capsys):
    class Existing:
        def run(self, *args, **kwargs):
            return json.dumps({"data": {"db_password": "existing-private-value"}, "immutable": True})

        def apply(self, *args, **kwargs):
            pytest.fail("An existing credential must never be overwritten")

    with pytest.raises(RuntimeError, match="original credentials"):
        ensure_credentials(Existing(), tmp_path)
    assert "existing-private-value" not in capsys.readouterr().out


def test_matching_credentials_are_reused_without_changes(tmp_path):
    prepare(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    class Existing:
        def run(self, *args, **kwargs):
            data = {key: base64.b64encode(before[name]).decode() for key, name in (("db_password", "db-password"), ("db_admin_password", "admin-password"))}
            return json.dumps({"data": data, "immutable": True})

        def apply(self, *args, **kwargs):
            pytest.fail("Matching credentials need no update")

    ensure_credentials(Existing(), tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_image_preflight_is_isolated_and_checks_actual_image_metadata(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"commit": local.DEFAULT_COMMIT, "version": "0.1.0", "uid": 10001}))

    monkeypatch.setattr(local.subprocess, "run", run)
    assert local.verify_image(local.DEFAULT_IMAGE, local.DEFAULT_COMMIT)["uid"] == 10001
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--rm" in command
    assert local.DEFAULT_IMAGE in command
    assert not any(argument in command for argument in ("--mount", "--volume", "--env", "--env-file"))


@pytest.mark.parametrize("output", [
    {"commit": "0" * 40, "version": "0.1.0", "uid": 10001},
    {"commit": local.DEFAULT_COMMIT, "version": "0.1.0", "uid": 0},
    {"version": "0.1.0", "uid": 10001},
    "not JSON metadata",
])
def test_invalid_image_metadata_is_rejected(monkeypatch, output):
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(output)))
    with pytest.raises(RuntimeError, match="does not match"):
        local.verify_image(local.DEFAULT_IMAGE, local.DEFAULT_COMMIT)


def test_image_timeout_cleans_only_its_own_named_container(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 120)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(local.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="before changing cluster"):
        local.verify_image(local.DEFAULT_IMAGE, local.DEFAULT_COMMIT)
    name = commands[0][commands[0].index("--name") + 1]
    assert name.startswith("offscript-image-check-")
    assert commands[1] == ["docker", "container", "rm", "--force", name]


def test_image_mismatch_stops_before_any_cluster_or_credential_mutation(monkeypatch):
    calls = []

    class Cluster:
        def run(self, *args, **kwargs):
            calls.append(args)
            assert args[:2] == ("config", "view")
            return "https://127.0.0.1:6443"

    def reject(*args):
        raise RuntimeError("Image mismatch")

    monkeypatch.setattr(local, "verify_image", reject)
    monkeypatch.setattr(local, "ensure_credentials", lambda *args: pytest.fail("Credentials must not be touched"))
    with pytest.raises(RuntimeError, match="Image mismatch"):
        local.deploy(Cluster(), local.DEFAULT_IMAGE, "0" * 40)
    assert len(calls) == 1


class Jobs:
    def __init__(self, *, first_attempt_fails=False):
        self.jobs = {}
        self.created = []
        self.first_attempt_fails = first_attempt_fails

    def run(self, *args, **kwargs):
        assert args[:2] == ("get", "job")
        job = self.jobs.get(args[2])
        return json.dumps(job) if job else ""

    def apply(self, job):
        name = job["metadata"]["name"]
        self.created.append(name)
        condition = "Failed" if self.first_attempt_fails and len(self.created) == 1 else "Complete"
        self.jobs[name] = {"metadata": {"name": name, "uid": f"attempt-{len(self.created)}"},
                           "status": {"conditions": [{"type": condition, "status": "True"}]}}


def test_failed_initialization_gets_one_fresh_attempt_on_next_invocation():
    cluster = Jobs(first_attempt_fails=True)
    job = {"metadata": {"name": "offscript-bootstrap-test"}}
    with pytest.raises(RuntimeError, match="Fix the cause and rerun"):
        local.initialize_catalogue(cluster, job)
    failed = json.dumps(cluster.jobs["offscript-bootstrap-test"], sort_keys=True)
    retry = local.initialize_catalogue(cluster, job)
    assert retry.startswith("offscript-bootstrap-test-retry-")
    assert json.dumps(cluster.jobs["offscript-bootstrap-test"], sort_keys=True) == failed
    assert local.initialize_catalogue(cluster, job) == retry
    assert cluster.created == ["offscript-bootstrap-test", retry]
    assert job["metadata"]["name"] == "offscript-bootstrap-test"


def test_active_initialization_resumes_without_recreating_or_deleting(monkeypatch):
    cluster = Jobs()
    cluster.jobs["active"] = {"metadata": {"name": "active", "uid": "same-running-attempt"}, "status": {"active": 1}}

    def finish(seconds):
        cluster.jobs["active"]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}

    monkeypatch.setattr(local.time, "sleep", finish)
    assert local.initialize_catalogue(cluster, {"metadata": {"name": "active"}}) == "active"
    assert not cluster.created
    assert cluster.jobs["active"]["metadata"]["uid"] == "same-running-attempt"


def test_wait_timeout_preserves_the_running_job(monkeypatch):
    cluster = Jobs()
    cluster.jobs["active"] = {"metadata": {"name": "active", "uid": "running"}, "status": {"active": 1}}
    ticks = iter((0, 2))
    monkeypatch.setattr(local.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="active Job is preserved"):
        local.initialize_catalogue(cluster, {"metadata": {"name": "active"}}, timeout=1)
    assert not cluster.created and cluster.jobs["active"]["metadata"]["uid"] == "running"


def test_approval_is_rechecked_after_image_verification_and_before_mutations(monkeypatch):
    calls = []

    class Cluster:
        def run(self, *args, **kwargs):
            assert args[:2] == ("config", "view")
            return "https://127.0.0.1:6443"

    def verified(*args):
        calls.append("image-verified")
        return {"version": "0.1.0"}

    def changed_main():
        calls.append("main-rechecked")
        raise RuntimeError("Main advanced")

    monkeypatch.setattr(local, "verify_image", verified)
    monkeypatch.setattr(local, "ensure_credentials", lambda *args: pytest.fail("No mutation may happen"))
    with pytest.raises(RuntimeError, match="Main advanced"):
        local.deploy(Cluster(), local.DEFAULT_IMAGE, local.DEFAULT_COMMIT, before_apply=changed_main)
    assert calls == ["image-verified", "main-rechecked"]
