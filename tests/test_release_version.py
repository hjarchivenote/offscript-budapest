import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import publish_image, release_version

COMMIT = "a" * 40
DIGEST = "sha256:" + "2" * 64
OTHER = "sha256:" + "3" * 64
REFERENCE = "ghcr.io/hjarchivenote/offscript-budapest@" + DIGEST
TAG = "ghcr.io/hjarchivenote/offscript-budapest:v1.0.0"


def test_parent_version_is_read_without_executing_source():
    assert release_version.source_version('raise RuntimeError("must not execute")\n__version__ = "0.1.0"') == "0.1.0"


@pytest.mark.parametrize("source", [
    '__version__ = "01.0.0"', '__version__ = "1.0"', '__version__ = 1',
    '__version__ = str("1.0.0")', '__version__ = "1.0.0"\n__version__ = "2.0.0"', 'other = "1.0.0"',
])
def test_ambiguous_or_nonliteral_version_cannot_select_a_release(source):
    with pytest.raises(ValueError, match="one literal"):
        release_version.source_version(source)


@pytest.mark.parametrize(("previous", "expected"), [("0.1.0", "v1.0.0"), ("1.0.0", None)])
def test_only_an_application_version_change_creates_semantic_release(monkeypatch, previous, expected):
    calls = []

    def git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=COMMIT if command[1] == "rev-parse" else f'__version__ = "{previous}"')

    monkeypatch.setattr(release_version, "__version__", "1.0.0")
    monkeypatch.setattr(release_version.subprocess, "run", git)
    assert release_version.new_release_tag(COMMIT) == expected
    assert calls[-1] == ["git", "show", COMMIT + "^:app/__init__.py"]


@pytest.mark.parametrize("fault", ["wrong-checkout", "missing-parent", "downgrade"])
def test_uncertain_history_or_downgrade_blocks_a_release(monkeypatch, fault):
    def git(command, **kwargs):
        if command[1] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="b" * 40 if fault == "wrong-checkout" else COMMIT)
        return SimpleNamespace(returncode=int(fault == "missing-parent"), stdout='__version__ = "2.0.0"')

    monkeypatch.setattr(release_version, "__version__", "1.0.0")
    monkeypatch.setattr(release_version.subprocess, "run", git)
    with pytest.raises((ValueError, RuntimeError)):
        release_version.new_release_tag(COMMIT)


@pytest.mark.parametrize("scenario", ["new", "same", "conflict", "denied", "wrong-push", "wrong-lookup"])
def test_version_alias_is_verified_and_never_overwrites_an_existing_release(scenario):
    calls = []
    pushed = False

    def docker(*args):
        nonlocal pushed
        calls.append(args)
        if args[:2] == ("manifest", "inspect"):
            if scenario == "denied":
                raise subprocess.CalledProcessError(1, args, stderr="unauthorized: access denied")
            if not pushed and scenario not in {"same", "conflict"}:
                raise subprocess.CalledProcessError(1, args, stderr="manifest unknown")
            digest = OTHER if scenario in {"conflict", "wrong-lookup"} else DIGEST
            return json.dumps({"Descriptor": {"digest": digest}})
        if args[:2] == ("image", "push"):
            pushed = True
            return "release: digest: " + (OTHER if scenario == "wrong-push" else DIGEST)
        return ""

    if scenario in {"new", "same"}:
        publish_image.publish_version_tag(docker, REFERENCE, TAG, ())
        assert len(calls) == (4 if scenario == "new" else 1)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            publish_image.publish_version_tag(docker, REFERENCE, TAG, ())
        if scenario in {"conflict", "denied"}:
            assert len(calls) == 1
    assert not any("build" in call for call in calls)


@pytest.mark.parametrize("payload", [[], {"Descriptor": {}}, {"Descriptor": {"digest": "not-a-digest"}}])
def test_unknown_manifest_shape_is_not_assumed_to_be_an_unused_tag(payload):
    with pytest.raises(ValueError):
        publish_image.manifest_digest(lambda *args: json.dumps(payload), TAG, missing_ok=True)
