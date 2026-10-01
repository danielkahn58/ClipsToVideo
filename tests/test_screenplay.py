import pytest

from autocutlib.report import AutocutError
from autocutlib.screenplay import characters, dump_text, load_dialogue, parse_pages
from fixtures import DIALOGUE


def test_parses_dialogue_in_order(screenplay):
    lines = load_dialogue(screenplay)
    assert [(ln.character, ln.text) for ln in lines] == DIALOGUE


def test_drops_parentheticals_contd_and_transitions(screenplay):
    lines = load_dialogue(screenplay)
    text = " ".join(ln.text for ln in lines)
    assert "not looking up" not in text
    assert "CUT TO" not in text
    assert all("CONT" not in ln.character for ln in lines)


def test_characters_in_order_with_counts(screenplay):
    assert characters(load_dialogue(screenplay)) == {"MIKEY": 4, "CLAIRE": 2}


def test_dump_matches_cli_format(screenplay):
    out = dump_text(load_dialogue(screenplay))
    assert out.startswith("Parsed 6 dialogue lines:")
    assert "  2  p1   CLAIRE         I did not touch your coffee." in out


def test_page_filter(screenplay):
    assert len(load_dialogue(screenplay, "1")) == 6
    with pytest.raises(AutocutError, match="selected pages"):
        load_dialogue(screenplay, "2")


def test_parse_pages():
    assert parse_pages("3-4,7") == {3, 4, 7}
    assert parse_pages("") is None
    with pytest.raises(AutocutError):
        parse_pages("three")
