"""Self-contained, escaped, offline HTML review with a restrictive CSP."""

from __future__ import annotations

import base64
import hashlib
import html
import json
from collections.abc import Sequence
from typing import Any

from opencollate.reporters.common import diagnostics, report_dict

_STYLE = """
:root{color-scheme:light dark;--bg:#f5f7fa;--panel:#fff;--ink:#192738;
--muted:#556575;--line:#d5dde6;--accent:#205bc5;--error:#b72237}
@media(prefers-color-scheme:dark){:root{--bg:#101720;--panel:#192330;
--ink:#e8edf4;--muted:#a9b8ca;--line:#354457;--accent:#8ab9ff;--error:#ff9ba6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:32px 24px}
header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:24px}
.eyebrow{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
h1{font-size:30px;margin:8px 0;line-height:1.2;overflow-wrap:anywhere}
.note{color:var(--muted);max-width:85ch}nav{display:flex;gap:12px;flex-wrap:wrap;
background:var(--panel);border:1px solid var(--line);padding:18px;border-radius:10px}
label{font-size:13px;color:var(--muted);display:grid;gap:5px}.search{flex:1;min-width:220px}
input,select,button{font:inherit;padding:9px 12px;border:1px solid var(--line);
border-radius:6px;background:var(--panel);color:var(--ink)}button{cursor:pointer}
button:disabled{opacity:.4;cursor:default}input:focus,select:focus,button:focus,summary:focus{
outline:3px solid var(--accent);outline-offset:2px}.stats{display:flex;gap:20px;flex-wrap:wrap}
.stats strong{font-size:23px}.finding{background:var(--panel);border:1px solid var(--line);
border-radius:10px;margin:15px 0;padding:20px;overflow-wrap:anywhere}
.finding h2{font-size:17px;margin:8px 0}.meta{font:13px ui-monospace,monospace;color:var(--muted)}
.badge{display:inline-block;font-size:11px;letter-spacing:.06em;font-weight:700;
border:1px solid var(--line);padding:3px 8px;border-radius:4px;margin-right:8px}
.error,.fatal{color:var(--error)}a{color:var(--accent)}summary{cursor:pointer;color:var(--accent)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace;
padding:14px;background:var(--bg);border-radius:6px}.toolbar{display:flex;align-items:center;
justify-content:space-between;gap:12px;margin:18px 0}.toolbar p{margin:0}
[hidden]{display:none!important}.help{border-left:3px solid var(--accent);padding-left:12px}
footer{margin:28px 0;color:var(--muted);font-size:12px}
@media print{nav,.toolbar button{display:none}body{background:white;color:black}
.finding{break-inside:avoid}.finding[hidden]{display:block!important}}
""".strip()

_SCRIPT = """
'use strict';
const cards=Array.from(document.querySelectorAll('.finding'));
const search=document.getElementById('search');
const severity=document.getElementById('severity');
const rule=document.getElementById('rule');
const state=document.getElementById('state');
const counter=document.getElementById('counter');
const prev=document.getElementById('previous');
const next=document.getElementById('next');
let page=0;const size=50;
const texts=cards.map(c=>c.textContent.toLocaleLowerCase());
function render(reset){
 if(reset)page=0;
 const term=search.value.toLocaleLowerCase().trim();
 const selected=cards.filter((c,i)=>(!term||texts[i].includes(term))&&
 (!severity.value||c.dataset.severity===severity.value)&&
 (!rule.value||c.dataset.rule===rule.value)&&(!state.value||c.dataset.state===state.value));
 page=Math.min(page,Math.max(0,Math.ceil(selected.length/size)-1));
 cards.forEach(c=>{c.hidden=true;});
 selected.slice(page*size,(page+1)*size).forEach(c=>{c.hidden=false;});
 counter.textContent=selected.length+' matching findings · page '+(page+1)+' of '+
 Math.max(1,Math.ceil(selected.length/size));
 prev.disabled=page===0;next.disabled=(page+1)*size>=selected.length;
 document.getElementById('empty').hidden=selected.length!==0;
}
[search,severity,rule,state].forEach(e=>e.addEventListener('input',()=>render(true)));
prev.addEventListener('click',()=>{page--;render(false);});
next.addEventListener('click',()=>{page++;render(false);});
document.getElementById('reset').addEventListener('click',()=>{
 [search,severity,rule,state].forEach(e=>{e.value='';});render(true);search.focus();});
render(true);
""".strip()


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _hash(value: str) -> str:
    return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode("ascii")


