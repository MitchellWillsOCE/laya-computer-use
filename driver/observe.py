"""Read visible controls from the page and tag them with stable IDs."""

from __future__ import annotations

OBSERVE_JS = r"""
({ cap, wholePage }) => {
  window.__layaStableIds ||= new Map();
  window.__layaNextId ||= 1;
  window.__layaDocumentId ||= (
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  );

  const sel = [
    "a", "button", "input", "select", "textarea", "summary",
    "[role='button']", "[role='link']", "[role='textbox']",
    "[role='searchbox']", "[role='combobox']", "[role='checkbox']",
    "[role='radio']", "[role='tab']", "[role='menuitem']", "[role='option']",
    "[role='listitem']", "[role='article']", "[role='list']", "[role='listbox']",
    "[contenteditable]:not([contenteditable='false'])", "article", "li",
  ].join(",");

  const clean = (value, max = 300) =>
    String(value || "").replace(/\s+/g, " ").trim().slice(0, max);

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width >= 2 && rect.height >= 2 &&
      style.visibility !== "hidden" && style.display !== "none" &&
      Number(style.opacity) !== 0;
  };

  const referencedText = (raw) =>
    clean((raw || "").split(/\s+/).map((id) => {
      const node = document.getElementById(id);
      return node ? (node.innerText || node.textContent || "") : "";
    }).join(" "));

  const dialogs = Array.from(
    document.querySelectorAll(
      "dialog[open], [role='dialog'], [role='alertdialog'], [aria-modal='true']"
    )
  ).filter(isVisible).map((el, index) => {
    const style = getComputedStyle(el);
    const z = Number.parseInt(style.zIndex, 10);
    const topLayer = el.tagName === "DIALOG" && el.open ? 1_000_000_000 : 0;
    const modal = el.getAttribute("aria-modal") === "true" ? 1_000_000 : 0;
    return { el, score: topLayer + modal + (Number.isFinite(z) ? z : 0), index };
  }).sort((a, b) => a.score - b.score || a.index - b.index);
  const dialog = !wholePage && dialogs.length ? dialogs[dialogs.length - 1].el : null;
  const scope = dialog || document;
  const candidates = Array.from(scope.querySelectorAll(sel));
  scope.querySelectorAll("*").forEach((el) => {
    const style = getComputedStyle(el);
    const scrollable =
      el.scrollHeight > el.clientHeight + 5 &&
      ["auto", "scroll"].includes(style.overflowY);
    if (scrollable && !candidates.includes(el)) candidates.push(el);
  });
  const occurrences = new Map();
  const usedIds = new Set();
  const out = [];

  for (const el of candidates) {
    const rect = el.getBoundingClientRect();
    if (!isVisible(el)) continue;
    if (!dialog && (rect.bottom < 0 || rect.top > window.innerHeight)) continue;

    const role = (el.getAttribute("role") || el.tagName || "").toLowerCase();
    const labelledBy = referencedText(el.getAttribute("aria-labelledby"));
    const labelText = clean(
      el.labels ? Array.from(el.labels).map((label) => label.innerText || label.textContent || "").join(" ") : ""
    );
    const name = clean(
      el.getAttribute("aria-label") ||
      labelledBy ||
      labelText ||
      el.getAttribute("placeholder") ||
      el.getAttribute("title") ||
      el.innerText ||
      el.getAttribute("name") ||
      "",
      160
    );
    if (/^skip to\b/i.test(name)) continue;

    const value = clean("value" in el ? el.value : "", 300);
    const type = (el.getAttribute("type") || "").toLowerCase();
    const tag = el.tagName.toLowerCase();
    const isFormField =
      ["input", "select", "textarea"].includes(tag) ||
      el.isContentEditable ||
      ["checkbox", "radio"].includes(type) ||
      ["textbox", "searchbox", "combobox", "checkbox", "radio"].includes(role);
    const style = getComputedStyle(el);
    const scrollable =
      el.scrollHeight > el.clientHeight + 5 &&
      ["auto", "scroll"].includes(style.overflowY);
    if (!name && !value && !isFormField && !scrollable) continue;

    const ariaChecked = el.getAttribute("aria-checked");
    let checked = null;
    if (type === "checkbox" || type === "radio" || role === "checkbox" || role === "radio" || ariaChecked !== null) {
      checked = ariaChecked === "mixed" ? "mixed" :
        ariaChecked !== null ? ariaChecked === "true" : Boolean(el.checked);
    }
    const selected = tag === "select"
      ? clean(Array.from(el.selectedOptions || []).map((option) => option.textContent || option.value).join(", "))
      : "";
    const describedBy = referencedText(
      [el.getAttribute("aria-describedby"), el.getAttribute("aria-errormessage")]
        .filter(Boolean).join(" ")
    );
    const ariaInvalid = el.getAttribute("aria-invalid");
    const invalid =
      (ariaInvalid !== null && ariaInvalid !== "false") ||
      Boolean(el.validity && !el.validity.valid);

    const baseKey = [
      role, type, name, el.getAttribute("name") || "", el.id || "",
      el.href || "", Math.round(rect.left + window.scrollX),
      Math.round(rect.top + window.scrollY),
    ].join("|");
    const occurrence = occurrences.get(baseKey) || 0;
    occurrences.set(baseKey, occurrence + 1);
    const stableKey = `${baseKey}|${occurrence}`;
    const existingId = el.getAttribute("data-laya-id");
    let id = (
      existingId?.startsWith(`${window.__layaDocumentId}:`)
        ? existingId
        : window.__layaStableIds.get(stableKey)
    );
    if (!id || usedIds.has(id)) {
      id = `${window.__layaDocumentId}:${window.__layaNextId++}`;
    }
    usedIds.add(id);
    window.__layaStableIds.set(stableKey, id);
    el.setAttribute("data-laya-id", id);

    out.push({
      id, role, name, value, type, tag,
      href: el.href || "",
      checked,
      selected,
      required: Boolean(el.required) || el.getAttribute("aria-required") === "true",
      disabled:
        Boolean(el.disabled) ||
        el.matches(":disabled") ||
        el.getAttribute("aria-disabled") === "true",
      invalid,
      error: describedBy,
      dialog: Boolean(dialog),
      form_field: isFormField,
      contenteditable: Boolean(el.isContentEditable),
      scrollable,
      x: Math.round(rect.x),
      y: Math.round(rect.y),
    });
    if (cap > 0 && out.length >= cap) break;
  }
  return out;
}
"""

