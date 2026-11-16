"""Reading and filling an application form in a real (visible) browser page.

Works on any ATS by reading labels, which is how Greenhouse, Lever, Ashby and most custom forms are
built. A guard injected into every page blocks form submission while jobagent is filling, so nothing
can be submitted except by the user's own click after filling ends.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

# Installed in every page (also after navigation). Off by default; switched on only while filling.
GUARD = """
(() => {
  if (window.__jobagentGuardInstalled) return;
  window.__jobagentGuardInstalled = true;
  window.__jobagentGuard = false;
  const block = e => { e.preventDefault(); e.stopImmediatePropagation(); };
  document.addEventListener('submit', e => { if (window.__jobagentGuard) block(e); }, true);
  document.addEventListener('click', e => {
    if (!window.__jobagentGuard) return;
    const b = e.target.closest && e.target.closest('button, input[type=submit], a');
    if (b && /submit|apply|send|next|continue|review/i.test((b.innerText || b.value || '').trim())) block(e);
  }, true);
  document.addEventListener('keydown', e => { if (window.__jobagentGuard && e.key === 'Enter') block(e); }, true);
})();
"""

# Returns one entry per question: text-like fields, selects, comboboxes, radio/checkbox groups,
# Yes/No button pairs and file inputs. Elements get a data-ja id so Python can find them again.
COLLECT = """() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const txt = el => (el && el.innerText || '').replace(/\\s+/g, ' ').trim();
  let n = 0; const tag = el => { if (!el.dataset.ja) el.dataset.ja = 'ja' + (n++) + '_' + Math.random().toString(36).slice(2, 6); return el.dataset.ja; };
  const ownLabel = el => {
    if (el.labels && el.labels.length) return txt(el.labels[0]);
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(' ').map(i => txt(document.getElementById(i))).join(' ');
    return '';
  };
  // The question text for a field or group of options: the group's own accessible label if it has one,
  // else the nearest label/legend that comes BEFORE it (a question's text never follows its answers).
  const before = (c, el) => !!(c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
  const question = (el, skip) => {
    let a = el.parentElement;
    for (let i = 0; a && i < 7; i++, a = a.parentElement) {
      const by = a.getAttribute && a.getAttribute('role') !== 'radio' && a.getAttribute('aria-labelledby');
      if (by) {
        const t = by.split(' ').map(id => txt(document.getElementById(id))).join(' ').trim();
        if (t && !skip.includes(t)) return t;
      }
    }
    a = el.parentElement;
    for (let i = 0; a && i < 7; i++, a = a.parentElement) {
      const found = [...a.querySelectorAll('legend, label, [class*=label], [class*=question], h3, h4')]
        .filter(c => { const t = txt(c); return t && !skip.includes(t) && t.length < 400 && !c.contains(el) && before(c, el); });
      if (found.length) return txt(found[found.length - 1]);
    }
    return '';
  };
  const out = [];
  const required = el => !!(el.required || el.getAttribute('aria-required') === 'true');
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'radio', 'checkbox', 'search'].includes(type)) continue;
    if (type !== 'file' && (!vis(el) || el.disabled || el.readOnly)) continue;
    let label = ownLabel(el) || el.getAttribute('placeholder') || '';
    if (type === 'file') {
      const images = el.accept && el.accept.split(',').every(a => /image|\\.(jpe?g|png|gif)/i.test(a));
      label = [label, question(el, []), el.id, el.name, images ? 'photo' : ''].join(' ');
    } else if (!label.trim()) continue;  // unlabelled helper inputs inside widgets (search boxes, hidden repeats)
    const kind = type === 'file' ? 'file' : el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea'
      : el.getAttribute('role') === 'combobox' ? 'combobox' : 'text';
    out.push({id: tag(el), kind, label: label.trim(), required: required(el),
              filled: type === 'file' ? el.files.length > 0 : kind === 'select' ? el.selectedIndex > 0
                : !!el.value && kind !== 'combobox',
              options: kind === 'select' ? [...el.options].map(o => o.text.trim()).filter(Boolean) : []});
  }
  const groups = {};
  for (const el of document.querySelectorAll('input[type=radio], input[type=checkbox]')) {
    if (!vis(el) && !(el.labels && el.labels.length && vis(el.labels[0]))) continue;
    (groups[el.type + ':' + (el.name || el.id)] = groups[el.type + ':' + (el.name || el.id)] || []).push(el);
  }
  for (const [key, els] of Object.entries(groups)) {
    const opts = els.map(e => ownLabel(e) || e.value);
    out.push({id: els.map(tag).join(','), kind: key.startsWith('radio') ? 'radio' : 'checkbox',
              label: els.length === 1 && key.startsWith('checkbox') ? opts[0] : question(els[0], opts),
              required: els.some(required), filled: els.some(e => e.checked), options: opts});
  }
  // Dropdowns built from a button that opens a list (Workday and similar): the label is on the button.
  for (const btn of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
    if (!vis(btn)) continue;
    const aria = btn.getAttribute('aria-label') || '';
    const label = (ownLabel(btn) || question(btn, []) || aria).replace(/\\s*(select one|required)\\s*/gi, ' ').trim();
    if (!label) continue;
    out.push({id: tag(btn), kind: 'dropdown', label, required: /required/i.test(aria),
              filled: !!txt(btn) && !/select one/i.test(txt(btn)), options: []});
  }
  for (const yes of document.querySelectorAll('button')) {
    if (txt(yes) !== 'Yes' || !vis(yes)) continue;
    const no = [...yes.parentElement.querySelectorAll('button')].find(b => txt(b) === 'No');
    if (!no) continue;
    out.push({id: tag(yes) + ',' + tag(no), kind: 'yesno', label: question(yes, ['Yes', 'No']), required: false,
              filled: false, options: ['Yes', 'No']});
  }
  return out;
}"""


@dataclass
class Question:
    id: str
    kind: str
    label: str
    required: bool
    filled: bool
    options: list[str] = field(default_factory=list)


def collect(page) -> list[Question]:
    return [Question(**q) for q in page.evaluate(COLLECT)]


def guard(page, on: bool) -> None:
    page.evaluate(GUARD)
    page.evaluate(f"window.__jobagentGuard = {'true' if on else 'false'}")


def pause(lo: float = 0.25, hi: float = 0.8) -> None:
    """A short random wait between actions, like a person moving between fields."""
    time.sleep(random.uniform(lo, hi))


def best_option(options: list[str], want: str) -> int | None:
    """Index of the option that matches the wanted answer, or None."""
    w = want.strip().lower()
    low = [o.strip().lower() for o in options]
    word = re.compile(rf"\b{re.escape(w)}\b")  # whole words: "Asian" must not match "Caucasian"
    for test in (lambda o: o == w, lambda o: o.startswith(w), lambda o: bool(word.search(o)),
                 lambda o: o and o in w and len(o) > 2):
        for i, o in enumerate(low):
            if test(o):
                return i
    return None


# Other ways a choice list may word an answer ("SharkNinja Career Website" for "Company website").
ALTERNATES = {"company website": ["website", "career"]}


def fill(page, q: Question, value: str) -> bool:
    """Put value into the question's field, trying other wordings for choice lists. True if it took."""
    choices = [v.strip() for v in value.split(" | ") if v.strip()]  # answers.yaml alternatives
    if q.kind in ("select", "combobox", "radio", "yesno", "dropdown"):
        return any(_fill(page, q, v) for v in [*choices, *ALTERNATES.get(value.lower(), [])])
    return _fill(page, q, choices[0] if " | " in value else value)


