from pathlib import Path

import pytest

from scripts.build_context import private_context_probes


@pytest.mark.parametrize("build_fails", [False, True])
def test_build_probe_cleanup_preserves_existing_source_and_private_files(tmp_path, build_fails):
    source = tmp_path / "app/main.py"
    source.parent.mkdir()
    source.write_text("original application source")
    private = tmp_path / "app/data/.local/real-developer-file.txt"
    private.parent.mkdir(parents=True)
    private.write_text("existing developer data")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def build():
        with private_context_probes(tmp_path) as probes:
            assert len(probes) == 32
            assert all((tmp_path / Path(path).relative_to("/app")).is_file() for path in probes)
            if build_fails:
                raise RuntimeError("Simulated Docker build failure")

    if build_fails:
        with pytest.raises(RuntimeError, match="Simulated Docker build failure"):
            build()
    else:
        build()
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert not list(tmp_path.rglob("__offscript_ci_probe_*"))
