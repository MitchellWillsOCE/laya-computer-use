"""CLI: python -m driver serve | prompt | check | smoke"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("USE_TF", "0")
    command = (argv or sys.argv[1:]).copy()
    action = command[0] if command else "serve"
    if action == "serve":
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from driver.server import DriverServer

        DriverServer().serve()
        return
    if action == "prompt":
        print(_prompt())
        return
    if action == "check":
        _check()
        return
    if action == "smoke":
        _smoke()
        return
    print("Usage: python -m driver [serve|prompt|check|smoke]", file=sys.stderr)
    raise SystemExit(2)


def _prompt() -> str:
    root = str(ROOT)
    return f"""You are the language driver for a local computer-use loop. Laya and Chromium run on this PC. You do not click, and you do not use Grok Bot computer use.

The driver is already running at http://127.0.0.1:8765
Read the bearer token from this file and do not paste the token into the chat:
{root}\\.token

When I give you a goal, run this in PowerShell on the local computer. Replace the goal text.

$token = (Get-Content -Raw "{root}\\.token").Trim()
$headers = @{{ Authorization = "Bearer $token" }}
Invoke-RestMethod -Uri http://127.0.0.1:8765/task -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ goal = "GOAL"; max_steps = 30; observe_cap = 120; element_limit = 40 }} | ConvertTo-Json)

A new /task always replaces an old running, blocked, or failed task and does not click anything. If no URL is supplied in url or the goal, the current tab is retained. If status is done, you can still send actions; you do not need a new task to continue.

Keep the complete response object, including error, elements, and suggestion. Then follow status until it is done, aborted, or error:

- needs_text: decide the exact string yourself. If the element is a search box or form field, send only the text that belongs in that field. Then:
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ text = "TEXT"; submit = $false }} | ConvertTo-Json)
  Set submit to $true only when Enter should be pressed after typing.
  If prompt asks for a URL, send that URL as text.

- needs_action: Laya's suggestion was not confident enough to execute. elements lists only id, role, name, value, checked, selected, disabled, and href when the control has one. text is empty. Choose the next actions for this page, at most 8, and send them together:
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ actions = @(@{{ action = "uncheck"; element_id = "ID" }}, @{{ action = "click"; element_id = "ID" }}) }} | ConvertTo-Json -Depth 4)
  Use only a current element id. One action field still works:
  # click
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "click"; element_id = "ID" }} | ConvertTo-Json)
  # type, optionally submitting with Enter
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "type"; element_id = "ID"; text = "TEXT"; submit = $true }} | ConvertTo-Json)
  # select / key / checkbox / container scroll / upload
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "select"; element_id = "ID"; label = "OPTION" }} | ConvertTo-Json)
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "press"; element_id = "ID"; key = "ArrowDown" }} | ConvertTo-Json)
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "uncheck"; element_id = "ID" }} | ConvertTo-Json)
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "scroll"; element_id = "ID"; amount = 700 }} | ConvertTo-Json)
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "upload"; element_id = "ID"; path = "LOCAL_PATH" }} | ConvertTo-Json)
  Valid actions are click, type, select, press, press_enter, check, uncheck, scroll, upload, read, wait, done, blocked, navigate, new_tab, switch_tab, close_tab, and abort.

Use GET /elements?query=TEXT when a needed control is absent or you need the page text. Use action=read with an optional element_id for the full text and the fuller element record. Do not call GET /screenshot. Ignore older packets and use only the latest reply.

- needs_confirm: this is a password, payment, or delete control. Ask me before continuing. Only after I agree, send confirm, then follow the status that comes back. A typing confirmation returns needs_text, and that next call carries the text:
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "confirm" }} | ConvertTo-Json)

- blocked: read the prompt and the screenshot path in the JSON. Tell me what is on screen and what you need. After I have logged in or cleared the page in the Chromium window, send:
  Invoke-RestMethod -Uri http://127.0.0.1:8765/reply -Method Post -Headers $headers -ContentType "application/json" -Body (@{{ action = "continue" }} | ConvertTo-Json)

- done: this is only set when you send action done. Laya suggesting done is needs_action, not permission to stop. Tell me the final url and stop calling the driver only after you send action done.
- error: show me the complete error field verbatim. A new task or navigate action may recover it.

Approve the local PowerShell command when the app asks. Do not browse with computer use while this driver is working. Call /stop only if I tell you to cancel.
"""


def _check() -> None:
    os.environ["USE_TF"] = "0"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from driver.decide import build_questions, build_state, parse_decision
    from laya import Router

    device = os.environ.get("LAYA_DEVICE", "cuda")
    model = os.environ.get("LAYA_MODEL", "english")
    router = Router(preload=False, device=device, max_loaded=1)
    router.preload([model])
    elements = [
        {"id": "1", "role": "textbox", "name": "From", "value": "", "type": "text"},
        {"id": "2", "role": "button", "name": "Search", "value": "", "type": ""},
    ]
    result = router.predict(
        build_state("Find flights", "https://example.com", elements),
        build_questions(elements),
        model=model,
    )
    print(parse_decision(result.get("answers") or {}))


def _smoke() -> None:
    os.environ["USE_TF"] = "0"
    os.environ["LAYA_HEADLESS"] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from driver.engine import Engine

    engine = Engine()
    engine.start()
    try:
        result = engine.task(
            "The page has finished loading. Choose DONE.",
            "https://example.com",
            restart=True,
        )
        print(result["status"], result.get("url"), result.get("prompt"))
    finally:
        engine.close()


if __name__ == "__main__":
    main()
