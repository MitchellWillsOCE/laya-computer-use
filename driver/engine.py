"""Playwright session plus the Laya step loop."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from driver.decide import (
    LIMIT,
    build_questions,
    build_state,
    is_sensitive,
    narrow,
    parse_decision,
    search_field,
    type_instead,
)
from driver.observe import observe, read_text

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profile"
SHOTS = ROOT / "runs" / "shots"
URL_RE = re.compile(r"https?://[^\s]+")


class Engine:
    def __init__(self) -> None:
        self.router = None
        self.playwright = None
        self.context = None
        self.page = None
        self.device = os.environ.get("LAYA_DEVICE", "cuda")
        self.model = os.environ.get("LAYA_MODEL", "english")
        self.auto_confidence = float(os.environ.get("LAYA_AUTO_CONFIDENCE", "0.35"))
        self.goal = ""
        self.step = 0
        self.max_steps = int(os.environ.get("LAYA_MAX_STEPS", "20"))
        self.default_max_steps = self.max_steps
        self.observe_cap = int(os.environ.get("LAYA_OBSERVE_CAP", "80"))
        self.default_observe_cap = self.observe_cap
        self.element_limit = int(os.environ.get("LAYA_ELEMENT_LIMIT", str(LIMIT)))
        self.default_element_limit = self.element_limit
        self.pending: dict | None = None
        self.status = "starting"
        self.last_url = ""
        self.last_error: str | None = None
        self.history: list[dict] = []
        self.visible: list[dict] = []
        self.full_elements: list[dict] = []
        self.last_screenshot: str | None = None
        self._attached_page_ids: set[int] = set()
        self._interrupt = threading.Event()
        self.last_decision_signature: tuple | None = None
        self.decision_repeats = 0

    def start(self) -> None:
        os.environ["USE_TF"] = "0"
        self.router = self._load_router()
        from playwright.sync_api import sync_playwright

        PROFILE.mkdir(parents=True, exist_ok=True)
        SHOTS.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=os.environ.get("LAYA_HEADLESS") == "1",
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._attach_page(self.page)
        self.context.on("page", self._attach_page)
        self.status = "idle"

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self.playwright is not None:
            self.playwright.stop()

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "goal": self.goal,
            "step": self.step,
            "url": self.last_url,
            "device": self.device,
            "model": self.model,
            "pending": self.pending,
            "error": self.last_error,
            "max_steps": self.max_steps,
            "observe_cap": self.observe_cap,
            "element_limit": self.element_limit,
        }

    def status_packet(self) -> dict:
        try:
            self._activate_latest_page()
            self._refresh_elements()
        except Exception:
            pass
        return self._packet(self.status)

    def fallback_error_packet(self, message: str, mutate: bool = True) -> dict:
        if mutate:
            self.status = "error"
            self.last_error = message
        return {
            "status": "error",
            "goal": self.goal,
            "step": self.step,
            "url": self.last_url,
            "prompt": message,
            "element": None,
            "screenshot": self.last_screenshot,
            "error": message,
            "suggestion": None,
            "elements": [_choice_element(item) for item in self.visible],
            "text": "",
        }

    def ensure_screenshot(self) -> str | None:
        return self.last_screenshot or self._shoot()

    def elements_packet(self, query: str | None = None) -> dict:
        self._activate_latest_page()
        elements = self._observe_page(0, whole_page=True)
        self.full_elements = elements
        if query:
            elements = [
                element for element in elements if _query_matches(element, query)
            ]
        return {
            "status": self.status,
            "step": self.step,
            "url": self.page.url,
            "query": query or "",
            "count": len(elements),
            "elements": [_public_element(element) for element in elements],
            "text": self._read_text(),
            "tabs": self._tabs(),
            "error": self.last_error,
        }

    def task(
        self,
        goal: str,
        url: str | None,
        restart: bool = False,
        max_steps: int | None = None,
        observe_cap: int | None = None,
        element_limit: int | None = None,
    ) -> dict:
        del restart  # Kept for API compatibility; every new task replaces the old one.
        self._interrupt.clear()
        self.goal = goal.strip()
        self.step = 0
        self.max_steps = _bounded_int(max_steps, self.default_max_steps, 1, 500)
        self.observe_cap = _bounded_int(
            observe_cap, self.default_observe_cap, 0, 5_000
        )
        self.element_limit = _bounded_int(
            element_limit, self.default_element_limit, 1, 500
        )
        self.pending = None
        self.last_error = None
        self.history = []
        self.last_decision_signature = None
        self.decision_repeats = 0
        if not self.goal:
            return self._error("goal is required")
        target = url or _first_url(self.goal)
        if target:
            self._goto(target)
        try:
            self._refresh_elements()
        except Exception:
            self.visible = []
        self.status = "needs_action"
        reason = (
            "No controls were visible on the page. Scroll, read, or navigate."
            if not self.visible
            else "Task started. Nothing has been clicked. Choose the next actions for this page."
        )
        self.pending = {"kind": "action", "prompt": reason, "suggestion": None}
        return self._packet("needs_action", prompt=reason)

    def reply(
        self,
        text: str | None,
        action: str | None,
        element_id: str | None = None,
        submit: bool = False,
        payload: dict | None = None,
    ) -> dict:
        payload = payload or {}
        action = (action or "").lower()
        actions = payload.get("actions")
        pending_kind = (self.pending or {}).get("kind")
        if isinstance(actions, list) and pending_kind in {None, "action"}:
            if self.pending is None:
                self.pending = {"kind": "action", "prompt": "Action list."}
            return self._run_action_list(actions)
        if action == "navigate":
            interrupted = self._abort_if_interrupted()
            if interrupted is not None:
                return interrupted
            target = payload.get("url") or text
            if not target:
                return self._error("navigate requires a URL in url or text")
            self.last_error = None
            self.pending = None
            try:
                self._goto(str(target))
                return self.run_until_stop()
            except Exception as exc:
                return self._error(str(exc))
        self.last_error = None
        if action == "abort":
            self.pending = None
            self.goal = ""
            self.status = "aborted"
            return self._packet("aborted")
        if self.pending is None and action:
            self.pending = {"kind": "action", "prompt": "Continuing after the previous status."}
            return self._run_external_action(
                action, element_id, text, submit, payload
            )
        kind = self.pending.get("kind")
        if kind == "url":
            if not text:
                return self._packet("needs_text")
            self._goto(text.strip())
            self.pending = None
            return self.run_until_stop()
        if kind == "type_text":
            if not text:
                return self._packet("needs_text")
            try:
                self._fill(self.pending, text, submit=submit)
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                return self._action_failure(exc)
        if kind == "confirm_click":
            if action != "confirm":
                return self._packet("needs_confirm")
            element = self.pending["element"]
            try:
                self._click_id(
                    element["id"], element.get("name"), element.get("role")
                )
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                return self._action_failure(exc)
        if kind == "confirm_type":
            if action != "confirm":
                return self._packet("needs_confirm")
            element = self.pending["element"]
            return self._stop(
                "needs_text",
                f'Type the text for {element["role"]} "{element["name"]}".',
                element=element,
                kind="type_text",
            )
        if kind == "blocked":
            if action != "continue":
                return self._packet("blocked")
            self.pending = None
            return self.run_until_stop()
        if kind == "action":
            return self._run_external_action(
                action, element_id, text, submit, payload
            )
        return self._error(f"cannot resume {kind}")

    def run_until_stop(self) -> dict:
        while self.step < self.max_steps:
            interrupted = self._abort_if_interrupted()
            if interrupted is not None:
                return interrupted
            self.step += 1
            self.status = "running"
            self._activate_latest_page()
            self.full_elements = self._observe_page(self.observe_cap)
            interrupted = self._abort_if_interrupted()
            if interrupted is not None:
                return interrupted
            elements = narrow(
                self.full_elements, self.goal, limit=self.element_limit
            )
            self.visible = elements
            current_url = self.page.url
            if self.last_url and current_url != self.last_url:
                self.last_decision_signature = None
                self.decision_repeats = 0
            self.last_url = current_url
            if not elements:
                self.page.mouse.wheel(0, 700)
                self.page.wait_for_timeout(400)
                self.full_elements = self._observe_page(self.observe_cap)
                elements = narrow(
                    self.full_elements, self.goal, limit=self.element_limit
                )
                self.visible = elements
            if not elements:
                return self._needs_action(
                    {"operation": "BLOCKED", "click_target": "none", "type_target": "none"},
                    "No controls were visible on the page. Scroll, read, or navigate.",
                )
            decision = self._decide(elements)
            interrupted = self._abort_if_interrupted()
            if interrupted is not None:
                return interrupted
            self.history.append({"step": self.step, "url": self.last_url, **decision})
            signature = (
                self.last_url,
                tuple(
                    (
                        item.get("id"),
                        item.get("name"),
                        item.get("value"),
                        item.get("href"),
                        item.get("checked"),
                        item.get("selected"),
                        item.get("invalid"),
                        item.get("error"),
                    )
                    for item in elements
                ),
                decision.get("operation"),
                decision.get("click_target"),
                decision.get("type_target"),
            )
            if signature == self.last_decision_signature:
                self.decision_repeats += 1
            else:
                self.last_decision_signature = signature
                self.decision_repeats = 1
            print(
                f"step {self.step} {decision['operation']} "
                f"click={decision['click_target']} type={decision['type_target']}",
                flush=True,
            )
            if self.decision_repeats >= 2:
                return self._needs_action(
                    decision,
                    "Laya repeated the same decision without a page-state change.",
                )
            operation = decision["operation"]
            if operation == "READ":
                return self._needs_action(
                    decision,
                    "Laya proposed READ. Use the packet text, or reply with action read.",
                )
            if operation == "CLICK":
                element = _find(elements, decision["click_target"])
                if element is not None and self.step == 1:
                    field = type_instead(self.goal, element, elements)
                    if field is not None and not _filled(field):
                        print(
                            f"step {self.step} override type into \"{field.get('name')}\" "
                            f"instead of click \"{element.get('name')}\"",
                            flush=True,
                        )
                        return self._needs_type(field)
            if not self._decision_confident(decision):
                return self._needs_action(
                    decision,
                    "Laya's confidence is below the auto-action threshold.",
                )
            if operation in {"CHECK", "UNCHECK"}:
                element = _checkable_target(
                    elements,
                    decision,
                    want_checked=(operation == "UNCHECK"),
                    goal=self.goal,
                )
                if element is None:
                    return self._needs_action(
                        decision,
                        f"Laya proposed {operation} without one clear checkbox.",
                    )
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                try:
                    self._set_checked(element, operation == "CHECK")
                except Exception as exc:
                    return self._action_failure(exc)
                continue
            if operation == "SELECT":
                element = _select_target(elements, decision)
                option = None if element is None else _goal_option(self.goal, element, self.page)
                if element is None or option is None:
                    return self._needs_action(
                        decision,
                        "Laya proposed SELECT without one option named in the goal.",
                    )
                if element.get("selected") != option:
                    interrupted = self._abort_if_interrupted()
                    if interrupted is not None:
                        return interrupted
                    try:
                        self._locator_from_element(element).select_option(label=option)
                    except Exception as exc:
                        return self._action_failure(exc)
                continue

            if operation == "DONE":
                return self._needs_action(
                    decision,
                    "Laya thinks the goal is already done. Send action done only if the page agrees.",
                )
            if operation == "BLOCKED":
                return self._needs_action(
                    decision,
                    "Laya marked this screen blocked, but the page is still open. Choose an action, or send blocked with a reason.",
                )
            if operation == "WAIT":
                self.page.wait_for_timeout(1200)
                continue
            if operation == "SCROLL":
                self.page.mouse.wheel(0, 700)
                self.page.wait_for_timeout(400)
                continue
            if operation == "PRESS_ENTER":
                self.page.keyboard.press("Enter")
                self._settle()
                continue
            if operation == "CLICK":
                element = _find(elements, decision["click_target"])
                if element is None:
                    return self._stop("blocked", "Laya chose a click target that is not on the list.")
                if is_sensitive(element):
                    return self._stop(
                        "needs_confirm",
                        f'Confirm before clicking "{element["name"]}".',
                        element=element,
                        kind="confirm_click",
                    )
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                try:
                    self._click_id(element["id"], element.get("name"), element.get("role"))
                except Exception as exc:
                    return self._action_failure(exc)
                continue
            if operation == "TYPE_TEXT":
                element = _find(elements, decision["type_target"])
                if element is None:
                    return self._stop("blocked", "Laya chose a field that is not on the list.")
                outcome = self._needs_type(element)
                if outcome is None:
                    continue
                return outcome
            return self._stop("blocked", f"Unhandled operation {operation}.")
        return self._stop("blocked", f"Stopped after {self.max_steps} steps.")

    def request_interrupt(self) -> None:
        self._interrupt.set()

    def _abort_if_interrupted(self) -> dict | None:
        if not self._interrupt.is_set():
            return None
        self.pending = None
        self.status = "aborted"
        return self._packet(
            "aborted",
            prompt="Replaced by a newer task.",
            screenshot=self._shoot(),
        )

    def _decide(self, elements: list[dict]) -> dict:
        state = build_state(self.goal, self.page.url, elements)
        questions = build_questions(elements)
        result = self.router.predict(state, questions, model=self.model)
        return parse_decision(result.get("answers") or {})

    def _decision_confident(self, decision: dict) -> bool:
        operation = decision.get("operation")
        values = [decision.get("operation_confidence")]
        if operation == "CLICK":
            values.append(decision.get("click_confidence"))
        elif operation == "TYPE_TEXT":
            values.append(decision.get("type_confidence"))
        try:
            return all(value is not None and float(value) >= self.auto_confidence for value in values)
        except (TypeError, ValueError):
            return False

    def _needs_action(self, decision: dict, reason: str) -> dict:
        self.status = "needs_action"
        self.pending = {
            "kind": "action",
            "prompt": (
                f"{reason} Reply with actions, a list of up to 8 actions for this page. "
                "Use a current element id. A single action field still works."
            ),
            "suggestion": decision,
        }
        return self._packet("needs_action")

    def _run_action_list(self, actions: list) -> dict:
        if not actions:
            return self._error("actions must contain at least one action", keep_pending=True)
        if len(actions) > 8:
            return self._error("send at most 8 actions", keep_pending=True)
        for index, item in enumerate(actions):
            if not isinstance(item, dict):
                return self._error(
                    f"action {index + 1} must be an object", keep_pending=True
                )
            interrupted = self._abort_if_interrupted()
            if interrupted is not None:
                return interrupted
            action = str(item.get("action") or "").lower()
            element_id = item.get("element_id")
            element_key = None if element_id is None else str(element_id)
            text = item.get("text")
            text_value = None if text is None else str(text)
            submit = bool(item.get("submit"))
            try:
                self._refresh_elements()
            except Exception:
                pass
            before = self._document_id()
            if element_key and action in {
                "click", "type", "select", "check", "uncheck", "upload",
                "press", "press_enter", "scroll",
            }:
                if self._element(element_key) is None:
                    self.status = "needs_action"
                    self.pending = {
                        "kind": "action",
                        "prompt": (
                            f"Stopped before action {index + 1} ({action}). "
                            f"Element {element_key} is not on this page. "
                            "Choose from the current elements."
                        ),
                    }
                    return self._packet("needs_action")
            if action == "navigate":
                target = item.get("url") or text_value
                if not target:
                    return self._error(
                        "navigate requires a URL in url or text", keep_pending=True
                    )
                try:
                    self._goto(str(target))
                except Exception as exc:
                    return self._action_failure(exc)
                self.pending = None
            else:
                result = self._run_external_action(
                    action, element_key, text_value, submit, item, resume=False
                )
                if result is not None:
                    return result
            if index + 1 < len(actions):
                try:
                    self._refresh_elements()
                except Exception:
                    pass
                after = self._document_id()
                if before and after and before != after:
                    self.status = "needs_action"
                    self.pending = {
                        "kind": "action",
                        "prompt": (
                            f"Stopped after action {index + 1} because the page changed. "
                            "Choose the next action from the current elements."
                        ),
                    }
                    return self._packet("needs_action")
        return self.run_until_stop()

    def _document_id(self) -> str:
        try:
            value = self.page.evaluate("() => window.__layaDocumentId || ''")
            return str(value or "")
        except Exception:
            return ""

    def _run_external_action(
        self,
        action: str,
        element_id: str | None,
        text: str | None,
        submit: bool,
        payload: dict,
        resume: bool = True,
    ) -> dict | None:
        saved_run = self.run_until_stop

        def finish() -> dict | None:
            self.pending = None
            if resume:
                return saved_run()
            return None

        self.run_until_stop = finish
        try:
            return self._dispatch_external_action(
                action, element_id, text, submit, payload
            )
        finally:
            self.run_until_stop = saved_run

    def _dispatch_external_action(
        self,
        action: str,
        element_id: str | None,
        text: str | None,
        submit: bool,
        payload: dict,
    ) -> dict | None:
        interrupted = self._abort_if_interrupted()
        if interrupted is not None:
            return interrupted
        saved_pending = self.pending
        if action == "abort":
            self.pending = None
            self.status = "aborted"
            return self._packet("aborted", screenshot=self._shoot())
        if action == "done":
            return self._stop("done", "Grok Bot marked the goal complete.")
        if action == "blocked":
            return self._stop("blocked", text or "Grok Bot marked the page blocked.")
        if action == "read":
            try:
                content = self._read_text(element_id)
                self.status = "needs_action"
                return self._packet(
                    "needs_action",
                    prompt="Read completed; choose the next action.",
                    screenshot=self._shoot(),
                    text_value=content,
                    detailed=True,
                )
            except Exception as exc:
                return self._error(str(exc), keep_pending=True)
        if action == "new_tab":
            try:
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                page = self.context.new_page()
                self._attach_page(page)
                target = payload.get("url") or text
                if target:
                    self._goto(str(target))
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action == "switch_tab":
            try:
                index = int(payload.get("index", payload.get("tab_index")))
                pages = self._open_pages()
                if index < 0 or index >= len(pages):
                    return self._error(
                        f"tab index {index} is out of range", keep_pending=True
                    )
                self.page = pages[index]
                self.page.bring_to_front()
                self.pending = None
                return self.run_until_stop()
            except (TypeError, ValueError):
                return self._error(
                    "switch_tab requires an integer index", keep_pending=True
                )
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action == "close_tab":
            try:
                pages = self._open_pages()
                raw_index = payload.get("index", payload.get("tab_index"))
                target = self.page if raw_index is None else pages[int(raw_index)]
                target.close()
                pages = self._open_pages()
                self.page = pages[-1] if pages else self.context.new_page()
                self._attach_page(self.page)
                self.pending = None
                return self.run_until_stop()
            except (IndexError, TypeError, ValueError):
                return self._error(
                    "close_tab received an invalid tab index", keep_pending=True
                )
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action == "wait":
            self.pending = None
            self.page.wait_for_timeout(1200)
            return self.run_until_stop()
        if action == "scroll":
            try:
                amount = int(payload.get("amount", payload.get("delta_y", 700)))
                if str(payload.get("direction", "")).lower() == "up":
                    amount = -abs(amount)
                elif str(payload.get("direction", "")).lower() == "down":
                    amount = abs(amount)
                if element_id:
                    element = self._element(element_id)
                    if element is None:
                        return self._error(
                            f"element_id {element_id!r} is not on the current page",
                            keep_pending=True,
                        )
                    locator = self._locator_from_element(element)
                    locator.evaluate("(el, dy) => el.scrollBy(0, dy)", amount)
                else:
                    self.page.mouse.wheel(0, amount)
                self.page.wait_for_timeout(400)
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action in {"press", "press_enter"}:
            try:
                key = "Enter" if action == "press_enter" else str(
                    payload.get("key") or text or ""
                )
                if not key:
                    return self._error("press requires key", keep_pending=True)
                if element_id:
                    element = self._element(element_id)
                    if element is None:
                        return self._error(
                            f"element_id {element_id!r} is not on the current page",
                            keep_pending=True,
                        )
                    self._locator_from_element(element).press(key)
                else:
                    interrupted = self._abort_if_interrupted()
                    if interrupted is not None:
                        return interrupted
                    self.page.keyboard.press(key)
                self._settle()
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)

        element = self._element(element_id or "")
        if element is None:
            return self._error(
                f"element_id {element_id!r} is not in the current elements list",
                keep_pending=True,
            )
        if action == "click":
            if is_sensitive(element):
                return self._stop(
                    "needs_confirm",
                    f'Confirm before clicking "{element["name"]}".',
                    element=element,
                    kind="confirm_click",
                )
            try:
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                self._click_id(
                    element["id"], element.get("name"), element.get("role")
                )
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action == "type":
            if text is None:
                return self._error("type requires text", keep_pending=True)
            if is_sensitive(element):
                return self._stop(
                    "needs_confirm",
                    f'Confirm before typing into "{element["name"]}".',
                    element=element,
                    kind="confirm_type",
                )
            try:
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                self._fill({"element": element}, text, submit=submit)
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action == "select":
            try:
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                locator = self._locator_from_element(element)
                label = payload.get("label")
                value = payload.get("value")
                option = text
                if label is not None:
                    locator.select_option(label=str(label))
                elif value is not None:
                    locator.select_option(value=str(value))
                elif option is not None:
                    try:
                        locator.select_option(label=str(option))
                    except Exception:
                        locator.select_option(value=str(option))
                else:
                    return self._error(
                        "select requires label, value, or text", keep_pending=True
                    )
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action in {"check", "uncheck"}:
            try:
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                locator = self._locator_from_element(element)
                (
                    locator.check(force=True)
                    if action == "check"
                    else locator.uncheck(force=True)
                )
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        if action == "upload":
            try:
                interrupted = self._abort_if_interrupted()
                if interrupted is not None:
                    return interrupted
                raw_path = payload.get("path") or text
                if not raw_path:
                    return self._error("upload requires path", keep_pending=True)
                path = Path(str(raw_path)).expanduser().resolve()
                if not path.is_file():
                    return self._error(
                        f"upload file does not exist: {path}", keep_pending=True
                    )
                self._locator_from_element(element).set_input_files(str(path))
                self.pending = None
                return self.run_until_stop()
            except Exception as exc:
                self.pending = saved_pending
                return self._action_failure(exc)
        return self._error(f"unsupported action {action!r}", keep_pending=True)

    def _load_router(self):
        from laya import Router

        candidates = [self.device, "cpu"] if self.device != "cpu" else ["cpu"]
        last_error = None
        for device in candidates:
            try:
                router = Router(preload=False, device=device, max_loaded=1)
                router.preload([self.model])
                self.device = device
                print(f"Laya ready on {device}, model {self.model}", flush=True)
                return router
            except Exception as exc:
                last_error = exc
                print(f"Laya failed on {device}: {exc}", flush=True)
        raise RuntimeError(f"Could not load Laya: {last_error}")

    def _goto(self, url: str) -> None:
        if not url.startswith("http"):
            url = "https://" + url
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._settle()

    def _blank(self) -> bool:
        return self.page.url in {"about:blank", "chrome://newtab/"}

    def _settle(self) -> None:
        for _ in range(3):
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                self.page.wait_for_timeout(500)
                self._activate_latest_page()
                return
            except Exception as exc:
                if "navigation" not in str(exc).lower() and "context" not in str(exc).lower():
                    break
                self.page.wait_for_timeout(300)
        self._activate_latest_page()

    def _attach_page(self, page) -> None:
        self.page = page
        if id(page) in self._attached_page_ids:
            return
        self._attached_page_ids.add(id(page))
        page.on("dialog", lambda dialog: dialog.dismiss())

    def _activate_latest_page(self) -> None:
        pages = self._open_pages()
        if pages and self.page not in pages:
            self.page = pages[-1]

    def _open_pages(self) -> list:
        if self.context is None:
            return []
        return [page for page in self.context.pages if not page.is_closed()]

    def _tabs(self) -> list[dict]:
        tabs = []
        for index, page in enumerate(self._open_pages()):
            try:
                title = page.title()
            except Exception:
                title = ""
            tabs.append(
                {
                    "index": index,
                    "title": title,
                    "url": page.url,
                    "active": page is self.page,
                }
            )
        return tabs

    def _read_text(self, element_id: str | None = None) -> str:
        return read_text(self.page, element_id=element_id)

    def _refresh_elements(self, full: bool = False) -> None:
        self.full_elements = self._observe_page(
            0 if full else self.observe_cap
        )
        self.visible = narrow(
            self.full_elements, self.goal, limit=self.element_limit
        )
        self.last_url = self.page.url

    def _element(self, element_id: str) -> dict | None:
        element = _find(self.full_elements, element_id) or _find(
            self.visible, element_id
        )
        if element is not None:
            return element
        try:
            self._refresh_elements(full=True)
        except Exception:
            return None
        return _find(self.full_elements, element_id) or _find(
            self.visible, element_id
        )

    def _observe_page(self, cap: int | None = None, whole_page: bool = False) -> list[dict]:
        """Retry DOM extraction while a click or Enter is replacing the document."""
        last_error: Exception | None = None
        for _ in range(8):
            try:
                return observe(
                    self.page,
                    self.observe_cap if cap is None else cap,
                    whole_page=whole_page,
                )
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if "execution context was destroyed" not in message and "navigation" not in message:
                    raise
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                self.page.wait_for_timeout(350)
        raise RuntimeError(f"Page did not settle after navigation: {last_error}")

    def mark_error(self, message: str, keep_pending: bool = False) -> dict:
        self.status = "error"
        self.last_error = message
        if not keep_pending:
            self.pending = None
        try:
            self._refresh_elements()
        except Exception:
            pass
        return self._packet(
            "error",
            prompt=message,
            screenshot=self._shoot(),
        )

    def _error(self, message: str, keep_pending: bool = False) -> dict:
        return self.mark_error(message, keep_pending=keep_pending)

    def _action_failure(self, exc: Exception) -> dict:
        message = str(exc)
        stale = any(
            marker in message.lower()
            for marker in (
                "no longer on the page",
                "not attached",
                "detached",
                "execution context was destroyed",
                "timeout",
                "strict mode violation",
            )
        )
        if stale:
            self.status = "needs_action"
            self.last_error = message
            previous = self.pending or {}
            self.pending = {
                "kind": "action",
                "prompt": (
                    "The control changed before the action completed. "
                    "Choose a fresh element id."
                ),
                "suggestion": previous.get("suggestion"),
            }
            try:
                self._refresh_elements()
            except Exception:
                pass
            return self._packet(
                "needs_action",
                prompt=(
                    "The control changed before the action completed. "
                    "Choose a fresh element id; the pending action was preserved."
                ),
                screenshot=self._shoot(),
            )
        return self._error(message, keep_pending=True)

    def _set_checked(self, element: dict, checked: bool) -> None:
        locator = self._locator_from_element(element)
        locator.check(force=True) if checked else locator.uncheck(force=True)

    def _press_enter(self) -> None:
        self.page.keyboard.press("Enter")
        self._settle()

    def _click_id(self, element_id: str, name: str | None, role: str | None = None) -> None:
        locator = self._locator(element_id, name, role)
        locator.click(timeout=8000)
        self._settle()

    def _fill(self, pending: dict, text: str, submit: bool = False) -> None:
        element = pending["element"]
        locator = self._locator_from_element(element)
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        editable = bool(locator.evaluate("el => el.isContentEditable"))
        legacy_submit = tag != "textarea" and text.endswith(("\n", "\r"))
        should_submit = submit or legacy_submit
        if tag == "textarea" and not should_submit:
            typed = text.replace("\r\n", "\n")
        elif editable and not should_submit:
            typed = text.replace("\r\n", "\n")
        else:
            typed = text.rstrip("\r\n").replace("\r", "").replace("\n", "")
        if tag == "select":
            try:
                locator.select_option(label=typed)
            except Exception:
                locator.select_option(value=typed)
        elif tag in {"input", "textarea"} or editable:
            locator.fill(typed)
        else:
            locator.click(timeout=8000, force=True)
            self.page.keyboard.type(typed)
        if should_submit:
            locator.press("Enter")
            self._settle()
        else:
            self._settle()

    def _locator_from_element(self, element: dict):
        return self._locator(
            str(element.get("id") or ""),
            element.get("name"),
            element.get("role"),
        )

    def _locator(self, element_id: str, name: str | None, role: str | None = None):
        del role
        locator = self.page.locator(f'[data-laya-id="{element_id}"]')
        if locator.count() > 0:
            return locator.first
        self._refresh_elements(full=True)
        locator = self.page.locator(f'[data-laya-id="{element_id}"]')
        if locator.count() > 0:
            return locator.first
        raise RuntimeError(f'Control "{name or element_id}" is no longer on the page.')

    def _stop(self, status: str, prompt: str, element: dict | None = None, kind: str | None = None) -> dict:
        self.status = status
        shot = self._shoot()
        if status == "done":
            self.pending = None
        else:
            self.pending = {
                "kind": kind or status,
                "prompt": prompt,
                "element": _public_element(element) if element else None,
            }
        return self._packet(status, prompt=prompt, screenshot=shot, element=element)

    def _packet(
        self,
        status: str,
        prompt: str | None = None,
        screenshot: str | None = None,
        element: dict | None = None,
        text_value: str | None = None,
        detailed: bool = False,
    ) -> dict:
        pending = self.pending or {}
        show = _public_element if detailed else _choice_element
        shown_element = show(element) if element else show(pending.get("element"))
        return {
            "status": status,
            "goal": self.goal,
            "step": self.step,
            "url": (
                self.page.url
                if self.page is not None and not self.page.is_closed()
                else self.last_url
            ),
            "prompt": prompt or pending.get("prompt"),
            "element": shown_element,
            "screenshot": screenshot or self.last_screenshot,
            "error": self.last_error,
            "suggestion": pending.get("suggestion"),
            "elements": [show(item) for item in self.visible],
            "text": "" if text_value is None else text_value,
        }

    def _needs_type(self, element: dict) -> dict | None:
        if _filled(element):
            print(f"step {self.step} \"{element.get('name')}\" already has text, pressing Enter", flush=True)
            self._press_enter()
            return None
        if is_sensitive(element):
            return self._stop(
                "needs_confirm",
                f'Confirm before typing into "{element["name"]}".',
                element=element,
                kind="confirm_type",
            )
        return self._stop(
            "needs_text",
            f'Type the text for {element["role"]} "{element["name"]}".',
            element=element,
            kind="type_text",
        )

    def _shoot(self) -> str | None:
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            path = SHOTS / f"step-{self.step}-{time.time_ns()}.png"
            self.page.screenshot(path=str(path))
            self.last_screenshot = str(path)
            return self.last_screenshot
        except Exception:
            return None


def _filled(element: dict | None) -> bool:
    return bool(element and (element.get("value") or "").strip())


def _is_checkable(element: dict) -> bool:
    role = (element.get("role") or "").lower()
    control = (element.get("type") or "").lower()
    return role in {"checkbox", "radio"} or control in {"checkbox", "radio"}


def _is_select(element: dict) -> bool:
    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()
    return tag == "select" or role == "listbox"


def _named_target(elements: list[dict], decision: dict, predicate) -> dict | None:
    for key in ("click_target", "type_target"):
        element = _find(elements, decision.get(key) or "")
        if element is not None and predicate(element):
            return element
    return None


def _checkable_target(
    elements: list[dict], decision: dict, want_checked: bool, goal: str = ""
) -> dict | None:
    named = _named_target(elements, decision, _is_checkable)
    if named is not None:
        return named
    folded_goal = (goal or "").lower()
    matches = [
        element
        for element in elements
        if _is_checkable(element)
        and element.get("checked") is want_checked
        and not element.get("disabled")
        and (element.get("name") or "").strip()
        and (element.get("name") or "").lower() in folded_goal
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _query_matches(element: dict, query: str) -> bool:
    haystack = " ".join(
        str(element.get(key) or "")
        for key in ("id", "role", "name", "value", "selected", "error", "href", "tag")
    ).casefold()
    needle = (query or "").casefold().strip()
    if not needle:
        return True
    if needle in haystack:
        return True
    words = [word for word in re.findall(r"[a-z0-9]+", needle) if len(word) >= 2]
    return bool(words) and all(word in haystack for word in words)


def _select_target(elements: list[dict], decision: dict) -> dict | None:
    named = _named_target(elements, decision, _is_select)
    if named is not None:
        return named
    matches = [
        element
        for element in elements
        if _is_select(element) and not element.get("disabled")
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _goal_option(goal: str, element: dict, page) -> str | None:
    locator = page.locator(f'[data-laya-id="{element["id"]}"]')
    labels = [label.strip() for label in locator.locator("option").all_inner_texts()]
    folded = (goal or "").lower()
    hits = [label for label in labels if label and label.lower() in folded]
    if len(hits) == 1:
        return hits[0]
    return None


def _find(elements: list[dict], element_id: str) -> dict | None:
    if not element_id or element_id == "none":
        return None
    for element in elements:
        if element["id"] == element_id:
            return element
    return None


def _choice_element(element: dict | None) -> dict | None:
    if not element:
        return None
    chosen = {
        "id": element.get("id"),
        "role": element.get("role"),
        "name": element.get("name") or "",
        "value": element.get("value") or "",
        "checked": element.get("checked"),
        "selected": element.get("selected") or "",
        "disabled": bool(element.get("disabled")),
    }
    href = element.get("href") or ""
    if href:
        chosen["href"] = href
    return chosen


def _public_element(element: dict | None) -> dict | None:
    if not element:
        return None
    return {
        "id": element.get("id"),
        "role": element.get("role"),
        "name": element.get("name"),
        "value": element.get("value"),
        "type": element.get("type"),
        "tag": element.get("tag"),
        "href": element.get("href"),
        "checked": element.get("checked"),
        "selected": element.get("selected"),
        "required": bool(element.get("required")),
        "disabled": bool(element.get("disabled")),
        "invalid": bool(element.get("invalid")),
        "error": element.get("error") or "",
        "dialog": bool(element.get("dialog")),
        "form_field": bool(element.get("form_field")),
        "contenteditable": bool(element.get("contenteditable")),
        "scrollable": bool(element.get("scrollable")),
        "x": element.get("x"),
        "y": element.get("y"),
    }


def _first_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(").,") if match else None


def _bounded_int(
    value: int | str | None,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
