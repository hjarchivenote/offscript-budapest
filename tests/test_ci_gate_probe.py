def test_intentional_ci_failure_blocks_the_container_stage():
    expected_status = "passing"
    observed_status = "intentional_failure"
    assert observed_status == expected_status, "Intentional CI gate verification; this commit must never be released."
