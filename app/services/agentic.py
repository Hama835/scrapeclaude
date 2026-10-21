"""AgenticScraper: follow plain-English steps in a real browser (click, type, scroll...), then extract.

Each step is planned by Claude from a numbered list of the page's interactive elements.
"""
from __future__ import annotations

from .. import cleaner, fetcher
from ..config import settings
from ..llm import get_llm
from .common import Usage, extract

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["click", "fill", "press", "select", "scroll", "wait", "goto", "done"]},
        "index": {"type": ["integer", "null"], "description": "Element number from the list"},
        "value": {"type": ["string", "null"], "description": "Text to type, key to press, option, or URL"},
        "reason": {"type": "string"},
    },
    "required": ["action", "index", "value", "reason"],
    "additionalProperties": False,
}

PLANNER_SYSTEM = (
    "You control a web browser. Given the current page's interactive elements and ONE step to "
    "perform, reply with the single browser action that accomplishes it. Use 'fill' to type into "
    "inputs (value = text), 'press' for keys like Enter (index = element or null), 'select' for "
    "dropdowns, 'goto' to open a URL, 'scroll' to scroll down, 'wait' if the page is still loading, "
    "'done' if the step is already complete. Respond with JSON only."
)

# Marks every visible interactive element with data-sc-idx and returns a compact description.
LIST_ELEMENTS_JS = r"""
() => {
  const sel = 'a[href], button, input, textarea, select, [role=button], [role=link], [onclick], [contenteditable=true], summary, label';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    el.setAttribute('data-sc-idx', String(i));
    const attrs = [];
    for (const a of ['type','name','placeholder','aria-label','href','title']) {
      const v = el.getAttribute(a); if (v) attrs.push(`${a}="${v.slice(0,80)}"`);
    }
    if ('value' in el && el.value) attrs.push(`current_value="${String(el.value).slice(0,80)}"`);
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g,' ').slice(0,100);
    out.push(`[${i}] <${el.tagName.toLowerCase()} ${attrs.join(' ')}> ${text}`);
    i++;
    if (i >= 300) break;
  }
  return out.join('\n');
}
"""


async def agentic(*, url: str, steps: list[str], user_prompt: str | None = None,
                  output_schema: dict | None = None, ai_extraction: bool = True,
                  headers: dict | None = None) -> tuple[dict, Usage]:
    if not settings.browser_enabled:
        raise RuntimeError("AgenticScraper needs the browser (BROWSER_ENABLED=true).")
    usage = Usage()
    llm = get_llm()
    await fetcher.assert_public_url(url)
    log: list[dict] = []

    ctx = await fetcher.browser.new_context(headers)
    try:
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=settings.fetch_timeout_s * 1000)
        await _settle(page)

        for step in steps:
            done_for_step: list[dict] = []
            for attempt in range(3):  # a step may need a couple of actions (e.g. fill then Enter)
                elements = await page.evaluate(LIST_ELEMENTS_JS)
                content = f"Current URL: {page.url}\nTitle: {await page.title()}\n\nElements:\n{elements}"
                instruction = f"Step to perform: {step}"
                if done_for_step:
                    instruction += (
                        "\n\nActions already taken for this step: "
                        + "; ".join(f"{a['action']} #{a.get('index')} {a.get('value') or ''}".strip() for a in done_for_step)
                        + "\nIf the step is now complete, answer with action 'done'."
                    )
                plan = await llm.extract(content, instruction, ACTION_SCHEMA, system=PLANNER_SYSTEM)
                usage.add(plan)
                act = plan.data
                if act.get("action") == "done":
                    break
                done_for_step.append(act)
                entry = {"step": step, "action": act}
                try:
                    finished = await _do(page, act)
                    entry["ok"] = True
                except Exception as e:  # report and continue with the next step
                    entry["ok"], entry["error"] = False, str(e)[:300]
                    finished = True
                log.append(entry)
                await _settle(page)
                if finished:
                    break

        html = await page.content()
        final_url = page.url
        if url != final_url:
            await fetcher.assert_public_url(final_url)
    finally:
        await ctx.close()

    markdown = cleaner.html_to_markdown(html, final_url)
    out: dict = {"steps_log": log, "final_url": final_url}
    if ai_extraction and user_prompt:
        out["result"] = await extract(markdown, user_prompt, output_schema, usage)
    else:
        out["markdown"] = markdown
    return out, usage


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


async def _do(page, act: dict) -> bool:
    """Run one action. Returns True when the step is complete."""
    kind, idx, value = act.get("action"), act.get("index"), act.get("value")
    loc = page.locator(f'[data-sc-idx="{idx}"]').first if idx is not None else None
    if kind == "click" and loc:
        await loc.click(timeout=8000)
    elif kind == "fill" and loc:
        await loc.fill(value or "", timeout=8000)
        return False  # re-check: the step may also ask to submit / press Enter
    elif kind == "press":
        if loc:
            await loc.press(value or "Enter", timeout=8000)
        else:
            await page.keyboard.press(value or "Enter")
    elif kind == "select" and loc:
        await loc.select_option(label=value or "", timeout=8000)
    elif kind == "scroll":
        await fetcher.auto_scroll(page, 1)
    elif kind == "wait":
        await page.wait_for_timeout(1500)
        return False
    elif kind == "goto" and value:
        await fetcher.assert_public_url(value)
        await page.goto(value, wait_until="domcontentloaded")
    elif kind == "done":
        return True
    else:
        raise ValueError(f"Could not perform action {act}")
    return True
