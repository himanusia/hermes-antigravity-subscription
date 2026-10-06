import json
import accounts as m


def test_classify_needs_login():
    out = "Authentication required. Please visit the URL to log in:"
    assert m.classify_probe_output(out, "", 1) == m.STATE_NEEDS_LOGIN
    assert m.classify_probe_output("", "You are not logged into Antigravity", 1) == m.STATE_NEEDS_LOGIN


def test_classify_not_eligible():
    err = "error: Eligibility check failed: Your current account is not eligible for Antigravity"
    assert m.classify_probe_output("", err, 1) == m.STATE_NOT_ELIGIBLE


def test_classify_unknown_on_garbage():
    assert m.classify_probe_output("boom", "", 1) == m.STATE_UNKNOWN


def test_login_error_detected():
    assert m.is_login_error("Please sign in to view available models")
    assert not m.is_login_error("Individual quota reached")


def test_needs_login_account_skipped_and_not_eligible_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_ACCOUNTS_FILE", str(tmp_path / "r.json"))
    m.add_account("", str(tmp_path / "a"), email="a@x.com")
    m.add_account("", str(tmp_path / "b"), email="b@x.com")
    m.record_account_state("a@x.com", m.STATE_NEEDS_LOGIN)
    m.record_account_state("b@x.com", m.STATE_NOT_ELIGIBLE)
    accs = {a["label"]: a for a in m.load_accounts()["accounts"]}
    assert not m._account_passes_gates(accs["a@x.com"], 0)
    assert accs["b@x.com"]["eligible"] is False
    m.record_account_state("a@x.com", m.STATE_OK)
    accs = {a["label"]: a for a in m.load_accounts()["accounts"]}
    assert m._account_passes_gates(accs["a@x.com"], 0)
    stored = json.loads((tmp_path / "r.json").read_text())["accounts"]
    assert [a["label"] for a in stored] == ["", ""]
