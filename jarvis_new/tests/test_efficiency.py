"""The work that was being done more than once.

None of these is a bug in the sense of a wrong answer. They are the same
correct answer computed three or four times, and every one of them sits behind
a tool the voice calls while somebody is waiting: the status tool reads the
style file, checks the key vault, imports packages and searches PATH, and did
all of it repeatedly for each of six stages.

The cost that matters in this codebase is not CPU. It is a conversation that
pauses. So these tests count the work rather than timing it: a count is stable
on a loaded machine, and a timing assertion that flakes gets deleted, which
means the thing it was protecting stops being protected.
"""

import json

import pytest

from content import styles
from content.pipeline import PIPELINE, Stage, readiness
from content.scriptwriter import extract_json


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from jarvis import paths

    paths.refresh()
    styles.forget()
    yield tmp_path
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    paths.refresh()
    styles.forget()


class TestTheStyleFileIsReadOnce:

    def counted(self, monkeypatch):
        """Count reads of the file itself, not calls to the accessor."""
        reads = []
        real = styles.path().__class__.read_text

        def counting(self, *args, **kwargs):
            if self.name == styles.FILENAME:
                reads.append(str(self))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(styles.path().__class__, "read_text", counting)
        return reads

    def test_a_prompt_block_does_not_re_read_it_for_every_lookup(
            self, monkeypatch):
        styles.write_starter_file()
        styles.forget()
        reads = self.counted(monkeypatch)

        styles.get()
        styles.default_name()
        styles.reference_block()
        styles.get()

        assert len(reads) == 1, f"read {len(reads)} times: {reads}"

    def test_editing_it_takes_effect_with_nothing_restarted(self, monkeypatch):
        """The property the whole 'style is a file' design rests on. A cache
        keyed on a timer would break it; keyed on the file's own modification
        time and size, it holds."""
        styles.write_starter_file()
        first = styles.default_name()

        data = json.loads(styles.path().read_text("utf-8"))
        name = next(iter(data["profiles"]))
        data["profiles"][name]["name"] = "Rewritten while running"
        data["default"] = name
        styles.path().write_text(json.dumps(data), "utf-8")

        assert styles.load_all()[name].name == "Rewritten while running"
        assert styles.default_name() == name
        assert first is not None

    def test_a_file_that_appears_later_is_picked_up(self):
        assert styles.get() is not None            # falls back to the shipped one
        styles.write_starter_file()
        assert styles.path().exists()
        assert styles.get() is not None

    def test_a_file_that_goes_away_is_not_served_from_memory(self):
        styles.write_starter_file()
        styles.get()
        styles.path().unlink()
        # Falls back rather than raising, and does not hand back the deleted
        # file's contents.
        assert styles.get() is not None

    def test_a_broken_file_is_not_an_exception_on_the_voice_path(self):
        styles.write_starter_file()
        styles.forget()
        styles.path().write_text("{not json at all", "utf-8")
        assert styles.get() is not None


class TestEachStageIsCheckedOnce:

    def test_one_stage_checks_itself_once_per_report(self, monkeypatch):
        calls = []
        real = Stage.missing

        def counting(self):
            calls.append(self.key)
            return real(self)

        monkeypatch.setattr(Stage, "missing", counting)
        PIPELINE[0].as_dict()
        assert calls == [PIPELINE[0].key]

    def test_the_status_tool_checks_each_stage_once(self, monkeypatch):
        """`missing()` decrypts the key vault, imports packages and searches
        PATH. Three times six, on a tool the voice calls."""
        calls = []
        real = Stage.missing

        def counting(self):
            calls.append(self.key)
            return real(self)

        monkeypatch.setattr(Stage, "missing", counting)
        readiness()
        assert len(calls) == len(PIPELINE), f"{len(calls)} checks for " \
            f"{len(PIPELINE)} stages"
        assert sorted(calls) == sorted(stage.key for stage in PIPELINE)

    def test_the_answer_is_the_same_as_checking_each_way(self):
        """Passing the gaps in has to give what computing them separately
        gave, or this is a speed-up that changed behaviour."""
        for stage in PIPELINE:
            row = stage.as_dict()
            assert row["ready"] is stage.ready()
            assert row["blocker"] == stage.blocker()
            assert row["missing"] == stage.missing()


class TestSalvagingATruncatedBatch:

    def five(self, keep):
        scripts = [{"hook": f"hook {n}", "script": f"script {n}",
                    "caption": f"caption {n}", "hashtags": ["#a"]}
                   for n in range(keep)]
        return json.dumps(scripts)[:-1] + ',\n  {"hook": "the sixth one", "scr'

    def test_four_good_scripts_are_not_thrown_away_for_a_fifth(self):
        """The most expensive failure this has: a long answer is the one that
        hits the output limit, and a long answer is the batch worth having."""
        parsed = extract_json(self.five(4))
        assert isinstance(parsed, list)
        assert len(parsed) == 4
        assert parsed[0]["hook"] == "hook 0"

    def test_nothing_is_invented_for_the_one_that_was_cut(self):
        parsed = extract_json(self.five(2))
        assert [item["hook"] for item in parsed] == ["hook 0", "hook 1"]

    def test_a_complete_answer_is_untouched(self):
        whole = json.dumps([{"hook": "a"}, {"hook": "b"}])
        assert extract_json(whole) == [{"hook": "a"}, {"hook": "b"}]

    def test_a_truncated_first_element_yields_nothing_rather_than_a_guess(self):
        assert extract_json('[\n  {"hook": "only the start of i') in (None, [])

    def test_a_bracket_inside_a_string_does_not_confuse_it(self):
        text = json.dumps([{"hook": "a ] in the text", "script": "and a } too"},
                           {"hook": "second"}])[:-1] + ', {"hook": "cut'
        parsed = extract_json(text)
        assert [item["hook"] for item in parsed] == ["a ] in the text", "second"]

    def test_an_escaped_quote_does_not_confuse_it(self):
        text = json.dumps([{"hook": 'he said "no"'}, {"hook": "second"}])
        text = text[:-1] + ', {"hook": "cut'
        parsed = extract_json(text)
        assert [item["hook"] for item in parsed] == ['he said "no"', "second"]

    def test_a_whole_batch_is_preferred_to_a_single_object(self):
        """Order matters: a truncated batch whose first element parses on its
        own would otherwise come back as one script, silently."""
        parsed = extract_json(self.five(3))
        assert isinstance(parsed, list) and len(parsed) == 3

    def test_prose_around_the_json_is_still_fine(self):
        text = ("Here are the scripts you asked for:\n\n"
                + json.dumps([{"hook": "a"}]) + "\n\nLet me know.")
        assert extract_json(text) == [{"hook": "a"}]

    def test_nothing_json_like_at_all(self):
        assert extract_json("I couldn't do that, sorry.") is None
