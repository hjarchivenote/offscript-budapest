"""Temporary negative control; this file must never be merged into main."""

import pytest


def test_intentional_failure_blocks_image_publication():
    pytest.fail("Intentional failure to verify publication blocking; remove only on the verification branch.")
