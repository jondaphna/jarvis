from prompts import AGENT_INSTRUCTIONS


def test_it_acts_before_it_talks() -> None:
    """The template shipped an example that taught the opposite - "Of course
    sir, I will now do XYZ" - and the agent duly announced everything before
    doing it."""
    assert "# Act first, talk after" in AGENT_INSTRUCTIONS
    assert "I will now do XYZ" not in AGENT_INSTRUCTIONS


def test_opening_a_site_goes_to_the_users_own_browser() -> None:
    """The browser Jarvis drives is the one holding the user's logins, so
    "open my Google" must go there rather than to a search."""
    assert "use open_url" in AGENT_INSTRUCTIONS
    assert "signed into their accounts" in AGENT_INSTRUCTIONS


def test_it_knows_to_keep_working_inside_a_page() -> None:
    """Opening Spotify and playing something is one flow, not two."""
    assert "type_text and click" in AGENT_INSTRUCTIONS


def test_programs_and_websites_are_told_apart() -> None:
    assert "use open_app" in AGENT_INSTRUCTIONS


def test_general_search_still_falls_back_to_the_web() -> None:
    assert "Only use search_the_web when no website" in AGENT_INSTRUCTIONS
