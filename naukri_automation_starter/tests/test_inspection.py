import json
import pytest

from naukri_automation.inspection import sanitize_url, verify_profile_refresh_inventory
from naukri_automation.tasks import verify_profile_refresh


def test_sanitize_url_removes_query_and_fragment():
    assert sanitize_url("https://www.naukri.com/mnjuser/profile?id=123&token=secret#resume") == "https://www.naukri.com/mnjuser/profile"


def test_sanitize_url_keeps_path():
    assert sanitize_url("https://www.naukri.com/mnjuser/profile/") == "https://www.naukri.com/mnjuser/profile/"


def test_sanitize_url_removes_url_credentials():
    assert sanitize_url("https://user:password@www.naukri.com/profile?token=secret") == "https://www.naukri.com/profile"


def test_verifier_reports_label_as_candidate_without_selector_or_action():
    result = verify_profile_refresh_inventory({"controls": {
        "buttons": [{"label": "Update Profile", "tag": "button"}],
        "links": [], "inputs": [],
    }})
    assert result["status"] == "candidate_labels_found"
    assert result["evidence"] == [{"kind": "button", "label": "Update Profile", "path": ""}]
    assert result["live_action_enabled"] is False
    assert "unverified candidates" in result["note"]


def test_verifier_does_not_match_unrelated_controls():
    result = verify_profile_refresh_inventory({"controls": {
        "buttons": [{"label": "Save"}], "links": [{"label": "Jobs", "path": "/jobs"}], "inputs": [],
    }})
    assert result["status"] == "no_candidate_labels_found"
    assert result["evidence"] == []


def test_verifier_rejects_missing_controls():
    with pytest.raises(ValueError, match="controls object"):
        verify_profile_refresh_inventory({})


def test_task_reads_inventory_from_file():
    path = __import__("pathlib").Path(".inspection-test.json")
    path.write_text(json.dumps({"controls": {"buttons": [], "links": [], "inputs": []}}), encoding="utf-8")
    try:
        assert verify_profile_refresh(path)["status"] == "no_candidate_labels_found"
    finally:
        path.unlink(missing_ok=True)
