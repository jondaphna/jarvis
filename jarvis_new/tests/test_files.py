"""Finding and reading your own documents, by voice.

"Where's that invoice?" and "what does my business plan say about pricing?"
are the same question asked two ways, so they are one tool: find the files,
then read the best one and quote the part that matters.

The things worth holding still:

* **It stays inside the folders you named.** A voice assistant that can be
  talked into reading `C:\\Users\\you\\.ssh` is not a feature.
* **It finds things by content, not just by filename.** Nobody names a file
  after the sentence they are looking for.
* **It never writes anything.** Reading is safe to do without asking; changing
  your files is not, and this tool is not the place to blur that line.
* **It says what it cannot read.** A PDF it has no library for should produce
  a sentence you can act on, not silence.
"""

import pytest

import files


@pytest.fixture
def workspace(tmp_path):
    """A small pretend filesystem with the shapes that come up in practice."""
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "business plan.md").write_text(
        "# Business plan\n\nWe charge 400 pounds a day for editing.\n"
        "Pricing is reviewed every quarter.\n", encoding="utf-8")
    (docs / "invoice-march.txt").write_text(
        "Invoice 0041\nClient: Acme\nTotal: 1200\n", encoding="utf-8")
    (docs / "shopping.txt").write_text("milk, bread\n", encoding="utf-8")

    junk = docs / "node_modules" / "left-pad"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("pricing pricing pricing", encoding="utf-8")

    (docs / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    return tmp_path


@pytest.fixture
def finder(workspace, monkeypatch) -> files.FileTools:
    tools = files.FileTools()
    monkeypatch.setattr(tools, "roots", lambda: [workspace / "Documents"])
    return tools


class TestFindingThings:
    async def test_it_finds_a_file_by_name(self, finder) -> None:
        answer = await finder.search_my_files._func(finder, None, "invoice march")
        assert "invoice-march.txt" in str(answer)

    async def test_it_finds_a_file_by_what_is_inside_it(self, finder) -> None:
        """Nobody names a file after the sentence they are looking for."""
        answer = await finder.search_my_files._func(finder, None, "400 pounds a day")
        assert "business plan.md" in str(answer)

    async def test_it_reads_the_best_match_back(self, finder) -> None:
        """So "what does my business plan say about pricing" gets an answer
        rather than a file path."""
        answer = await finder.search_my_files._func(finder, None, "pricing")
        assert "reviewed every quarter" in str(answer)

    async def test_the_excerpt_is_near_the_words_you_asked_about(self, finder) -> None:
        long_file = next(iter(finder.roots())) / "notes.md"
        long_file.write_text(
            "padding\n" * 4000 + "the mushroom risotto recipe\n" + "padding\n" * 4000,
            encoding="utf-8")
        answer = str(await finder.search_my_files._func(finder, None, "mushroom risotto"))
        assert "mushroom risotto recipe" in answer

    async def test_nothing_found_says_so_plainly(self, finder) -> None:
        answer = await finder.search_my_files._func(finder, None, "quarterly llama census")
        assert "nothing" in str(answer).lower() or "couldn't find" in str(answer).lower()

    async def test_an_empty_search_is_refused(self, finder) -> None:
        from livekit.agents.llm import ToolError

        with pytest.raises(ToolError):
            await finder.search_my_files._func(finder, None, "   ")

    async def test_it_can_be_pointed_at_one_folder(self, finder, workspace) -> None:
        answer = await finder.search_my_files._func(
            finder, None, "invoice", folder="Documents")
        assert "invoice-march.txt" in str(answer)


class TestWhatItSkips:
    async def test_it_ignores_the_folders_nobody_means(self, finder) -> None:
        """"Find my pricing notes" must not return a copy of left-pad."""
        answer = str(await finder.search_my_files._func(finder, None, "pricing"))
        assert "node_modules" not in answer
        assert "left-pad" not in answer

    async def test_it_does_not_try_to_read_images(self, finder) -> None:
        answer = str(await finder.search_my_files._func(finder, None, "photo"))
        assert "PNG" not in answer

    def test_enormous_files_are_skipped(self, finder, workspace) -> None:
        big = next(iter(finder.roots())) / "huge.log"
        big.write_text("x" * 200, encoding="utf-8")
        assert files.readable(big) is True
        assert files.readable(big, max_bytes=100) is False


class TestItStaysWhereItIsAllowed:
    def test_a_folder_outside_your_roots_is_refused(self, finder, workspace) -> None:
        with pytest.raises(ValueError):
            finder.resolve_folder("/etc")

    def test_climbing_out_with_dot_dot_is_refused(self, finder) -> None:
        """The oldest trick there is, and it costs nothing to close."""
        with pytest.raises(ValueError):
            finder.resolve_folder("../../..")

    def test_a_folder_inside_your_roots_is_allowed(self, finder, workspace) -> None:
        assert finder.resolve_folder("Documents") == workspace / "Documents"

    def test_no_folder_means_everywhere_you_allowed(self, finder, workspace) -> None:
        assert finder.resolve_folder("") is None

    def test_the_default_roots_are_your_obvious_folders(self) -> None:
        """Useful with no setup at all. A tool you have to configure before it
        works is a tool that never gets used. Checked against the list rather
        than the disk, because a build machine has no Desktop."""
        assert set(files.DEFAULT_FOLDER_NAMES) >= {
            "Documents", "Desktop", "Downloads"}

    def test_a_folder_the_machine_does_not_have_is_left_out(self, monkeypatch,
                                                            tmp_path) -> None:
        monkeypatch.setattr(files.Path, "home", staticmethod(lambda: tmp_path))
        (tmp_path / "Documents").mkdir()
        assert [p.name for p in files.default_roots()] == ["Documents"]

    def test_a_symlink_out_of_your_roots_is_not_followed(self, finder,
                                                         workspace) -> None:
        """Confining the folder is not the same as confining the files in it.

        `resolve_folder` refuses a path outside the roots, but the walk used to
        accept anything it found below an allowed folder and read it. A link
        inside Documents pointing at a file outside it was returned by search,
        contents and all.
        """
        outside = workspace / "private"
        outside.mkdir()
        secret = outside / "payslip.txt"
        secret.write_text("salary 98000 pricing pricing pricing",
                          encoding="utf-8")
        (workspace / "Documents" / "innocent.txt").symlink_to(secret)

        assert secret not in finder.walk(None)
        assert not any(path.name == "innocent.txt" for path in finder.walk(None))

    async def test_a_linked_file_does_not_come_back_from_a_search(
            self, finder, workspace) -> None:
        outside = workspace / "private"
        outside.mkdir()
        secret = outside / "payslip.txt"
        secret.write_text("salary 98000 sekritword", encoding="utf-8")
        (workspace / "Documents" / "innocent.txt").symlink_to(secret)

        answer = str(await finder.search_my_files._func(finder, None,
                                                       "sekritword"))
        assert "98000" not in answer
        assert "payslip" not in answer

    def test_an_ordinary_file_in_your_roots_is_still_found(self, finder,
                                                           workspace) -> None:
        """The guard must not cost the normal case."""
        names = {path.name for path in finder.walk(None)}
        assert "business plan.md" in names
        assert "invoice-march.txt" in names


class TestItNeverChangesAnything:
    def test_there_is_no_write_tool_here(self) -> None:
        """Reading your files is safe to do on a spoken word. Changing them is
        not, and this is not the place to blur that."""
        names = {t.info.name for t in files.FileTools().tools}
        for forbidden in ("write", "delete", "move", "rename", "edit"):
            assert not any(forbidden in n for n in names), names

    def test_the_tool_says_it_only_reads(self) -> None:
        doc = " ".join((files.FileTools.search_my_files.__doc__ or "").split())
        assert "read" in doc.lower()

    def test_it_is_governed_by_a_permission_switch(self) -> None:
        import permissions

        assert "search_my_files" in permissions.BY_KEY["files"].tools


class TestItSurvivesAMess:
    async def test_a_root_that_does_not_exist_is_skipped(self, finder, tmp_path) -> None:
        finder.roots = lambda: [tmp_path / "nowhere", *(
            [p for p in [tmp_path / "Documents"] if p.exists()])]
        answer = await finder.search_my_files._func(finder, None, "invoice")
        assert "invoice-march.txt" in str(answer)

    async def test_an_unreadable_file_does_not_stop_the_search(
            self, finder, monkeypatch) -> None:
        real = files.extract_text

        def sometimes(path, *args, **kwargs):
            if path.name.startswith("invoice"):
                raise OSError("permission denied")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(files, "extract_text", sometimes)
        answer = await finder.search_my_files._func(finder, None, "pricing")
        assert "business plan.md" in str(answer)

    def test_a_document_format_it_cannot_read_says_so(self, tmp_path) -> None:
        book = tmp_path / "contract.pdf"
        book.write_bytes(b"%PDF-1.4 not really a pdf")
        text = files.extract_text(book)
        assert text == "" or "pdf" in text.lower()
