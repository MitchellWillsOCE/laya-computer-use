"""Turn a page's controls into one Laya decision."""

from __future__ import annotations

import os
import re

LIMIT = int(os.environ.get("LAYA_ELEMENT_LIMIT", "30"))
_GENERIC_WORDS = {
    "the", "and", "for", "into", "then", "click", "type", "open", "page",
    "google", "search", "press", "enter", "with", "from", "that", "this",
    "box", "result", "link", "you", "your",
}
FIELD_ROLES = {"input", "textarea", "textbox", "searchbox", "combobox"}
FIELD_TYPES = {"", "text", "search", "email", "tel", "url", "number"}
NON_TEXT_INPUT_TYPES = {
    "button", "checkbox", "color", "file", "hidden", "image", "radio",
    "range", "reset", "submit",
}
SENSITIVE = re.compile(
    r"password|passcode|cvv|cvc|card number|pay now|place order|"
    r"purchase|checkout|delete|remove account|transfer money",
    re.IGNORECASE,
)

OPERATIONS = {
    "CLICK": "activate one listed control",
    "TYPE_TEXT": "a listed field needs text from the driver before anything else happens",
    "PRESS_ENTER": "submit what is already on screen",
    "SELECT": "choose an option in a select field",
    "CHECK": "check a checkbox",
    "UNCHECK": "uncheck a checkbox",
    "READ": "read page, dialog, or element text",
    "SCROLL": "the needed control is below the current view",
    "WAIT": "the page is still loading",
    "DONE": "the goal is already achieved on this page",
    "BLOCKED": "a login, captcha, or missing control stops progress",
}


def narrow(elements: list[dict], goal: str, limit: int = LIMIT) -> list[dict]:
    limit = max(1, int(limit))
    if len(elements) <= limit:
        return elements
    words = goal_words(goal)

    def score(el: dict) -> int:
        haystack = " ".join(
            str(el.get(key) or "").lower()
            for key in ("name", "value", "selected", "error", "href")
        )
        overlap = sum(1 for word in words if word in haystack)
        role = el.get("role")
        bonus = 8 if el.get("form_field") or is_field(el) else 0
        bonus += 6 if el.get("dialog") else 0
        bonus += 4 if role in {"listitem", "article", "li"} else 0
        bonus += 4 if el.get("scrollable") else 0
        bonus += 3 if role == "button" else 0
        bonus += 3 if el.get("invalid") or el.get("error") else 0
        return overlap * 10 + bonus

    mandatory = [
        element
        for element in elements
        if element.get("dialog") or element.get("form_field") or is_field(element)
    ]
    mandatory_ids = {element.get("id") for element in mandatory}
    ranked = sorted(
        (element for element in elements if element.get("id") not in mandatory_ids),
        key=score,
        reverse=True,
    )
    return mandatory + ranked[: max(0, limit - len(mandatory))]


def is_sensitive(element: dict) -> bool:
    if (element.get("type") or "").lower() == "password":
        return True
    return bool(SENSITIVE.search(element.get("name") or ""))


def _label(element: dict) -> str:
    label = f'{element["role"]} "{element.get("name") or ""}"'
    if element.get("value"):
        label += f' current value "{element["value"]}"'
    if element.get("href"):
        label += f' href "{element["href"]}"'
    if element.get("checked") is not None:
        label += f' checked={element["checked"]}'
    if element.get("selected"):
        label += f' selected "{element["selected"]}"'
    for flag in ("required", "disabled", "invalid"):
        if element.get(flag):
            label += f" {flag}=true"
    if element.get("error"):
        label += f' error "{element["error"]}"'
    return label


def goal_words(goal: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]{3,}", (goal or "").lower()))
    return words - _GENERIC_WORDS


def name_matches_goal(goal: str, name: str) -> bool:
    words = goal_words(goal)
    if not words:
        return True
    folded = (name or "").lower()
    return any(word in folded for word in words)


def search_field(elements: list[dict]) -> dict | None:
    for element in elements:
        if is_text_field(element) and re.search(r"search", element.get("name") or "", re.IGNORECASE):
            return element
    return None


def is_submit(element: dict) -> bool:
    name = element.get("name") or ""
    role = element.get("role") or ""
    control_type = (element.get("type") or "").lower()
    if control_type == "submit":
        return True
    if role in {"button", "submit", "input"} and re.search(r"search|submit|\bgo\b", name, re.IGNORECASE):
        return True
    return False


def type_instead(goal: str, element: dict, elements: list[dict]) -> dict | None:
    """Use an empty search box when a click target is unrelated to the goal."""
    if name_matches_goal(goal, element.get("name") or ""):
        return None
    if is_submit(element):
        return None
    field = search_field(elements)
    if field is None or (field.get("value") or "").strip():
        return None
    return field


def is_field(element: dict) -> bool:
    if element.get("form_field"):
        return True
    if element.get("role") in FIELD_ROLES:
        return True
    return (element.get("type") or "") in FIELD_TYPES and element.get("role") == "input"


def is_text_field(element: dict) -> bool:
    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()
    input_type = (element.get("type") or "").lower()
    if tag == "textarea" or element.get("contenteditable"):
        return True
    if role in {"textbox", "searchbox", "combobox", "textarea"}:
        return tag != "select"
    if tag == "input" or role == "input":
        return input_type not in NON_TEXT_INPUT_TYPES
    return False


def build_questions(elements: list[dict]) -> dict:
    click = {"none": "no listed control should be clicked"}
    typed = {"none": "no listed field should be typed into"}
    for element in elements:
        label = _label(element)
        click[element["id"]] = label
        if is_text_field(element):
            typed[element["id"]] = label
    return {
        "operation": {
            "type": "choice",
            "instructions": "What is the single next operation toward the goal?",
            "criteria": OPERATIONS,
        },
        "click_target": {
            "type": "choice",
            "instructions": "If the operation is CLICK, which element? Otherwise choose none.",
            "criteria": click,
        },
        "type_target": {
            "type": "choice",
            "instructions": "If the operation is TYPE_TEXT, which field? Otherwise choose none.",
            "criteria": typed,
        },
    }


def build_state(goal: str, url: str, elements: list[dict]) -> dict:
    lines = [f'{element["id"]} {_label(element)}' for element in elements]
    return {
        "goal": goal[:1500],
        "url": url[:300],
        "elements": lines,
    }


def choice_of(answers: dict, key: str) -> str:
    node = answers.get(key) or {}
    value = node.get("choice")
    return str(value) if value else "BLOCKED" if key == "operation" else "none"


def parse_decision(answers: dict) -> dict:
    operation = choice_of(answers, "operation")
    if operation not in OPERATIONS:
        operation = "BLOCKED"
    click_node = answers.get("click_target") or {}
    type_node = answers.get("type_target") or {}
    return {
        "operation": operation,
        "click_target": choice_of(answers, "click_target"),
        "type_target": choice_of(answers, "type_target"),
        "click_confidence": click_node.get("confidence"),
        "type_confidence": type_node.get("confidence"),
        "operation_confidence": (answers.get("operation") or {}).get("confidence"),
    }
