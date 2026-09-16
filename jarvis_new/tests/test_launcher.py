"""Opening things is the part that kept breaking, so it gets tests.

Each of these is a real failure that reached the user: a site opening in the
wrong browser, an app reporting success while doing nothing, and a Chrome
profile that was signed into nobody.
"""

import json

import chrome_finder
import pytest

import launcher


class TestSites:
    def test_names_people_actually_say(self) -> None:
        assert launcher.site_url("youtube") == "https://www.youtube.com"
        assert launcher.site_url("netflix") == "https://www.netflix.com"
        assert launcher.site_url("gmail") == "https://mail.google.com"

    def test_my_prefix_is_ignored(self) -> None:
        """"Open my Netflix" is the normal way to say it."""
        assert launcher.site_url("my netflix") == launcher.site_url("netflix")
        assert launcher.site_url("the youtube") == launcher.site_url("youtube")

    def test_bare_domains_and_urls(self) -> None:
        assert launcher.site_url("netflix.com") == "https://netflix.com"
        assert launcher.site_url("https://example.com/x") == "https://example.com/x"

    def test_a_program_is_not_a_website(self) -> None:
        """Otherwise "open task manager" would open a search page."""
        assert launcher.site_url("task manager") is None
        assert launcher.site_url("") is None


class TestApps:
    def test_built_in_windows_programs_resolve(self) -> None:
        assert launcher.resolve_app("notepad") == ("exe", "notepad.exe")
        assert launcher.resolve_app("calculator") == ("exe", "calc.exe")

    def test_something_not_installed_resolves_to_nothing(self) -> None:
        """It must return None rather than a guess. `start whatever` reports
        success even when nothing exists, which is how "opened Spotify" was
        said out loud while nothing happened."""
        assert launcher.resolve_app("definitely not installed xyzzy") is None

    def test_start_menu_shortcuts_are_found(self, tmp_path, monkeypatch) -> None:
        programs = tmp_path / "Programs"
        (programs / "Music").mkdir(parents=True)
        (programs / "Spotify.lnk").write_text("")
        (programs / "Music" / "Spotify Web Helper.lnk").write_text("")
        monkeypatch.setattr(launcher, "_start_menu_dirs", lambda: [programs])

        found = launcher.find_shortcut("spotify")
        assert found is not None
        # The exact name wins over the longer one that merely contains it.
        assert found.stem == "Spotify"

    def test_a_partial_match_prefers_the_shortest_name(self, tmp_path, monkeypatch) -> None:
        programs = tmp_path / "Programs"
        programs.mkdir(parents=True)
        (programs / "Discord Updater.lnk").write_text("")
        (programs / "Discord.lnk").write_text("")
        monkeypatch.setattr(launcher, "_start_menu_dirs", lambda: [programs])
        assert launcher.find_shortcut("discor").stem == "Discord"


class TestBrowserProfile:
    """Which Chrome profile "open my Google" lands in."""

    @pytest.fixture
    def chrome_dir(self, tmp_path, monkeypatch):
        root = tmp_path / "User Data"
        for name in ("Default", "Profile 1", "Profile 2"):
            (root / name).mkdir(parents=True)
        (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Person 1", "user_name": ""},
            "Profile 1": {"name": "Jonathan", "user_name": "me@gmail.com"},
            "Profile 2": {"name": "Work", "user_name": "work@example.com"},
            "Profile 9": {"name": "Deleted", "user_name": "gone@example.com"},
        }}}))
        monkeypatch.setattr(chrome_finder, "user_data_dir", lambda: root)
        return root

    def test_a_signed_in_profile_wins_over_the_default(self, chrome_dir) -> None:
        """The default profile is often the signed-out one, which is how
        "open my Google" showed a logged-out Google."""
        assert chrome_finder.preferred_profile() == "Profile 1"

    def test_an_explicit_choice_is_honoured(self, chrome_dir) -> None:
        assert chrome_finder.preferred_profile("Profile 2") == "Profile 2"

    def test_a_profile_that_no_longer_exists_is_ignored(self, chrome_dir) -> None:
        assert chrome_finder.preferred_profile("Profile 9") == "Profile 1"

    def test_profiles_are_listed_with_names_you_would_recognise(self, chrome_dir) -> None:
        listed = chrome_finder.list_profiles()
        assert {p["name"] for p in listed} == {"Jonathan", "Work", "Person 1"}

    def test_no_chrome_at_all_still_answers(self, monkeypatch) -> None:
        monkeypatch.setattr(chrome_finder, "user_data_dir", lambda: None)
        assert chrome_finder.preferred_profile() == "Default"
        assert chrome_finder.list_profiles() == []


class TestItUnderstandsHowPeopleActuallySpeak:
    """What reaches `site_url` is whatever the model passed through, and the
    model passes through what was said. "Open my Spotify" used to come back as
    "that doesn't look like a website" - for the single most ordinary request
    there is."""

    @pytest.mark.parametrize("said", [
        "spotify", "my spotify", "open my spotify", "Open my Spotify",
        "go to spotify", "open up spotify", "launch spotify",
        "pull up my spotify", "spotify please",
    ])
    def test_every_way_of_asking_for_spotify_works(self, said) -> None:
        assert launcher.site_url(said) == "https://open.spotify.com", said

    @pytest.mark.parametrize("said,expected", [
        ("open netflix", "https://www.netflix.com"),
        ("go to youtube", "https://www.youtube.com"),
        ("my email", "https://mail.google.com"),
        ("the bbc", "https://www.bbc.co.uk"),
        ("open my gmail", "https://mail.google.com"),
    ])
    def test_the_sites_he_asks_for(self, said, expected) -> None:
        assert launcher.site_url(said) == expected

    def test_a_real_url_still_passes_straight_through(self) -> None:
        assert launcher.site_url("https://example.com/thing") == \
            "https://example.com/thing"
        assert launcher.site_url("bbc.co.uk") == "https://bbc.co.uk"

    def test_a_site_it_does_not_know_is_searched_for_rather_than_refused(self) -> None:
        """"Open Shopify" answered "that doesn't look like a website". Landing
        on a search for it is a far better wrong answer than refusing."""
        found = launcher.site_url("shopify")
        assert found and "shopify" in found

    def test_something_that_is_plainly_not_a_website_is_still_refused(self) -> None:
        """Otherwise "open the window" opens a web search for 'the window',
        when what was meant was a program or a real window."""
        for said in ("", "   ", "the window", "task manager",
                     "my printer settings"):
            assert launcher.site_url(said) is None, said
