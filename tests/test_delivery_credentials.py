import os
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from scripts import delivery
from tests.test_delivery import COMMIT, REFERENCE, RUN_ID, approved

OLD = "expired-test-credential"
NEW = "replacement-test-credential"


@pytest.fixture
def credential_file(tmp_path, monkeypatch):
    path = tmp_path / "private" / "github-token"
    monkeypatch.delenv("OFFSCRIPT_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("OFFSCRIPT_GITHUB_TOKEN_FILE", str(path))
    return path


def replace_token(path, value):
    path.parent.mkdir(exist_ok=True)
    replacement = path.with_name("new-token")
    replacement.write_text(value)
    replacement.chmod(0o600)
    replacement.replace(path)


def client(handler, *, provider=None):
    github = delivery.GitHub(token_provider=provider)
    github.api.close()
    github.api = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return github


def test_client_does_not_require_credentials_until_a_request():
    def unavailable():
        pytest.fail("Constructing a worker must not read a credential or exit")

    github = delivery.GitHub(token_provider=unavailable)
    assert "authorization" not in github.api.headers
    github.close()


@pytest.mark.parametrize("fault", ["missing", "public-mode", "symlink", "fifo", "owner", "empty", "binary", "oversized", "header-injection"])
def test_unsafe_missing_or_malformed_token_files_never_reach_github(credential_file, monkeypatch, fault):
    path = credential_file
    replace_token(path, NEW)
    if fault == "missing":
        path.unlink()
    elif fault == "public-mode":
        path.chmod(0o644)
    elif fault == "symlink":
        path.rename(path.with_suffix(".real"))
        path.symlink_to(path.with_suffix(".real"))
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif fault == "owner":
        monkeypatch.setattr(delivery.os, "getuid", lambda: -1)
    elif fault == "empty":
        path.write_text("")
    elif fault == "binary":
        path.write_bytes(b"\xff")
    elif fault == "oversized":
        path.write_text("a" * 4096 + "\nadditional-data")
    else:
        path.write_text(NEW + "\nAuthorization: other")
    github = client(lambda request: pytest.fail("Unsafe credentials must not send HTTP requests"))
    try:
        with pytest.raises(delivery.AuthenticationRequired, match="authentication"):
            github.head()
    finally:
        github.close()


def test_keychain_timeout_is_reported_without_subprocess_output(monkeypatch, tmp_path):
    monkeypatch.delenv("OFFSCRIPT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OFFSCRIPT_GITHUB_TOKEN_FILE", raising=False)
    monkeypatch.setattr(delivery, "token_path", lambda: tmp_path / "absent")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("credential", 15, output=OLD)

    monkeypatch.setattr(delivery.subprocess, "run", timeout)
    with pytest.raises(delivery.AuthenticationRequired) as error:
        delivery.git_read_token()
    assert OLD not in str(error.value)


def test_every_request_reads_a_replaced_credential_without_restarting_client(credential_file):
    replace_token(credential_file, OLD)
    seen = []

    def api(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"commit": {"sha": COMMIT}})

    github = client(api)
    try:
        assert github.head() == COMMIT
        replace_token(credential_file, NEW)
        assert github.head() == COMMIT
        assert seen == ["Bearer " + OLD, "Bearer " + NEW]
        assert "authorization" not in github.api.headers
    finally:
        github.close()


def test_401_rereads_rotated_credential_and_retries_original_request_once(credential_file):
    replace_token(credential_file, OLD)
    seen = []

    def api(request):
        seen.append(request)
        if len(seen) == 1:
            replace_token(credential_file, NEW)
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer " + NEW
        return httpx.Response(200, json={"workflow_runs": []})

    github = client(api)
    try:
        assert github.latest_run(COMMIT) is None
        assert len(seen) == 2 and seen[0].url == seen[1].url
        assert seen[1].url.params["head_sha"] == COMMIT
    finally:
        github.close()


@pytest.mark.parametrize("changed", [False, True])
def test_rejected_replacement_cannot_create_an_unbounded_retry_loop(changed):
    tokens = iter([OLD, NEW if changed else OLD])
    seen = []
    github = client(lambda request: (seen.append(request) or httpx.Response(401)), provider=lambda: next(tokens))
    try:
        with pytest.raises(delivery.AuthenticationRequired) as error:
            github.head()
        assert len(seen) == (2 if changed else 1)
        assert OLD not in str(error.value) and NEW not in str(error.value)
    finally:
        github.close()


def test_refresh_provider_failure_never_logs_its_sensitive_exception():
    reads = []

    def provider():
        reads.append(True)
        if len(reads) > 1:
            raise ValueError(NEW)
        return OLD

    github = client(lambda request: httpx.Response(401), provider=provider)
    try:
        with pytest.raises(delivery.AuthenticationRequired) as error:
            github.head()
        assert len(reads) == 2 and OLD not in str(error.value) and NEW not in str(error.value)
    finally:
        github.close()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_other_failures_do_not_blindly_refresh_or_retry(status):
    reads = []
    seen = []
    github = client(lambda r: (seen.append(r) or httpx.Response(status)),
                    provider=lambda: (reads.append(True) or OLD))
    try:
        with pytest.raises(delivery.DeliveryError, match=str(status)):
            github.head()
        assert len(reads) == len(seen) == 1
    finally:
        github.close()


