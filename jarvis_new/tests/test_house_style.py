"""The house style file: the form the six reference Reels get described in.

The engine can write scripts today; what it cannot do is write them in his
voice, because nothing in any of these environments can open instagram.com to
see what his references do. So the style is a file with six numbered slots,
each asking eleven specific questions, and the whole of "teach it the style"
is answering them.

What these tests pin is the loop that makes that work without a code change:
answer a slot, and the profile stops calling itself a placeholder, the prompt
grows a section describing the references, and the doctor stops nagging.
"""

import json

import pytest

from content import styles
from content.styles import (
    DESCRIBED_BY,
    REFERENCE_FIELDS,
    REFERENCE_SLOTS,
    REFERENCE_URLS,
    TOP_PRIORITY_SLOTS,
)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from jarvis import paths

    paths.refresh()
    yield tmp_path
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    paths.refresh()


def written():
    return json.loads(styles.path().read_text("utf-8"))


class TestTheFileItself:

    def test_it_is_called_what_he_asked_for(self):
        assert styles.FILENAME == "house_style.json"

    def test_it_lands_in_the_jarvis_folder_beside_the_settings(self, home):
        assert styles.write_starter_file().parent == home

    def test_it_is_written_once_and_not_overwritten(self):
        first = styles.write_starter_file()
        first.write_text('{"default": "mine", "profiles": {}}', encoding="utf-8")
        styles.write_starter_file()
        assert json.loads(first.read_text())["default"] == "mine"

    def test_you_can_ask_for_it_back(self):
        styles.write_starter_file()
        styles.path().write_text("{}", encoding="utf-8")
        styles.write_starter_file(force=True)
        assert written()["references"]

    def test_it_opens_with_instructions_for_the_person_filling_it_in(self):
        styles.write_starter_file()
        assert "Fill in" in written()["_readme"]

    def test_it_is_valid_json_a_person_can_edit(self):
        styles.write_starter_file()
        text = styles.path().read_text("utf-8")
        assert json.loads(text)
        assert "\n" in text          # indented, not one long line


class TestTheSixSlots:

    def test_there_are_six(self):
        styles.write_starter_file()
        assert len(written()["references"]) == REFERENCE_SLOTS == 6

    def test_they_are_numbered(self):
        styles.write_starter_file()
        assert [slot["slot"] for slot in written()["references"]] \
            == [1, 2, 3, 4, 5, 6]

    def test_the_first_two_are_marked_top_priority(self):
        """He said the first two are the exact core execution style."""
        styles.write_starter_file()
        priorities = {slot["slot"]: slot["priority"]
                      for slot in written()["references"]}
        for number in TOP_PRIORITY_SLOTS:
            assert priorities[number] == "top"
        assert priorities[3] == "normal"

    def test_each_slot_already_holds_its_own_reel(self):
        """So it is answering questions about a specific video, not filling in
        a blank form and then working out which one it meant."""
        styles.write_starter_file()
        urls = [slot["url"] for slot in written()["references"]]
        assert urls == list(REFERENCE_URLS)
        assert all(url.startswith("https://www.instagram.com/reel/")
                   for url in urls)

    def test_every_field_carries_its_question(self):
        styles.write_starter_file()
        slot = written()["references"][0]
        for key, _question in REFERENCE_FIELDS:
            assert key in slot
            if key != "url":
                assert slot[key].startswith("(") and slot[key].endswith(")")

    def test_the_questions_are_the_ones_a_scriptwriter_needs(self):
        keys = {key for key, _ in REFERENCE_FIELDS}
        assert {"first_two_seconds", "narration", "on_screen_text", "visuals",
                "cutting", "sound", "subject", "why_it_works", "copy_this",
                "do_not_copy"} <= keys