def _fill(page, q: Question, value: str) -> bool:
    ids = q.id.split(",")
    loc = page.locator(f'[data-ja="{ids[0]}"]')
    if q.kind == "file":
        loc.set_input_files(value)
        return True
    if q.kind in ("text", "textarea"):
        loc.click()
        loc.fill("")
        if len(value) > 120:
            loc.fill(value)
        else:
            loc.press_sequentially(value, delay=random.randint(15, 45))
        if not same_text(loc.input_value(), value):
            # Typing can be lost while a page is still settling (the first field especially); set it directly.
            page.wait_for_timeout(500)
            loc.fill(value)
        return same_text(loc.input_value(), value)
    if q.kind == "select":
        i = best_option(q.options, value)
        if i is None:
            return False
        loc.select_option(label=q.options[i])
        return True
    if q.kind in ("radio", "yesno"):
        i = best_option(q.options, value)
        if i is None:
            return False
        target = page.locator(f'[data-ja="{ids[i]}"]')
        if q.kind == "yesno":
            target.click()
            return True
        try:
            target.check(force=True)
        except Exception:  # noqa: BLE001 - custom-styled radios hide the real input
            pass
        if not target.is_checked():
            # Click what the user would click: the option's visible label.
            target.evaluate("e => (e.closest('label') || document.querySelector(`label[for='${e.id}']`) || e).click()")
        return target.is_checked()
    if q.kind == "dropdown":
        # Open the list, pick the matching option; type-ahead if the list is long.
        loc.click()
        options = page.locator("[role=option]:visible")
        try:
            options.first.wait_for(state="visible", timeout=4000)
        except Exception:  # noqa: BLE001 - no list appeared
            page.keyboard.press("Escape")
            return False
        i = best_option(options.all_inner_texts(), value)
        if i is None:
            page.keyboard.press("Escape")
            return False
        options.nth(i).click()
        return True
    if q.kind == "combobox":
        # Type to search, then click the matching option in the list that appears.
        query = value.split(",")[0] if len(value) > 20 or "," in value else value
        loc.click()
        loc.fill("")
        loc.press_sequentially(query, delay=random.randint(30, 60))
        # Only options that are showing: some widgets (e.g. phone country pickers) keep hidden lists.
        options = page.locator("[role=option]:visible")
        try:
            options.first.wait_for(state="visible", timeout=4000)
        except Exception:  # noqa: BLE001 - no list appeared
            page.keyboard.press("Escape")
            loc.fill("")
            return False
        texts = options.all_inner_texts()
        i = best_option(texts, value) if best_option(texts, value) is not None else best_option(texts, query)
        if i is None:
            page.keyboard.press("Escape")
            loc.fill("")
            return False
        options.nth(i).click()
        return True
    return False


