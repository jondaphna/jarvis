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
        server, pin, scheme = rt_server.serve(config, port=0, lan=lan)
        port = server.server_address[1]
        return server, pin, port, scheme

    def _get(self, port: int, path: str, scheme: str = "http"):
        import ssl

        # The certificate is self-signed by design, so verification is off here
        # for the same reason a phone has to tap through the warning.
        context = ssl._create_unverified_context() if scheme == "https" else None
        try:
            with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}{path}",
                                        timeout=5, context=context) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def test_local_only_by_default(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=False)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert pin is None
        finally:
            server.shutdown()

    def test_network_exposure_forces_a_pin(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=True)
        try:
            assert server.server_address[0] == "0.0.0.0"
            assert pin and len(pin) == 6
        finally:
            server.shutdown()

    def test_network_exposure_uses_https(self, settings):
        """Phones refuse the microphone on plain http, so LAN mode must be TLS."""
        server, pin, port, scheme = self._serve(settings, lan=True)
        try:
            assert scheme == "https"
            status, body = self._get(port, f"/token?pin={pin}", scheme)
            assert status == 200 and body["token"]
        finally:
            server.shutdown()

    def test_no_token_without_the_pin(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=True)
        try:
            status, body = self._get(port, "/token", scheme)
            assert status == 403
            assert "token" not in body
        finally:
            server.shutdown()

    def test_the_right_pin_works(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=True)
        try:
            status, body = self._get(port, f"/token?pin={pin}", scheme)
            assert status == 200 and body["token"]
        finally:
            server.shutdown()

    def test_guessing_gets_locked_out(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=True)
        try:
            for _ in range(9):
                self._get(port, "/token?pin=000000", scheme)
            # Even the correct code is refused once it has been hammered.
            status, body = self._get(port, f"/token?pin={pin}", scheme)
            assert status == 403
            assert "attempts" in body.get("error", "")
        finally:
            server.shutdown()

    def test_the_api_secret_is_never_served(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=False)
        try:
            _, body = self._get(port, "/token", scheme)
            assert "devsecret0123456789abcdef" not in json.dumps(body)
            _, config_body = self._get(port, "/config", scheme)
            assert "devsecret0123456789abcdef" not in json.dumps(config_body)
        finally:
            server.shutdown()

    def test_unknown_paths_are_refused(self, settings):
        server, pin, port, scheme = self._serve(settings, lan=False)
        try:
            status, _ = self._get(port, "/../../etc/passwd", scheme)
            assert status in (403, 404)
        finally:
            server.shutdown()


class TestLocalServerMode:
    """Running LiveKit on this machine, with no account at all."""

    def test_it_detects_that_nothing_is_running(self):
        from jarvis.realtime import local

        # Nothing is listening on this port in the test environment.
        assert local.is_running(port=7999, timeout=0.2) is False

    def test_it_detects_a_listening_server(self):
        import socket
        import threading

        from jarvis.realtime import local

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        threading.Thread(target=lambda: listener.accept(), daemon=True).start()
        try:
            assert local.is_running(port=port, timeout=1.0) is True
        finally:
            listener.close()

    def test_dev_credentials_are_applied_without_being_stored(self, settings,
                                                              monkeypatch):
        """A publicly known secret must not end up in the encrypted vault."""
        from jarvis.config import Config
        from jarvis.realtime import local, tokens

        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            monkeypatch.delenv(name, raising=False)

        config = Config()
        local.apply(config)

        url, key, secret = tokens.credentials(config)
        assert url == local.DEV_URL and key == local.DEV_KEY
        # Present for this run, but never written to the vault.
        assert "LIVEKIT_API_SECRET" not in config.vault._load()

    def test_the_install_help_names_both_routes(self):
        from jarvis.realtime import local

        help_text = local.install_help()
        assert "winget" in help_text
        assert "github.com/livekit/livekit/releases" in help_text
        assert "--dev" in help_text


class TestPhoneCertificate:
    def test_a_certificate_is_generated_and_reused(self, tmp_path):
        from jarvis.realtime import certs

        cert, key = certs.ensure(tmp_path)
        assert cert.exists() and key.exists()
        again, _ = certs.ensure(tmp_path)
        assert again == cert
        assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")

    def test_it_covers_the_addresses_a_phone_would_use(self, tmp_path):
        """A LAN address missing from the certificate = the phone can't connect."""
        import socket

        from cryptography import x509

        from jarvis.realtime import certs

        cert_path, _ = certs.ensure(tmp_path)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        covered = {str(entry.value) for entry in san}
        assert "localhost" in covered
        assert socket.gethostname() in covered


