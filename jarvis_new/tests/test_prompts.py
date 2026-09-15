from prompts import AGENT_INSTRUCTIONS


def test_it_acts_before_it_talks() -> None:
    """The template shipped an example that taught the opposite - "Of course
    sir, I will now do XYZ" - and the agent duly announced everything before
    doing it."""
    assert "# Act first, talk after" in AGENT_INSTRUCTIONS
    assert "I will now do XYZ" not in AGENT_INSTRUCTIONS


def test_opening_a_site_goes_to_the_users_own_browser() -> None:
    """There are two browsers. Sending "open YouTube" to the automation one
    lands the user in a window signed into nothing."""
    assert "use open_website" in AGENT_INSTRUCTIONS
    assert "signed into nothing" in AGENT_INSTRUCTIONS


def test_programs_and_websites_are_told_apart() -> None:
    assert "use open_app" in AGENT_INSTRUCTIONS


def test_general_search_still_falls_back_to_the_web() -> None:
    assert "Only use search_the_web when no website" in AGENT_INSTRUCTIONS