def country_code(page, q: Question) -> str | None:
    """The code shown by a country-code picker next to the phone field ("+1", "+91"), "?" if there is a picker
    without a visible code, or None when the field has no picker."""
    return page.evaluate("""([id]) => {
      const el = document.querySelector(`[data-ja="${id}"]`);
      let a = el && el.parentElement;
      for (let i = 0; a && i < 3; i++, a = a.parentElement) {
        // Stop before reaching a container of other questions.
        if (a.querySelectorAll('input:not([type=hidden]), textarea, select').length > 2) break;
        const m = (a.innerText || '').match(/\\+\\d{1,3}(?!\\d)/);
        if (m) return m[0];
        if (a.querySelector('.iti__selected-country, .iti__flag-container, select[name*=country i]')) return '?';
      }
      return null;
    }""", [q.id.split(",")[0]])


def pick_us_country_code(page, q: Question) -> bool:
    """Switch a phone field's country picker (often +91 from browsing in India) to United States (+1)."""
    picker = page.evaluate_handle("""id => {
      let a = document.querySelector(`[data-ja="${id}"]`).parentElement;
      for (let i = 0; a && i < 3; i++, a = a.parentElement) {
        const b = a.querySelector('.iti__selected-country, button[aria-label*="country" i], [aria-haspopup][class*=country i]')
          || [...a.querySelectorAll('button, [role=button], [role=combobox]')].find(b => /^\\+\\d{1,3}$/.test((b.innerText || '').trim()));
        if (b) return b;
      }
      return null;
    }""", q.id.split(",")[0]).as_element()
    if picker is None or not picker.is_visible():
        return False  # e.g. Greenhouse hides it and sets the code from its Country field
    try:
        picker.click(timeout=3000)
    except Exception:  # noqa: BLE001 - leave the code as it is; the full +1 number is typed instead
        return False
    page.wait_for_timeout(500)
    us = page.locator("[role=option]:visible", has_text=re.compile(r"^\s*United States\b"))
    if us.count() == 0:
        page.keyboard.type("United States", delay=30)  # pickers with a search box
        page.wait_for_timeout(500)
    if us.count() == 0:
        page.keyboard.press("Escape")
        return False
    us.first.click()
    page.wait_for_timeout(300)
    return country_code(page, q) == "+1"