class TestServerInstall:
    """Fetching and launching the local server."""

    def test_the_download_url_is_the_official_release(self):
        from jarvis.realtime import local

        url = local.download_url()
        assert url.startswith("https://github.com/livekit/livekit/releases/download/")
        assert local.KNOWN_VERSION in url

    def test_the_asset_matches_this_platform(self, monkeypatch):
        from jarvis.realtime import local

        monkeypatch.setattr("sys.platform", "win32")
        asset, binary = local._platform_asset("1.13.7")
        assert asset.endswith(".zip") and "windows" in asset
        assert binary == "livekit-server.exe"

        monkeypatch.setattr("sys.platform", "linux")
        asset, binary = local._platform_asset("1.13.7")
        assert asset.endswith(".tar.gz") and "linux" in asset
        assert binary == "livekit-server"

    def test_it_refuses_a_non_github_source(self, monkeypatch):
        """The URL is built here, never taken from elsewhere - keep it that way."""
        from jarvis.realtime import local

        monkeypatch.setattr(local, "RELEASES", "https://evil.test/releases")
        with pytest.raises(RuntimeError, match="github.com"):
            local.download()

    def test_failures_explain_themselves(self):
        """"It exited immediately" helps nobody; the real reason must surface."""
        from jarvis.realtime import local

        class Stopped:
            def communicate(self, timeout=None):
                return ("listen tcp [::1]:7880: address family not supported "
                        "by protocol", "")

        assert "IPv6" in local._why_it_stopped(Stopped(), None)

        class Busy:
            def communicate(self, timeout=None):
                return ("", "listen tcp :7880: bind: address already in use")

        assert "already" in local._why_it_stopped(Busy(), None)

    def test_a_missing_binary_is_reported_clearly(self, tmp_path, monkeypatch):
        from jarvis.realtime import local

        monkeypatch.setattr(local, "find_binary", lambda: None)
        with pytest.raises(FileNotFoundError, match="isn't installed"):
            local.start()


class TestWorkerStartup:
    """The bugs that made `jarvis realtime` print success and do nothing."""

    def test_the_worker_is_not_run_through_livekits_cli(self):
        """LiveKit's cli.run_app parses sys.argv - it ate our own arguments and
        died with "No such command 'realtime'"."""
        import inspect

        from jarvis.realtime import agent

        source = inspect.getsource(agent.run)
        # The docstring explains why it isn't used, so match the CALL, not
        # the mention.
        assert "cli.run_app(" not in source
        assert "server.run(" in source

    def test_the_coroutine_is_awaited(self):
        """AgentServer.run is async; calling it bare returns a coroutine and
        silently does nothing."""
        import inspect

        from livekit.agents import AgentServer

        from jarvis.realtime import agent

        assert inspect.iscoroutinefunction(AgentServer.run)
        assert "asyncio.run(server.run" in inspect.getsource(agent.run)

    def test_no_agent_name_so_it_joins_the_room(self):
        """Naming the agent switches LiveKit to explicit dispatch, and it then
        never joins the room the browser opened."""
        import inspect

        from jarvis.realtime import agent

        source = inspect.getsource(agent.build_server)
        assert "rtc_session(agent_name" not in source
        assert "@server.rtc_session" in source

    def test_credentials_reach_the_worker_environment(self, settings,
                                                      monkeypatch):
        """The worker reads LiveKit settings from the environment, not the vault."""
        import os

        from jarvis.config import Config
        from jarvis.realtime import agent

        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            monkeypatch.delenv(name, raising=False)

        config = Config()
        config.vault.set("LIVEKIT_URL", "wss://example.livekit.cloud")
        config.vault.set("LIVEKIT_API_KEY", "key123")
        config.vault.set("LIVEKIT_API_SECRET", "secret456789abcdef")

        agent.export_credentials(config)
        assert os.environ["LIVEKIT_URL"] == "wss://example.livekit.cloud"
        assert os.environ["LIVEKIT_API_KEY"] == "key123"

    def test_preflight_reports_an_unreachable_server(self, settings, monkeypatch):
        """Otherwise the worker retries in the background while the terminal
        says everything is fine."""
        from jarvis.config import Config
        from jarvis.realtime import agent

        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            monkeypatch.delenv(name, raising=False)

        config = Config()
        config.vault.set("LIVEKIT_URL", "ws://127.0.0.1:1")   # nothing listens
        config.vault.set("LIVEKIT_API_KEY", "key")
        config.vault.set("LIVEKIT_API_SECRET", "secret")

        trouble = agent.preflight(config)
        assert "Couldn't reach" in trouble
        assert "ws://127.0.0.1:1" in trouble

    def test_preflight_passes_against_a_live_server(self, settings, monkeypatch):
        import http.server
        import threading

        from jarvis.config import Config
        from jarvis.realtime import agent

        class Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
                monkeypatch.delenv(name, raising=False)
            config = Config()
            config.vault.set("LIVEKIT_URL", f"ws://127.0.0.1:{port}")
            config.vault.set("LIVEKIT_API_KEY", "key")
            config.vault.set("LIVEKIT_API_SECRET", "secret")
            assert agent.preflight(config) == ""
        finally:
            server.shutdown()