def test_refreshed_archive_request_keeps_credentials_out_of_storage(credential_file):
    replace_token(credential_file, OLD)
    api_calls = []
    storage_calls = []

    def api(request):
        api_calls.append(request)
        if len(api_calls) == 1:
            replace_token(credential_file, NEW)
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer " + NEW
        return httpx.Response(302, headers={"location": "https://results.blob.core.windows.net/artifact"})

    def storage(request):
        storage_calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"checked ZIP")

    github = client(api)
    github.download.close()
    github.download = httpx.Client(transport=httpx.MockTransport(storage))
    try:
        assert github.archive({"id": 5}) == b"checked ZIP"
        assert len(api_calls) == 2 and len(storage_calls) == 1
    finally:
        github.close()


def test_missing_and_expired_credentials_recover_in_same_controller_without_mutation(credential_file, monkeypatch, tmp_path, capsys):
    seen = []

    def api(request):
        seen.append(request)
        if request.headers["authorization"] != "Bearer " + NEW:
            return httpx.Response(401)
        if request.url.path.endswith("branches/main"):
            return httpx.Response(200, json={"commit": {"sha": COMMIT}})
        return httpx.Response(200, json={"workflow_runs": [approved()]})

    github = client(api)
    folder = tmp_path / "worker"
    controller = delivery.Controller(github, "kubectl", tmp_path / "private-config", folder)
    previous = {"commit": COMMIT, "image": REFERENCE, "run_id": RUN_ID, "run_attempt": 1}
    controller.state["last_success"] = previous
    monkeypatch.setattr(delivery, "Kubernetes", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(delivery, "application_matches", lambda *a: True)
    monkeypatch.setattr(delivery, "deploy", lambda *a, **k: pytest.fail("Authentication checks must preserve the deployment"))
    try:
        assert controller.step()["status"] == "authentication-required" and not seen
        replace_token(credential_file, OLD)
        assert controller.step()["status"] == "authentication-required" and len(seen) == 1
        replace_token(credential_file, NEW)
        result = controller.step()
        assert result["status"] == "up-to-date" and result["failure_count"] == 0
        assert result["last_success"] == previous
        logs = (folder / "state.json").read_text() + (folder / "events.jsonl").read_text() + capsys.readouterr().out
        assert OLD not in logs and NEW not in logs
    finally:
        github.close()


def test_refresh_during_final_main_check_cannot_authorize_a_stale_release(credential_file):
    replace_token(credential_file, OLD)
    seen = []

    def api(request):
        seen.append(request)
        if request.url.path.endswith("/" + str(RUN_ID)):
            return httpx.Response(200, json=approved())
        if request.headers["authorization"] == "Bearer " + OLD:
            replace_token(credential_file, NEW)
            return httpx.Response(401)
        return httpx.Response(200, json={"commit": {"sha": "b" * 40}})

    github = client(api)
    try:
        with pytest.raises(delivery.DeliveryError, match="changed"):
            github.require_current(approved())
        assert len(seen) == 3
    finally:
        github.close()


def test_network_failure_backoff_prevents_repeated_head_requests(monkeypatch, tmp_path):
    seen = []
    github = client(lambda r: (seen.append(r) or httpx.Response(500)), provider=lambda: OLD)
    controller = delivery.Controller(github, "kubectl", tmp_path / "private-config", tmp_path / "worker")
    monkeypatch.setattr(delivery.time, "time", lambda: 1000)
    try:
        assert controller.step()["status"] == "deployment-failed" and len(seen) == 1
        assert controller.step()["status"] == "deployment-failed" and len(seen) == 1
        monkeypatch.setattr(delivery.time, "time", lambda: 1015)
        assert controller.step()["status"] == "deployment-failed" and len(seen) == 2
    finally:
        github.close()


@pytest.mark.parametrize("fault", ["none", "read-rejected", "write-failed"])
def test_hidden_configuration_saves_only_verified_token_atomically(credential_file, monkeypatch, capsys, fault):
    replace_token(credential_file, OLD)
    monkeypatch.setattr(delivery.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(delivery.getpass, "getpass", lambda prompt: NEW)

    class Probe:
        def __init__(self, token):
            assert token == NEW

        def head(self):
            if fault == "read-rejected":
                raise delivery.AuthenticationRequired(delivery.AUTHENTICATION_MESSAGE)
            return COMMIT

        def latest_run(self, commit):
            assert credential_file.read_text() == OLD
            return None

        def close(self):
            pass

    monkeypatch.setattr(delivery, "GitHub", Probe)
    if fault != "none":
        if fault == "write-failed":
            def fail_replace(*args):
                raise PermissionError

            monkeypatch.setattr(delivery.os, "replace", fail_replace)
        with pytest.raises(delivery.AuthenticationRequired if fault == "read-rejected" else OSError):
            delivery.configure_token()
        assert credential_file.read_text() == OLD
    else:
        delivery.configure_token()
        assert credential_file.read_text() == NEW + "\n"
        assert credential_file.stat().st_mode & 0o777 == 0o600
    assert not list(credential_file.parent.glob(".github-token-*"))
    assert NEW not in capsys.readouterr().out


def test_noninteractive_configuration_never_prompts_or_saves(credential_file, monkeypatch):
    monkeypatch.setattr(delivery.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(delivery.getpass, "getpass", lambda prompt: pytest.fail("A hidden terminal prompt is required"))
    with pytest.raises(delivery.DeliveryError, match="interactive"):
        delivery.configure_token()
    assert not credential_file.exists()
