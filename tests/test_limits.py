"""Account and model limits (Oct 11 2026: Opus hit its weekly limit and killed three sub-agents at once; the second
account's 'You've hit your session limit · resets 3:45pm' had to be waited out by hand)."""
from datetime import datetime, timedelta, timezone

from swarm.adapters.base import parse_reset_at
from swarm.limits import LimitStore, fallback_chain, limited_model, model_family, pick_model, status_line

NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)


def test_parse_reset_reads_a_weekly_date_and_a_session_clock():
    weekly = parse_reset_at("Opus weekly limit reached ∙ resets Oct 14, 9am (UTC)", now=NOW)
    assert weekly == datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    weekly2 = parse_reset_at("limit reached · resets Oct 14 at 9:30pm (UTC)", now=NOW)
    assert weekly2 == datetime(2026, 10, 14, 21, 30, tzinfo=timezone.utc)
    session = parse_reset_at("You've hit your session limit · resets 3:45pm (UTC)", now=NOW)
    assert session == datetime(2026, 10, 11, 15, 45, tzinfo=timezone.utc)


def test_limited_model_names_the_model_only_when_the_limit_is_per_model():
    assert limited_model("Opus weekly limit reached ∙ resets Oct 14, 9am") == "opus"
    assert limited_model("You've reached your Opus limit") == "opus"
    assert limited_model("You've hit your session limit · resets 3:45pm") == ""
    assert limited_model("Claude usage limit reached") == ""
    assert model_family("claude-opus-5-5") == "opus" and model_family("gpt-6-astra") == "gpt-6-astra"


def test_fallback_chain_follows_config_without_cycles():
    fb = {"fable": "opus", "opus": "sonnet", "sonnet": "opus"}
    assert fallback_chain("fable", fb) == ["fable", "opus", "sonnet"]
    assert fallback_chain("haiku", fb) == ["haiku"]


def test_store_marks_expires_and_picks_a_fallback_model(tmp_path):
    st = LimitStore(tmp_path / "limits.json", now=lambda: NOW)
    st.mark("claude-a", NOW + timedelta(days=3), "Opus weekly limit reached", model="opus")
    st.mark("pro", NOW + timedelta(minutes=90), "session limit")
    assert st.until("claude-a", "claude-opus-5-5") == NOW + timedelta(days=3)
    assert st.until("claude-a", "sonnet") is None and st.until("claude-a") is None
    assert st.until("pro", "sonnet") == NOW + timedelta(minutes=90)     # an account limit covers every model
    assert pick_model("claude-a", "opus", {"opus": "sonnet"}, st) == ("sonnet", ["opus"])
    assert pick_model("claude-a", "opus", {}, st) == ("opus", [])        # no fallback: keep it, the run says why
    line = status_line(st)
    assert "pro limited until 15:30 UTC" in line and "claude-a/opus limited until Oct 14 14:00 UTC" in line
    later = LimitStore(tmp_path / "limits.json", now=lambda: NOW + timedelta(hours=2))
    assert later.until("pro") is None and "pro" not in status_line(later)
    later.clear("claude-a", "opus")
    assert later.active() == []