class TestAnsweringThem:

    def fill(self, slot_number=1, **answers):
        styles.write_starter_file()
        body = written()
        body["references"][slot_number - 1].update(answers)
        styles.path().write_text(json.dumps(body), encoding="utf-8")

    def test_an_unanswered_file_describes_nothing(self):
        styles.write_starter_file()
        assert styles.described() == []
        assert styles.reference_block() == ""

    def test_a_url_on_its_own_is_not_a_description(self):
        """Pasting the link back in tells the scriptwriter nothing at all."""
        self.fill(1, url="https://www.instagram.com/reel/anything/")
        assert styles.described() == []

    @pytest.mark.parametrize("field", DESCRIBED_BY)
    def test_answering_any_of_the_real_questions_counts(self, field):
        self.fill(1, **{field: "It opens on a hard cut with no music."})
        assert len(styles.described()) == 1

    def test_the_profile_stops_calling_itself_a_placeholder(self):
        assert styles.get().placeholder is True
        self.fill(1, why_it_works="The first half second is silent.")
        assert styles.get().placeholder is False

    def test_the_description_reaches_the_prompt(self):
        self.fill(1, first_two_seconds="A hand slams a laptop shut.",
                  copy_this="The silence before the first word.")
        block = styles.get().as_prompt_block()
        assert "The reference Reels" in block
        assert "A hand slams a laptop shut." in block
        assert "The silence before the first word." in block

    def test_the_top_priority_ones_are_marked_in_the_prompt(self):
        self.fill(1, why_it_works="Because of the hook.")
        assert "(top priority)" in styles.reference_block()

    def test_a_normal_one_is_not(self):
        self.fill(3, why_it_works="Because of the pacing.")
        block = styles.reference_block()
        assert "## Reference 3" in block
        assert "(top priority)" not in block

    def test_unanswered_fields_are_left_out_of_the_prompt(self):
        """Otherwise the model is shown six paragraphs of questions in
        brackets and does its best to imitate them."""
        self.fill(1, why_it_works="The hook.")
        block = styles.reference_block()
        assert "(" not in block.replace("(top priority)", "")

    def test_the_url_itself_is_not_put_in_the_prompt(self):
        """Nothing in these environments can open instagram.com, and a model
        given a link it cannot fetch invents what it found there."""
        self.fill(1, why_it_works="The hook.")
        assert "instagram.com" not in styles.reference_block()

    def test_several_answered_slots_all_appear(self):
        styles.write_starter_file()
        body = written()
        for index in range(3):
            body["references"][index]["why_it_works"] = f"Reason {index}"
        styles.path().write_text(json.dumps(body), encoding="utf-8")
        assert len(styles.described()) == 3
        block = styles.reference_block()
        assert all(f"Reason {index}" in block for index in range(3))


class TestItNeverBreaks:

    def test_a_missing_file_is_six_blank_slots(self):
        assert len(styles.references()) == REFERENCE_SLOTS
        assert styles.described() == []

    def test_a_corrupt_file_is_not_an_exception(self):
        styles.path().parent.mkdir(parents=True, exist_ok=True)
        styles.path().write_text("{not json", encoding="utf-8")
        assert styles.references()
        assert styles.reference_block() == ""
        assert styles.get().name

    @pytest.mark.parametrize("body", [
        '{"references": "not a list"}',
        '{"references": [1, 2, 3]}',
        '{"references": []}',
        '[]',
        'null',
    ])
    def test_a_references_key_of_the_wrong_shape(self, body):
        styles.path().parent.mkdir(parents=True, exist_ok=True)
        styles.path().write_text(body, encoding="utf-8")
        assert isinstance(styles.references(), list)
        assert styles.reference_block() == ""

    def test_a_slot_missing_fields_is_read_as_far_as_it_goes(self):
        styles.path().parent.mkdir(parents=True, exist_ok=True)
        styles.path().write_text(
            json.dumps({"references": [{"slot": 1, "why_it_works": "Good hook."}]}),
            encoding="utf-8")
        assert "Good hook." in styles.reference_block()


class TestTheOldFilename:

    def test_an_existing_install_keeps_its_edits(self, home):
        """It used to be called content_styles.json. Somebody who edited that
        must not silently get the shipped guesswork back under a new name."""
        (home / styles.LEGACY_FILENAME).write_text(json.dumps({
            "default": "mine",
            "profiles": {"mine": {"name": "mine", "seconds": 45,
                                  "notes": "my own words"}},
        }), encoding="utf-8")
        styles.write_starter_file()
        body = written()
        assert body["default"] == "mine"
        assert body["profiles"]["mine"]["notes"] == "my own words"
        assert styles.get().seconds == 45

    def test_the_new_slots_are_added_alongside_the_carried_profiles(self, home):
        (home / styles.LEGACY_FILENAME).write_text(json.dumps({
            "default": "mine", "profiles": {"mine": {"name": "mine"}}}),
            encoding="utf-8")
        styles.write_starter_file()
        assert len(written()["references"]) == REFERENCE_SLOTS

    def test_a_corrupt_old_file_does_not_stop_the_new_one(self, home):
        (home / styles.LEGACY_FILENAME).write_text("{broken", encoding="utf-8")
        styles.write_starter_file()
        assert written()["default"] == "reference"
