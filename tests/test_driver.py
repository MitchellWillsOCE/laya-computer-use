import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from driver.engine import Engine
from driver.observe import observe, read_text


DIALOG_HTML = """
<a href="#main">Skip to main content</a>
<button id="background">Background action</button>
<main id="main">Background page</main>
<section role="dialog" aria-modal="true">
  <h2>Application submitted 東京</h2>
  <label for="phone">Phone number</label>
  <input id="phone" required aria-invalid="true" aria-describedby="phone-error">
  <span id="phone-error">Phone is required</span>
  <span id="city-label">Location</span>
  <input id="city" aria-labelledby="city-label">
  <input id="unnamed">
  <div id="editable" contenteditable style="width:40px;height:20px"></div>
  <input aria-label="Disabled field" disabled>
  <label><input id="follow" type="checkbox" checked> Follow company</label>
  <div role="checkbox" aria-label="Custom check" aria-checked="false"
       tabindex="0" style="width:20px;height:20px"></div>
  <label for="resume">Resume card</label>
  <select id="resume">
    <option>Resume A</option>
    <option selected>Resume B</option>
  </select>
  <button>Next</button>
</section>
"""


ACTIONS_HTML = """
<main>Résumé envoyé — 東京</main>
<label for="country">Country</label>
<select id="country">
  <option value="au">Australia</option>
  <option value="ca">Canada</option>
</select>
<label><input id="follow" type="checkbox"> Follow company</label>
<input id="keys" aria-label="Location">
<textarea id="notes" aria-label="Notes"></textarea>
<div id="pane" role="list" aria-label="Job results"
     style="height:60px; overflow-y:auto">
  <div style="height:500px">Job cards</div>
</div>
<label for="upload">Resume file</label>
<input id="upload" type="file">
<form id="search-form">
  <input id="search" aria-label="Search">
</form>
<script>
  document.querySelector("#keys").addEventListener("keydown", (event) => {
    document.body.dataset.key = event.key;
  });
  document.querySelector("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    document.body.dataset.submitted = "true";
  });
</script>
"""


def _by_name(elements, name):
    return next(element for element in elements if element["name"] == name)


def test_observer_fields_dialog_state_and_stable_ids():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(DIALOG_HTML)

        first = observe(page, cap=100)
        assert all(element["dialog"] for element in first)
        assert not any(element["name"].startswith("Skip to") for element in first)
        assert not any(element["name"] == "Background action" for element in first)

        phone = _by_name(first, "Phone number")
        assert phone["value"] == ""
        assert phone["required"] is True
        assert phone["invalid"] is True
        assert phone["error"] == "Phone is required"
        assert _by_name(first, "Location")["value"] == ""
        assert any(element["tag"] == "input" and not element["name"] for element in first)
        assert any(element["contenteditable"] for element in first)
        assert _by_name(first, "Disabled field")["disabled"] is True
        assert _by_name(first, "Follow company")["checked"] is True
        assert _by_name(first, "Custom check")["checked"] is False
        assert _by_name(first, "Resume card")["selected"] == "Resume B"
        assert "Application submitted 東京" in read_text(page)
        assert "Phone number" in read_text(page, phone["id"])
        assert "Phone is required" in read_text(page, phone["id"])
        try:
            read_text(page, element_id="missing")
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing element read should fail")

        first_id = phone["id"]
        page.evaluate(
            """() => {
              const old = document.querySelector('#phone');
              const replacement = old.cloneNode(true);
              replacement.removeAttribute('data-laya-id');
              old.replaceWith(replacement);
            }"""
        )
        second = observe(page, cap=100)
        assert _by_name(second, "Phone number")["id"] == first_id
        browser.close()


