import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver.decide import (  # noqa: E402
    build_questions,
    is_sensitive,
    narrow,
    parse_decision,
    type_instead,
)


def test_narrow_prefers_goal_words():
    elements = [
        {"id": str(i), "role": "link", "name": f"item {i}", "value": ""}
        for i in range(1, 45)
    ]
    elements.append({"id": "99", "role": "button", "name": "Search flights", "value": ""})
    picked = narrow(elements, "search flights to London")
    assert len(picked) == 30
    assert picked[0]["id"] == "99"


def test_narrow_always_keeps_dialogs_and_fields():
    elements = [
        {"id": str(i), "role": "link", "name": f"job {i}", "value": ""}
        for i in range(20)
    ]
    elements += [
        {
            "id": "field",
            "role": "input",
            "name": "",
            "value": "",
            "form_field": True,
        },
        {
            "id": "dialog",
            "role": "button",
            "name": "Next",
            "value": "",
            "dialog": True,
        },
    ]
    picked = narrow(elements, "unrelated", limit=3)
    assert {item["id"] for item in picked} >= {"field", "dialog"}


def test_questions_include_none_and_fields():
    elements = [
        {"id": "1", "role": "textbox", "name": "To", "value": "", "type": "text"},
        {"id": "2", "role": "button", "name": "Search", "value": "", "type": ""},
        {
            "id": "3",
            "role": "input",
            "name": "Follow",
            "value": "on",
            "type": "checkbox",
            "tag": "input",
            "form_field": True,
        },
        {
            "id": "4",
            "role": "select",
            "name": "Country",
            "value": "Australia",
            "type": "",
            "tag": "select",
            "form_field": True,
        },
    ]
    questions = build_questions(elements)
    assert "TYPE_TEXT" in questions["operation"]["criteria"]
    assert questions["type_target"]["criteria"]["1"].startswith("textbox")
    assert "2" not in questions["type_target"]["criteria"]
    assert "3" not in questions["type_target"]["criteria"]
    assert "4" not in questions["type_target"]["criteria"]
    assert "none" in questions["click_target"]["criteria"]


def test_sensitive_and_parse():
    assert is_sensitive({"type": "password", "name": "secret"})
    assert is_sensitive({"type": "submit", "name": "Place order"})
    assert not is_sensitive({"type": "submit", "name": "Search"})
    parsed = parse_decision(
        {
            "operation": {"choice": "CLICK", "confidence": 0.9},
            "click_target": {"choice": "2"},
            "type_target": {"choice": "none"},
        }
    )
    assert parsed["operation"] == "CLICK"
    assert parsed["click_target"] == "2"
    assert parse_decision({})["operation"] == "BLOCKED"


def test_unrelated_click_becomes_search_text():
    elements = [
        {"id": "1", "role": "link", "name": "Images", "value": "", "type": ""},
        {"id": "2", "role": "combobox", "name": "Search", "value": "", "type": ""},
    ]
    field = type_instead("Type youtube into the Google search box and press Enter", elements[0], elements)
    assert field["id"] == "2"
    assert type_instead("Open Google Images", elements[0], elements) is None
    filled = [
        {"id": "1", "role": "link", "name": "Google apps", "value": "", "type": ""},
        {"id": "2", "role": "combobox", "name": "Search", "value": "youtube", "type": ""},
        {"id": "3", "role": "button", "name": "Google Search", "value": "", "type": "submit"},
    ]
    assert type_instead("Type youtube into the Google search box", filled[0], filled) is None
    assert type_instead("Type youtube into the Google search box", filled[2], filled) is None
