from naukri_automation.matching import score_job, tokens


def test_score_is_bounded_and_explained():
    score, reason = score_job("Python SQL REST API", "Python backend REST API role", "Backend Engineer")
    assert 0 <= score <= 100
    assert "Shared terms" in reason
    assert "python" in reason


def test_empty_inputs_return_zero():
    assert score_job("", "Python role")[0] == 0
    assert score_job("Python SQL", "")[0] == 0


def test_tokens_remove_common_words():
    assert "python" in tokens("Python and SQL")
    assert "and" not in tokens("Python and SQL")