def _json(value: Any) -> str:
    return _escape(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False))


def render_html(result: object) -> str:
    """Render all evidence as data; never load scripts, fonts, or source files."""

    report = report_dict(result)
    if "findings" in report and "diagnostics" not in report:
        rows = []
        for entry in report["findings"]:
            item = dict(entry.get("current") or entry.get("baseline") or {})
            item["review_state"] = entry.get("state", "")
            item["review_evidence"] = entry
            rows.append(item)
        report = {**report, "diagnostics": rows}
    findings = diagnostics(report, include_suppressed=True)
    rules = sorted({str(item.get("code", "")) for item in findings})
    exit_code = report.get("exit_code")
    status = (
        "Analysis incomplete"
        if exit_code == 2
        else "Violations found"
        if exit_code == 1
        else "Review findings"
    )
    cards = []
    for index, item in enumerate(findings):
        code = str(item.get("code", ""))
        severity = str(item.get("severity", "info"))
        review_state = str(
            item.get("review_state")
            or ("waived" if item.get("waived") or item.get("suppressed") else "active")
        )
        title = _escape(item.get("message", ""))
        evidence = {
            key: item[key]
            for key in (
                "evidence",
                "metadata",
                "location",
                "object",
                "property",
                "waiver_reason",
                "review_evidence",
            )
            if key in item
        }
        help_text = '<p class="help">' + _escape(item["help"]) + "</p>" if item.get("help") else ""
        cards.append(
            f'<article class="finding" id="finding-{index + 1}" '
            f'data-severity="{_escape(severity)}" '
            f'data-rule="{_escape(code)}" data-state="{_escape(review_state)}">'
            f'<span class="badge {_escape(severity)}">{_escape(severity.upper())}</span>'
            f'<span class="badge">{_escape(code)}</span>'
            f'<span class="badge">{_escape(review_state)}</span>'
            f'<h2>{title}</h2><p class="meta">Fingerprint: '
            f"{_escape(item.get('fingerprint', 'not supplied'))}</p>"
            f"{help_text}<details><summary>Source evidence and diagnostic details</summary>"
            f"<pre>{_json(evidence)}</pre></details></article>"
        )
    state_names = sorted(
        {
            str(
                item.get("review_state")
                or ("waived" if item.get("waived") or item.get("suppressed") else "active")
            )
            for item in findings
        }
    )

    def options(values: Sequence[str]) -> str:
        return "".join(
            f'<option value="{_escape(value)}">{_escape(value)}</option>' for value in values
        )

    csp = (
        f"default-src 'none'; script-src 'sha256-{_hash(_SCRIPT)}'; "
        f"style-src 'sha256-{_hash(_STYLE)}'; base-uri 'none'; form-action 'none'"
    )
    project = _escape(report.get("project", "OpenCollate"))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_escape(csp)}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenCollate review — {project}</title><style>{_STYLE}</style></head>
<body><main><header><div class="eyebrow">OpenCollate / offline evidence review</div>
<h1>{project}</h1><p>{status}</p>
<div class="stats"><span><strong>{len(findings)}</strong> recorded findings</span></div>
<p class="note">Absence of a reported mismatch is not proof of complete analysis or signoff.
Review unknowns, unsupported constructs, taint, and waivers. This file contains design data;
protect it like the original collateral.</p></header>
<nav aria-label="Finding filters"><label class="search">Search evidence
<input id="search" type="search"
placeholder="Rule, object, path, or message" autocomplete="off"></label>
<label>Severity<select id="severity"><option value="">All severities</option>
{options(["fatal", "error", "warning", "info"])}</select></label>
<label>Rule<select id="rule"><option value="">All rules</option>{options(rules)}</select></label>
<label>State<select id="state"><option value="">All states</option>
{options(state_names)}</select></label>
<button id="reset" type="button">Reset filters</button></nav>
<div class="toolbar"><p id="counter" role="status" aria-live="polite">All findings shown</p>
<div><button id="previous" type="button">Previous</button>
<button id="next" type="button">Next</button></div></div>
<p id="empty" hidden>No findings match the current filters.</p>
<noscript><p>Filtering requires JavaScript.
All findings and expandable evidence remain available below.</p></noscript>
<section aria-label="Diagnostics">{"".join(cards)}</section>
<details><summary>Complete machine-readable report</summary><pre>{_json(report)}</pre></details>
<footer>Self-contained report. No remote assets, source-file requests, or telemetry.</footer>
</main><script>{_SCRIPT}</script></body></html>
'''
