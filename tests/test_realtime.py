"""The realtime stack: tool bridge, tokens, and the web server's front door.

The LiveKit session itself needs a live server and a microphone, so what's
tested here is everything around it - especially that the permission gate still
applies over a phone connection, and that the web server can't be walked into.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

livekit = pytest.importorskip("livekit.agents",
                              reason="realtime extras not installed")

from jarvis.realtime import bridge, tokens          # noqa: E402
from jarvis.realtime import server as rt_server     # noqa: E402


@pytest.fixture
async def app(workspace):
    from jarvis.config import Config
    from jarvis.core.assistant import Assistant

    config = Config()
    config.settings.set("autonomy.workspace", str(workspace))
    assistant = Assistant(config)
    await assistant.start(with_scheduler=False)
    yield assistant
    assistant.shutdown()


class TestToolBridge:
    async def test_jarvis_tools_become_livekit_tools(self, app):
        tools = bridge.build_tools(app)
        assert tools
        names = {t.info.name for t in tools}
        assert "write_file" in names
        assert "list_files" in names

    async def test_keyboard_tools_are_left_out(self, app):
        """Driving the keyboard from a phone call is not a sensible idea."""
        names = {t.info.name for t in bridge.build_tools(app)}
        assert "type_text" not in names
        assert "press_keys" not in names

    async def test_the_schema_survives_the_trip(self, app):
        tools = bridge.build_tools(app)
        write = next(t for t in tools if t.info.name == "write_file")
        params = write.info.raw_schema["parameters"]
        assert params["type"] == "object"
        assert "path" in params["properties"]
        assert write.info.raw_schema["description"]

    async def test_a_tool_actually_runs(self, app, workspace):
        tools = bridge.build_tools(app)
        write = next(t for t in tools if t.info.name == "write_file")
        result = await write({"path": "from_phone.txt", "content": "hello"},
                             context=None)
        assert "from_phone.txt" in result
        assert (workspace / "from_phone.txt").read_text() == "hello"

    async def test_permissions_still_apply_over_the_phone(self, app):
        """The gate is not bypassed just because the caller is remote."""
        from livekit.agents.llm import ToolError

        tools = bridge.build_tools(app)
        run_command = next(t for t in tools if t.info.name == "run_command")
        with pytest.raises(ToolError) as caught:
            await run_command({"command": "rm -rf /"}, context=None)
        assert "never-run" in str(caught.value).lower()

    async def test_instructions_carry_personality_and_policy(self, app):
        text = bridge.instructions_for(app)
        assert "workspace" in text.lower()
        assert "interrupt" in text.lower()
        assert "permission" in text.lower()

    async def test_standing_commands_reach_the_instructions(self, app):
        from jarvis.core.rules import KIND_INSTRUCT

        app.rules.create("", "always call me sir", kind=KIND_INSTRUCT)
        assert "always call me sir" in bridge.instructions_for(app)


class TestTokens:
    def test_missing_credentials_explain_themselves(self, settings):
        from jarvis.config import Config

        config = Config()
        with pytest.raises(tokens.TokenError) as caught:
            tokens.credentials(config)
        message = str(caught.value)
        assert "LIVEKIT_URL" in message
        assert "cloud.livekit.io" in message

    def test_a_token_is_minted_and_scoped(self, settings):
        from jarvis.config import Config

        config = Config()
        config.vault.set("LIVEKIT_URL", "wss://test.livekit.cloud")
        config.vault.set("LIVEKIT_API_KEY", "devkey")
        config.vault.set("LIVEKIT_API_SECRET", "devsecret0123456789abcdef")

        minted = tokens.mint(config, room="jarvis")
        assert minted["url"] == "wss://test.livekit.cloud"
        assert minted["token"].count(".") == 2          # a JWT

        import base64
        payload = minted["token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        assert claims["video"]["room"] == "jarvis"
        assert claims["video"]["roomJoin"] is True
        # It must not be a master key: no room creation, no admin.
        assert not claims["video"].get("roomAdmin")
        assert not claims["video"].get("roomCreate")

    def test_pins_are_six_digits_and_random(self):
        pins = {tokens.new_pin() for _ in range(50)}
        assert all(len(p) == 6 and p.isdigit() for p in pins)
        assert len(pins) > 40          # not a fixed or sequential value


class TestWebServerSecurity:
    """This opens a socket, so it gets held to a higher standard."""

    def _serve(self, settings, lan: bool):
        from jarvis.config import Config

        config = Config()
        config.vault.set("LIVEKIT_URL", "wss://test.livekit.cloud")
        config.vault.set("LIVEKIT_API_KEY", "devkey")
        config.vault.set("LIVEKIT_API_SECRET", "devsecret0123456789abcdef")
        server, pin = rt_server.serve(config, port=0, lan=lan)
        port = server.server_address[1]
        return server, pin, port

    def _get(self, port: int, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                        timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def test_local_only_by_default(self, settings):
        server, pin, port = self._serve(settings, lan=False)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert pin is None
        finally:
            server.shutdown()

    def test_network_exposure_forces_a_pin(self, settings):
        server, pin, port = self._serve(settings, lan=True)
        try:
            assert server.server_address[0] == "0.0.0.0"
            assert pin and len(pin) == 6
        finally:
            server.shutdown()

    def test_no_token_without_the_pin(self, settings):
        server, pin, port = self._serve(settings, lan=True)
        try:
            status, body = self._get(port, "/token")
            assert status == 403
            assert "token" not in body
        finally:
            server.shutdown()

    def test_the_right_pin_works(self, settings):
        server, pin, port = self._serve(settings, lan=True)
        try:
            status, body = self._get(port, f"/token?pin={pin}")
            assert status == 200 and body["token"]
        finally:
            server.shutdown()

    def test_guessing_gets_locked_out(self, settings):
        server, pin, port = self._serve(settings, lan=True)
        try:
            for _ in range(9):
                self._get(port, "/token?pin=000000")
            # Even the correct code is refused once it has been hammered.
            status, body = self._get(port, f"/token?pin={pin}")
            assert status == 403
            assert "attempts" in body.get("error", "")
        finally:
            server.shutdown()

    def test_the_api_secret_is_never_served(self, settings):
        server, pin, port = self._serve(settings, lan=False)
        try:
            _, body = self._get(port, "/token")
            assert "devsecret0123456789abcdef" not in json.dumps(body)
            _, config_body = self._get(port, "/config")
            assert "devsecret0123456789abcdef" not in json.dumps(config_body)
        finally:
            server.shutdown()

    def test_unknown_paths_are_refused(self, settings):
        server, pin, port = self._serve(settings, lan=False)
        try:
            status, _ = self._get(port, "/../../etc/passwd")
            assert status in (403, 404)
        finally:
            server.shutdown()
