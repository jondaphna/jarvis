"""The things that would be bad, held still.

Jarvis runs as you, drives a browser you are signed into, and can open programs
on your machine. That is a lot of reach for something you talk to, so the limits
on it are worth asserting rather than assuming.
"""

import inspect
from pathlib import Path

import control_api
import live_browser
import os_tools
import permissions
import tools as browser_tools


class TestYourCredentialsStayYours:
    def test_sessions_are_copied_but_never_passwords(self) -> None:
        """Chrome's "Login Data" is its saved-password database and "Web Data"
        is autofill - addresses, card numbers. Neither is needed to stay signed
        in to a site, so copying them would be handing over far more than
        "open my Netflix" asks for."""
        copied = set(live_browser.SESSION_FILES) | set(live_browser.SESSION_FOLDERS)
        assert not copied & set(live_browser.NEVER_COPIED)
        assert "Login Data" in live_browser.NEVER_COPIED
        assert "Web Data" in live_browser.NEVER_COPIED

    def test_the_copy_loop_only_walks_the_allowed_names(self) -> None:
        """A literal filename in the loop would bypass the list above."""
        source = inspect.getsource(live_browser.seed_from_your_chrome)
        body = source.split("return copied")[0]
        for forbidden in live_browser.NEVER_COPIED:
            assert f'"{forbidden}"' not in body

    def test_copying_can_be_turned_off(self) -> None:
        assert "browser.copy_sessions" in inspect.getsource(
            live_browser.seeding_wanted)

    def test_it_never_writes_to_your_own_profile(self) -> None:
        """Read from yours, write to Jarvis's. Never the other way."""
        source = inspect.getsource(live_browser.seed_from_your_chrome)
        for line in source.splitlines():
            if "copy2(" in line or "copytree(" in line:
                assert "destination" in line or "target /" in line, line

    def test_jarvis_browser_is_not_your_everyday_profile(self) -> None:
        """Chrome refuses remote debugging on the default profile, and only one
        Chrome may hold a profile at a time."""
        assert "Google" not in str(live_browser.profile_dir())


class TestTheControlServiceIsLocalOnly:
    def test_it_binds_to_loopback(self) -> None:
        """It can write API keys into the vault and schedule unattended work.
        A page open in another tab must not be able to reach it."""
        source = inspect.getsource(control_api.serve)
        assert '"127.0.0.1"' in source
        assert '"0.0.0.0"' not in source

    def test_it_requires_a_token(self) -> None:
        source = inspect.getsource(control_api._Handler._route)
        assert "_authorised" in source

    def test_the_token_is_compared_in_constant_time(self) -> None:
        assert "compare_digest" in inspect.getsource(
            control_api._Handler._authorised)

    def test_it_never_hands_back_a_stored_key(self) -> None:
        """The interface needs to know whether a key is set, never what it is."""
        source = inspect.getsource(control_api.Control.providers)
        assert "configured" in source
        assert "self.config.key(spec.name)" in source
        assert "bool(" in source, "the value must be reduced to a yes or no"

    def test_a_pasted_command_is_refused_before_storage(self) -> None:
        """This exact mistake cost an evening: a whole command line pasted into
        a key box dies much later inside an HTTP header."""
        source = inspect.getsource(control_api.Control.set_key)
        assert "isspace()" in source


class TestNothingCanRunArbitraryCommands:
    def test_there_is_no_shell_tool(self) -> None:
        """A voice assistant that can be talked into running any command is a
        remote code execution bug with a personality."""
        names = {t.info.name for t in os_tools.OSTools().tools}
        for banned in ("run_command", "shell", "bash", "run_shell", "execute"):
            assert banned not in names

    def test_no_module_shells_out_with_a_string(self) -> None:
        for module in (os_tools, live_browser):
            source = inspect.getsource(module)
            assert "shell=True" not in source
            assert "os.system(" not in source

    def test_nothing_evaluates_text(self) -> None:
        for module in (os_tools, live_browser, control_api, browser_tools):
            source = inspect.getsource(module)
            assert "eval(" not in source
            assert "exec(" not in source


class TestTheBrowserWillNotGoAnywhere:
    def test_only_http_and_https(self) -> None:
        """file:// would read the disk; javascript: would run in the page."""
        from browser import BrowserError, BrowserManager

        for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)",
                    "data:text/html,<script>", "chrome://settings"):
            try:
                BrowserManager._validate_url(bad)
            except BrowserError:
                continue
            raise AssertionError(f"{bad} should have been refused")

    def test_credentials_in_a_url_are_refused(self) -> None:
        from browser import BrowserError, BrowserManager

        try:
            BrowserManager._validate_url("https://user:pass@example.com")
        except BrowserError:
            return
        raise AssertionError("a URL carrying a password should be refused")


class TestDangerousThingsAreOffByDefault:
    def test_power_actions_start_off(self) -> None:
        assert permissions.BY_KEY["power"].default is False

    def test_shutting_down_needs_a_spoken_yes(self) -> None:
        source = inspect.getsource(os_tools.OSTools.power_action._func)
        assert "self._confirmed != wanted" in source

    def test_one_yes_authorises_exactly_one_action(self) -> None:
        """Otherwise a single confirmation would arm every later shutdown."""
        source = inspect.getsource(os_tools.OSTools.power_action._func)
        assert "self._confirmed = None" in source

    def test_switching_a_capability_off_removes_its_tools(self) -> None:
        """Not just tells the model not to. A tool that is still there is one
        persuasive sentence away from being used."""
        source = inspect.getsource(permissions.filter_tools)
        assert "forbidden" in source


class TestSecretsNeverReachDisk:
    def test_no_key_is_committed_anywhere(self) -> None:
        """A key in a tracked file is a key on GitHub."""
        repo = Path(__file__).resolve().parents[2]
        suspicious = []
        for path in list(repo.glob("*.bat")) + list(repo.glob("**/*.md")):
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in ("sk-ant-", "AIzaSy", "APIKEY"):
                if marker in text:
                    suspicious.append(f"{path.name}: {marker}")
        assert not suspicious, suspicious