def same_text(actual: str, wanted: str) -> bool:
    """Forms reformat what you type ("+1 (555) 010-0123" -> "(555) 010-0123"); compare letters and digits."""
    a, w = (re.sub(r"\W", "", x.lower()) for x in (actual, wanted))
    return bool(a) and (a == w or (len(w) >= 10 and w[-10:] in a))


def mark(page, q: Question, color: str) -> None:
    """Outline a field so the user can spot it: yellow = drafted by Claude, red = needs you."""
    first = q.id.split(",")[0]
    page.evaluate("""([id, color]) => {
      const el = document.querySelector(`[data-ja="${id}"]`);
      const box = el && (el.closest('[class*=field], [class*=question], fieldset, li, div') || el);
      if (box) { box.style.outline = `3px solid ${color}`; box.style.outlineOffset = '3px'; }
    }""", [first, color])


CAPTCHA_HOSTS = ("recaptcha", "hcaptcha.com", "challenges.cloudflare.com", "arkoselabs", "funcaptcha",
                 "captcha-delivery.com")


def has_captcha(page) -> bool:
    """A CAPTCHA the user must solve (not the invisible reCAPTCHA v3 badge)."""
    for frame in page.frames:
        url = frame.url
        if ("recaptcha" in url and "anchor" in url and "size=invisible" not in url) or any(
                h in url for h in CAPTCHA_HOSTS[1:]):
            return True
    return False


def needs_login(page, questions: list[Question]) -> bool:
    return page.locator("input[type=password]:visible").count() > 0 and len(questions) < 4


# Buttons that open an application from a job description page. "Apply with LinkedIn", "Easy Apply" and the like
# are never clicked: they hand the application to another site.
APPLY_BUTTON = re.compile(r"^(apply( now| online| here| for this (job|position|role))?|i'?m interested|"
                          r"start (my |your )?application|apply to (this )?job)$", re.I)
NOT_THIS_BUTTON = re.compile(r"linkedin|indeed|google|seek|easy|quick|\bwith\b", re.I)


def is_application_form(questions: list[Question]) -> bool:
    """A real application asks for a resume or an email; a job description page doesn't."""
    return any(q.kind == "file" or re.search(r"e-?mail", q.label, re.I) for q in questions)


def open_application(page):
    """On a job description page, click its Apply button once to reach the form.

    Only called before filling starts and only when no application form is on the page, so the click
    navigates and can't submit anything. Returns the page holding the form (some sites open a new tab)."""
    if is_application_form(collect(page)):
        return page
    for el in page.locator("a, button, [role=button], input[type=submit]").all():
        try:
            if not el.is_visible():
                continue
            text = " ".join((el.inner_text() or el.get_attribute("value") or "").split())
        except Exception:  # noqa: BLE001 - element went away
            continue
        if not APPLY_BUTTON.match(text) or NOT_THIS_BUTTON.search(text):
            continue
        before = len(page.context.pages)
        el.click()
        page.wait_for_timeout(1500)
        if len(page.context.pages) > before:  # opened in a new tab
            page = page.context.pages[-1]
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - slow pages still get a chance
            pass
        page.wait_for_timeout(1500)
        return page
    return page


# Cookie banners cover forms and block clicks. Only the most private choice is ever clicked.
DECLINE_COOKIES = re.compile(r"^(decline( all)?|decline optional cookies|reject( all)?( cookies)?|deny( all)?|only (necessary|essential)( cookies)?|"
                             r"(use )?(necessary|essential) (cookies )?only|refuse( all)?)$", re.I)


def decline_cookies(page) -> bool:
    """Click a cookie banner's Decline/Reject button if there is one. Never accepts."""
    for el in page.locator("button, [role=button], a").all():
        try:
            if el.is_visible() and DECLINE_COOKIES.match(" ".join(el.inner_text().split())):
                el.click()
                page.wait_for_timeout(500)
                return True
        except Exception:  # noqa: BLE001 - element went away
            continue
    return False