def test_engine_actions_tabs_screenshot_and_recovery():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1000, "height": 700})
        page = context.new_page()
        page.set_content(ACTIONS_HTML)

        engine = Engine()
        engine.playwright = playwright
        engine.context = context
        engine.page = page
        engine._attach_page(page)
        engine.goal = "test the controls"
        engine.status = "needs_action"
        engine.run_until_stop = lambda: engine._packet("needs_action")
        engine._refresh_elements()

        def act(action, element=None, text=None, **payload):
            engine.pending = {"kind": "action", "prompt": "test"}
            result = engine.reply(
                text,
                action,
                element["id"] if element else None,
                bool(payload.get("submit")),
                payload,
            )
            assert result["status"] != "error", result.get("error")
            return result

        country = _by_name(engine.full_elements, "Country")
        act("select", country, label="Canada")
        assert page.locator("#country").input_value() == "ca"

        follow = _by_name(engine.full_elements, "Follow company")
        act("check", follow)
        assert page.locator("#follow").is_checked()
        act("uncheck", follow)
        assert not page.locator("#follow").is_checked()

        location = _by_name(engine.full_elements, "Location")
        act("press", location, key="Enter")
        assert page.locator("body").get_attribute("data-key") == "Enter"

        pane = _by_name(engine.full_elements, "Job results")
        act("scroll", pane, amount=120)
        assert page.locator("#pane").evaluate("el => el.scrollTop") > 0

        upload = _by_name(engine.full_elements, "Resume file")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"resume")
            upload_path = Path(handle.name)
        try:
            act("upload", upload, path=str(upload_path))
            assert page.locator("#upload").evaluate("el => el.files[0].name") == upload_path.name
        finally:
            upload_path.unlink(missing_ok=True)

        engine._refresh_elements()
        notes = _by_name(engine.full_elements, "Notes")
        engine.pending = {
            "kind": "type_text",
            "prompt": "type",
            "element": notes,
        }
        engine.reply("line one\nline two", None, submit=False)
        assert page.locator("#notes").input_value() == "line one\nline two"

        search = _by_name(engine.full_elements, "Search")
        engine.pending = {
            "kind": "type_text",
            "prompt": "type",
            "element": search,
        }
        engine.reply("youtube", None, submit=False)
        assert page.locator("#search").input_value() == "youtube"
        assert page.locator("body").get_attribute("data-submitted") is None
        engine.pending = {
            "kind": "type_text",
            "prompt": "type",
            "element": search,
        }
        engine.reply("youtube", None, submit=True)
        assert page.locator("body").get_attribute("data-submitted") == "true"

        original_page = engine.page
        act("new_tab")
        assert len(engine._tabs()) == 2
        act("switch_tab", index=0)
        assert engine.page is original_page
        engine._activate_latest_page()
        assert engine.page is original_page
        act("close_tab", index=1)
        assert len(engine._tabs()) == 1

        packet = act("read")
        assert "Résumé envoyé — 東京" in packet["text"]
        stopped = engine._stop("blocked", "manual test")
        assert stopped["screenshot"]
        assert Path(stopped["screenshot"]).is_file()

        page.set_content("<button aria-label='Replace me'>Old</button>")
        engine._refresh_elements()
        button = _by_name(engine.full_elements, "Replace me")
        engine.pending = {"kind": "action", "prompt": "test"}
        original_click = engine._click_id

        def stale(*_args, **_kwargs):
            raise RuntimeError('Control "Replace me" is no longer on the page.')

        engine._click_id = stale
        try:
            stale_packet = engine.reply(
                None, "click", button["id"], payload={}
            )
        finally:
            engine._click_id = original_click
        assert stale_packet["status"] == "needs_action"
        assert engine.pending["kind"] == "action"
        assert stale_packet["elements"]

        current_url = page.url
        engine.pending = {"kind": "blocked", "prompt": "old task"}
        engine.request_interrupt()
        engine.task(
            "replacement task",
            None,
            restart=False,
            max_steps=7,
            observe_cap=11,
            element_limit=9,
        )
        assert page.url == current_url
        assert engine.goal == "replacement task"
        assert engine.max_steps == 7
        assert engine.observe_cap == 11
        assert engine.element_limit == 9
        assert not engine._interrupt.is_set()

        context.close()
        browser.close()


