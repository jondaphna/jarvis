"""Vault, settings, memory and the voice text pipeline."""

from __future__ import annotations

import pytest

from jarvis.config import Settings, Vault
from jarvis.core.voice_out import _clean_for_speech, _sentences, _take_sentence


class TestVault:
    def test_round_trip(self):
        Vault().set("ANTHROPIC_API_KEY", "sk-test-123")
        assert Vault().get("ANTHROPIC_API_KEY") == "sk-test-123"

    def test_keys_are_encrypted_on_disk(self):
        from jarvis import paths
        Vault().set("ANTHROPIC_API_KEY", "sk-super-secret")
        raw = paths.VAULT_FILE.read_bytes()
        assert b"sk-super-secret" not in raw

    def test_environment_wins(self, monkeypatch):
        Vault().set("OPENAI_API_KEY", "stored")
        monkeypatch.setenv("OPENAI_API_KEY", "from-env")
        assert Vault().get("OPENAI_API_KEY") == "from-env"

    def test_wrong_passphrase_is_reported_not_silently_empty(self):
        Vault("correct").set("ANTHROPIC_API_KEY", "sk-1")
        locked = Vault("wrong")
        assert locked.needs_passphrase

    def test_deleting_a_key(self):
        vault = Vault()
        vault.set("EXA_API_KEY", "x")
        vault.delete("EXA_API_KEY")
        assert not vault.get("EXA_API_KEY")

    def test_names_never_leak_values(self):
        Vault().set("TAVILY_API_KEY", "secret-value")
        assert "secret-value" not in str(Vault().names())


class TestSettings:
    def test_defaults_are_present(self):
        settings = Settings.load()
        assert settings.get("autonomy.require_grant_for_high_risk") is True

    def test_dotted_set_and_save(self):
        settings = Settings.load()
        settings.set("voice.wake_word", "computer")
        settings.save()
        assert Settings.load().get("voice.wake_word") == "computer"

    def test_unknown_key_returns_default(self):
        assert Settings.load().get("nope.nothing", "fallback") == "fallback"

    def test_partial_file_is_merged_with_defaults(self):
        from jarvis import paths
        paths.CONFIG_FILE.write_text('{"user_name": "Jon"}', encoding="utf-8")
        settings = Settings.load()
        assert settings.get("user_name") == "Jon"
        assert settings.model_for("voice") == "claude-haiku-4-5"

    def test_corrupt_file_falls_back_to_defaults(self):
        from jarvis import paths
        paths.CONFIG_FILE.write_text("{not json", encoding="utf-8")
        assert Settings.load().get("autonomy.unattended_mode") is True


class TestNothingCostsMoneyUntilYouSaySo:
    """The promise: free out of the box, paid behind one deliberate switch.

    It has to hold for a fresh install AND for a settings file written before
    the default changed, because "a saved value always wins over a default" is
    exactly how a better default fails to reach anyone who already ran setup.
    """

    def test_a_fresh_install_spends_nothing(self):
        settings = Settings.load()
        assert settings.get("thinking.model") == "auto"
        assert settings.allow_paid is False

    def test_a_settings_file_carrying_the_old_paid_default_is_migrated(self):
        from jarvis import paths
        paths.CONFIG_FILE.write_text(
            '{"thinking": {"model": "claude-opus-5", "effort": "high"}}',
            encoding="utf-8")
        settings = Settings.load()
        assert settings.get("thinking.model") == "auto"
        assert settings.allow_paid is False

    def test_a_paid_model_you_chose_on_purpose_is_left_alone(self):
        """Migrating away from a default is right; overruling a decision is
        not. Having switched paid on is what tells the two apart."""
        from jarvis import paths
        paths.CONFIG_FILE.write_text(
            '{"thinking": {"model": "claude-opus-5", "allow_paid": true}}',
            encoding="utf-8")
        settings = Settings.load()
        assert settings.get("thinking.model") == "claude-opus-5"
        assert settings.allow_paid is True

    def test_the_free_brains_come_first_in_the_provider_order(self):
        order = Settings.load().get("provider_order")
        assert order.index("ollama") < order.index("anthropic")
        assert order.index("gemini") < order.index("anthropic")

    def test_the_one_required_key_is_the_free_one(self):
        """Claude used to be required, which meant an assistant that did
        nothing until you had bought credit."""
        from jarvis.config import KEY_SPECS_BY_NAME

        assert KEY_SPECS_BY_NAME["GEMINI_API_KEY"].required is True
        assert KEY_SPECS_BY_NAME["ANTHROPIC_API_KEY"].required is False


class TestMemory:
    def test_conversation_round_trip(self, memory):
        conversation = memory.start_conversation("voice")
        memory.add_message(conversation, "user", "hello")
        memory.add_message(conversation, "assistant", "Good evening.")
        history = memory.history(conversation)
        assert [m.role for m in history] == ["user", "assistant"]

    def test_facts_survive_and_update(self, memory):
        memory.remember("business", "agency")
        memory.remember("business", "video agency")
        assert memory.recall("business") == "video agency"
        assert "video agency" in memory.facts_block()

    def test_spend_accounting(self, memory):
        memory.log_usage("claude-opus-5", 1000, 500, cost_usd=0.02)
        memory.log_usage("claude-haiku-4-5", 500, 200, cost_usd=0.001)
        assert memory.spend_since(24) == pytest.approx(0.021)

    def test_run_history(self, memory):
        run = memory.start_run("m1", "Mission", "manual")
        step = memory.start_step(run, 0, "step", "llm")
        memory.finish_step(step, "ok", {"n": 1})
        memory.finish_run(run, "success", summary="done")
        assert memory.runs("m1")[0]["status"] == "success"
        assert memory.run_steps(run)[0]["status"] == "ok"

    def test_huge_step_output_is_truncated(self, memory):
        run = memory.start_run("m1", "M", "manual")
        step = memory.start_step(run, 0, "s", "llm")
        memory.finish_step(step, "ok", "x" * 50_000)
        assert len(memory.run_steps(run)[0]["output"]) < 25_000


class TestSpeechText:
    def test_sentences_are_grouped_not_fragmented(self):
        chunks = _sentences("Right. I made ten videos. Three failed, so I retried them.")
        assert all(len(c) > 20 for c in chunks)

    def test_streaming_takes_complete_sentences(self):
        head, tail = _take_sentence(
            "Sure. I have generated the clips and merged the audio. Next up is posting.")
        assert head.endswith(".")
        assert "Next up" in tail

    def test_incomplete_text_is_held_back(self):
        head, _ = _take_sentence("I am still writing this sentence")
        assert head is None

    def test_markdown_and_links_are_stripped(self):
        spoken = _clean_for_speech("**Done!** See https://x.com/foo for the _thread_")
        assert "*" not in spoken
        assert "https" not in spoken
        assert "that link" in spoken
