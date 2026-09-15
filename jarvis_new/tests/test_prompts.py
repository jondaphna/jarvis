from prompts import AGENT_INSTRUCTIONS


def test_it_acts_before_it_talks() -> None:
    """The template shipped an example teaching the opposite - "Of course sir,
    I will now do XYZ" - and the agent duly announced everything first."""
    assert "# Act first, talk after" in AGENT_INSTRUCTIONS
    assert "I will now do XYZ" not in AGENT_INSTRUCTIONS


def test_opening_reuses_the_open_tab() -> None:
    """Asking for Spotify and then for a song left two Spotifys open."""
    assert "use open_url" in AGENT_INSTRUCTIONS
    assert "reuses the tab" in AGENT_INSTRUCTIONS


def test_it_knows_to_finish_the_job() -> None:
    """Landing on a search page is not playing the song."""
    assert "search_on_site" in AGENT_INSTRUCTIONS
    assert "not the same as playing the song" in AGENT_INSTRUCTIONS


def test_it_never_handles_a_password() -> None:
    assert "Never type, guess or ask for a password" in AGENT_INSTRUCTIONS


def test_no_stale_tool_names_survive() -> None:
    """Every rename so far has left an orphan telling the model to call
    something that no longer exists."""
    assert "open_website" not in AGENT_INSTRUCTIONS