def test_confident_uncheck_and_select_run_locally():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(DIALOG_HTML)
        engine = Engine()
        engine.playwright = playwright
        engine.context = page.context
        engine.page = page
        engine._attach_page(page)
        engine.goal = "Uncheck Follow company, select Resume A, and click Next"
        engine.auto_confidence = 0.35
        engine.max_steps = 4
        seen = {"n": 0}

        def decide(_elements):
            seen["n"] += 1
            operation = {1: "UNCHECK", 2: "SELECT"}.get(seen["n"], "DONE")
            return {
                "operation": operation,
                "click_target": "none",
                "type_target": "none",
                "click_confidence": 0.1,
                "type_confidence": 0.1,
                "operation_confidence": 0.9,
            }

        engine._decide = decide
        packet = engine.run_until_stop()
        assert page.locator("#follow").is_checked() is False
        selected = page.locator("#resume").evaluate("el => el.selectedOptions[0].text")
        assert selected == "Resume A"
        assert packet["status"] == "done"
        browser.close()


def test_action_list_and_choice_packet():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(DIALOG_HTML)
        engine = Engine()
        engine.playwright = playwright
        engine.context = page.context
        engine.page = page
        engine._attach_page(page)
        engine.goal = "Uncheck Follow company and select Resume A"
        engine._refresh_elements()
        follow = _by_name(engine.full_elements, "Follow company")
        resume = _by_name(engine.full_elements, "Resume card")
        continued = {"n": 0}

        def continued_packet():
            continued["n"] += 1
            return engine._packet("done", prompt="continued")

        engine.run_until_stop = continued_packet
        engine.pending = {"kind": "action", "prompt": "choose"}
        finished = engine.reply(
            None,
            None,
            payload={
                "actions": [
                    {"action": "uncheck", "element_id": follow["id"]},
                    {"action": "select", "element_id": resume["id"], "label": "Resume A"},
                ]
            },
        )
        assert continued["n"] == 1
        assert finished["status"] == "done"
        assert finished["text"] == ""
        assert finished["elements"]
        assert "tag" not in finished["elements"][0]
        assert "dialog" not in finished["elements"][0]
        assert set(finished["elements"][0]) <= {
            "id", "role", "name", "value", "checked", "selected", "disabled", "href",
        }
        assert page.locator("#follow").is_checked() is False
        assert page.locator("#resume").evaluate("el => el.selectedOptions[0].text") == "Resume A"

        full = engine.elements_packet()
        assert "Phone is required" in full["text"]
        assert "tag" in full["elements"][0]

        engine.pending = {"kind": "action", "prompt": "choose"}
        stopped = engine.reply(
            None,
            None,
            payload={
                "actions": [
                    {"action": "click", "element_id": "missing-id"},
                ]
            },
        )
        assert stopped["status"] == "needs_action"
        assert "missing-id" in stopped["prompt"]
        assert continued["n"] == 1

        page.set_content(
            """
            <button id="leave">Leave</button>
            <script>
              document.querySelector("#leave").addEventListener("click", () => {
                delete window.__layaDocumentId;
                document.documentElement.innerHTML = "<p>Arrived</p>";
              });
            </script>
            """
        )
        engine._refresh_elements()
        leave = _by_name(engine.full_elements, "Leave")
        engine.pending = {"kind": "action", "prompt": "choose"}
        changed = engine.reply(
            None,
            None,
            payload={
                "actions": [
                    {"action": "click", "element_id": leave["id"]},
                    {"action": "click", "element_id": leave["id"]},
                ]
            },
        )
        assert changed["status"] == "needs_action"
        assert "page changed" in changed["prompt"]
        assert continued["n"] == 1
        browser.close()


if __name__ == "__main__":
    test_observer_fields_dialog_state_and_stable_ids()
    test_engine_actions_tabs_screenshot_and_recovery()
    test_confident_uncheck_and_select_run_locally()
    test_action_list_and_choice_packet()
    print("driver tests ok")