READ_TEXT_JS = r"""
({ elementId, limit }) => {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width >= 2 && rect.height >= 2 &&
      style.visibility !== "hidden" && style.display !== "none" &&
      Number(style.opacity) !== 0;
  };
  const referencedText = (raw) =>
    clean((raw || "").split(/\s+/).map((id) => {
      const node = document.getElementById(id);
      return node ? (node.innerText || node.textContent || "") : "";
    }).join(" "));

  let root = elementId
    ? document.querySelector(`[data-laya-id="${CSS.escape(String(elementId))}"]`)
    : null;
  if (!root && !elementId) {
    const dialogs = Array.from(
      document.querySelectorAll(
        "dialog[open], [role='dialog'], [role='alertdialog'], [aria-modal='true']"
      )
    ).filter(visible).map((el, index) => {
      const style = getComputedStyle(el);
      const z = Number.parseInt(style.zIndex, 10);
      const topLayer = el.tagName === "DIALOG" && el.open ? 1_000_000_000 : 0;
      const modal = el.getAttribute("aria-modal") === "true" ? 1_000_000 : 0;
      return { el, score: topLayer + modal + (Number.isFinite(z) ? z : 0), index };
    }).sort((a, b) => a.score - b.score || a.index - b.index);
    root = dialogs.length
      ? dialogs[dialogs.length - 1].el
      : document.querySelector("main, [role='main']") || document.body;
  }
  if (!root) return null;

  const pieces = [];
  if (elementId) {
    pieces.push(
      root.getAttribute("aria-label") || "",
      referencedText(root.getAttribute("aria-labelledby")),
      root.labels
        ? Array.from(root.labels).map((label) => label.innerText || label.textContent || "").join(" ")
        : "",
      "value" in root ? root.value : "",
      referencedText(
        [root.getAttribute("aria-describedby"), root.getAttribute("aria-errormessage")]
          .filter(Boolean).join(" ")
      )
    );
  }
  pieces.push(root.innerText || root.textContent || "");
  root.querySelectorAll?.(
    "[aria-describedby], [aria-errormessage], [role='alert'], .error, .invalid-feedback"
  ).forEach((el) => pieces.push(el.innerText || el.textContent || ""));
  return clean(pieces.join("\n")).slice(0, limit);
}
"""


def observe(page, cap: int | None = None, whole_page: bool = False) -> list[dict]:
    elements = page.evaluate(
        OBSERVE_JS,
        {"cap": max(0, int(cap or 0)), "wholePage": bool(whole_page)},
    )
    return list(elements or [])


def read_text(page, element_id: str | None = None, limit: int = 12_000) -> str:
    result = page.evaluate(
        READ_TEXT_JS,
        {"elementId": element_id, "limit": max(1, int(limit))},
    )
    if element_id is not None and result is None:
        raise RuntimeError(f"Element {element_id!r} is no longer on the page.")
    return str(result or "")
