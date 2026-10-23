"""Private, read-only inspection of the currently open Naukri page."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import json
import re
from .db import record_run

_PROFILE_REFRESH_TERMS = re.compile(r"\b(profile|resume)\b.*\b(refresh|update)\b|\b(refresh|update)\b.*\b(profile|resume)\b", re.IGNORECASE)


def sanitize_url(url: str) -> str:
    """Remove credentials, query strings, and fragments from a URL."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parts.scheme, host + port, parts.path, "", ""))


def build_inventory(page) -> dict:
    """Collect visible control labels without collecting typed values or page text."""
    controls = page.evaluate("""() => {
      const visible = el => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      };
      const clean = value => (value || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
      const buttons = Array.from(document.querySelectorAll('button, input[type=button], input[type=submit], [role=button]'))
        .filter(visible).slice(0, 100).map(el => ({
          tag: el.tagName.toLowerCase(),
          label: clean(el.getAttribute('aria-label') || el.getAttribute('title') || (el.tagName === 'INPUT' ? el.getAttribute('value') : el.innerText))
        }));
      const links = Array.from(document.querySelectorAll('a[href]')).filter(visible).slice(0, 150).map(el => {
        let path = '';
        try { const u = new URL(el.href); path = u.origin + u.pathname; } catch {}
        return {label: clean(el.getAttribute('aria-label') || el.innerText || el.title), path};
      });
      const inputs = Array.from(document.querySelectorAll('input, textarea, select')).filter(visible).slice(0, 100).map(el => ({
        tag: el.tagName.toLowerCase(),
        type: clean(el.getAttribute('type') || ''),
        label: clean(el.getAttribute('aria-label') || el.labels?.[0]?.innerText || ''),
        placeholder: clean(el.getAttribute('placeholder') || '')
        // Intentionally never read el.value.
      }));
      return {buttons, links, inputs};
    }""")
    return {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "page_title": page.title(),
        "page_url": sanitize_url(page.url),
        "controls": controls,
        "privacy_note": "Read-only UI inventory. Input values, cookies, and page body text are not recorded."
    }


def save_inventory(page, output_path: Path | None = None) -> Path:
    output_path = output_path or (Path("data") / "site_inspection" / "latest.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    inventory = build_inventory(page)
    output_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    record_run("profile_inspection", "captured", inventory["page_url"])
    return output_path


def verify_profile_refresh_inventory(inventory: dict) -> dict:
    """Report visible label evidence only; this does not identify selectors or act."""
    controls = inventory.get("controls")
    if not isinstance(controls, dict):
        raise ValueError("Inspection is missing a controls object")

    evidence = []
    for group in ("buttons", "links"):
        items = controls.get(group, [])
        if not isinstance(items, list):
            raise ValueError(f"Inspection controls.{group} must be a list")
        for item in items:
            if not isinstance(item, dict):
                continue
            label = item.get("label", "")
            if isinstance(label, str) and _PROFILE_REFRESH_TERMS.search(label):
                evidence.append({"kind": group[:-1], "label": label, "path": item.get("path", "")})

    return {
        "status": "candidate_labels_found" if evidence else "no_candidate_labels_found",
        "evidence": evidence,
        "live_action_enabled": False,
        "note": "Label matches are unverified candidates only. No selectors were inferred and no page action was performed.",
    }
