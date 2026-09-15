from prompts import AGENT_INSTRUCTIONS


def test_it_acts_before_it_talks() -> None:
    """The template shipped an example that taught the opposite - "Of course
    sir, I will now do XYZ" - and the agent duly announced everything first."""
    assert "# Act first, talk after" in AGENT_INSTRUCTIONS
    assert "I will now do XYZ" not in AGENT_INSTRUCTIONS


def test_opening_goes_to_the_users_own_browser() -> None:
    assert "Use open_url" in AGENT_INSTRUCTIONS
    assert "signed in as them" in AGENT_INSTRUCTIONS


def test_it_knows_how_to_act_inside_a_site() -> None:
    """Playing a song is a link into Spotify's own search, not a robot
    clicking through a page it is not signed into."""
    assert "search_on_site" in AGENT_INSTRUCTIONS
    assert "already signed in" in AGENT_INSTRUCTIONS


def test_the_hidden_browser_is_described_as_hidden() -> None:
    assert "hidden browser" in AGENT_INSTRUCTIONS
    assert "The user never sees them" in AGENT_INSTRUCTIONS


def test_it_never_handles_a_password() -> None:
    assert "Never type, guess or ask for a password" in AGENT_INSTRUCTIONS


def test_no_stale_tool_names_survive() -> None:
    """Every rename so far has left an orphan behind that told the model to
    call something that no longer exists."""
    assert "open_website" not in AGENT_INSTRUCTIONS
