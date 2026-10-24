import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.conftest import PDF

PAGE = """<!doctype html><html><body>
<h1>Regulatory filing lookup</h1>
<label>Find a Case <input aria-label="Find a Case" id="case"></label>
<button onclick="openCase()">Find case</button>
<div id="content"></div>
<script>
const scenario = new URLSearchParams(location.search).get('case') || 'normal';
function openCase() {
 const number = scenario === 'wrong' ? 'M12383' : document.querySelector('#case').value;
 document.querySelector('#content').innerHTML = `
 <section><dl>
 <dt>Matter number</dt><dd>${number}</dd>
 <dt>Title</dt><dd>Halifax Regional Water Commission - Windsor Street Project</dd>
 <dt>Type</dt><dd>Capital Expenditure Approvals</dd>
 <dt>Category</dt><dd>Water</dd><dt>Status</dt><dd>Closed</dd>
 <dt>Outcome</dt><dd>Directions given</dd>
 <dt>Date Received</dt><dd>04/07/2025</dd>
 <dt>Decision Date</dt><dd>10/23/2025</dd></dl></section>
 <nav><button onclick="showFiles('Exhibits')">Exhibits (1)</button>
 <button onclick="showFiles('Key Documents')">Key Documents (2)</button>
 <button onclick="showFiles('Other Documents')">Other Documents (12)</button>
 <button onclick="showFiles('Transcripts')">Transcripts (0)</button>
 <button onclick="showFiles('Recordings')">Recordings (0)</button></nav>
 <section id="files"></section>`;
 if (scenario === 'focusable') document.querySelectorAll('dd').forEach(el=>el.tabIndex=0);
}
function showFiles(category) {
 const count = {'Exhibits':1,'Key Documents':2,'Other Documents':12,
   'Transcripts':0,'Recordings':0}[category];
 const rows = Array.from({length:count}, (_,i)=>
 `<article><span>${200000+i}</span><strong>
 ${scenario==='same_titles' ? 'Correspondence' : 'Source filing '+(i+1)}</strong>
 <time>04/07/2025</time><span>${scenario === 'restricted' ? 'Confidential' : 'Public'}</span>
 <span>.pdf</span><button onclick="openFile(${i})">
 ${scenario==='direct' ? 'Download PDF' : 'Retrieve attachment'}</button>
 </article>`).join('');
 document.querySelector('#files').innerHTML = '<h2>'+category+'</h2>'+rows;
 if (scenario==='deep_rows') document.querySelectorAll('article').forEach(row=>{
   const inner=row.innerHTML; row.innerHTML='<div>'.repeat(7)+inner+'</div>'.repeat(7);
 });
}
function openFile(index) {
 if (scenario==='direct') { getFile(index); return; }
 if (scenario === 'popup') { window.open('http://localhost:'+location.port+'/trap'); return; }
 const dialog = document.createElement('dialog');
 dialog.innerHTML = `<p>Source attachments</p><button onclick="getFile(${index})">Save PDF</button>
 ${scenario==='multiple' ? '<button onclick="getFile('+index+')">Second file.pdf</button>' : ''}
 <button onclick="closeFile(this)">Done</button>`;
 document.body.append(dialog); dialog.showModal();
}
function closeFile(button) {
 const dialog=button.closest('dialog');
 setTimeout(()=>dialog.remove(),scenario==='slowclose' ? 400 : 0);
}
function getFile(index) {
 const link=document.createElement('a');
 link.href=scenario==='external' ? 'https://unapproved.invalid/file.pdf' : '/file?index='+index;
 link.download='filing.pdf'; link.click();
}
if (scenario === 'delayed') {
 const fragment=document.createDocumentFragment();
 while (document.body.firstChild) fragment.append(document.body.firstChild);
 setTimeout(()=>document.body.append(fragment),500);
}
</script></body></html>"""


@pytest.fixture(scope="session")
def redesigned_site() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            url = urlsplit(self.path)
            body = PAGE.encode()
            if url.path == "/file":
                body = PDF + parse_qs(url.query)["index"][0].encode()
                source = parse_qs(urlsplit(self.headers.get("Referer", "")).query).get("case", [])
                if source == ["corrupt"] and parse_qs(url.query)["index"] == ["0"]:
                    body = b"<html>Source session expired</html>"
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/pdf" if url.path == "/file" else "text/html"
            )
            if url.path == "/file":
                self.send_header("Content-Disposition", 'attachment; filename="filing.pdf"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/", requests
    server.shutdown()
    server.server_close()
    thread.join()


def scripted_model(request: httpx.Request) -> httpx.Response:

    nodes_input = json.loads(request.content)["input"][0]["content"][0]["text"]
    data = json.loads(nodes_input)
    nodes, state = data["nodes"], data["state"]

    def find(text, *, tag=None, within=None):
        matches = [
            n
            for n in nodes
            if n["text"] == text
            and (tag is None or n["tag"] == tag)
            and (within is None or within in n["ancestors"])
        ]
        matches.sort(key=lambda n: n["kind"] != "text")
        return matches[0]["id"]

    result = {"action": "stop", "target": None, "matter": None, "document": None}
    if not state["verified_matter"]:
        if any(n["text"] == "Capital Expenditure Approvals" for n in nodes):
            result["action"] = "read_matter"
            result["matter"] = {
                "matter_number": find(
                    "M12383" if any(n["text"] == "M12383" for n in nodes) else "M12205", tag="dd"
                ),
                "title": find(
                    "Halifax Regional Water Commission - Windsor Street Project", tag="dd"
                ),
                "matter_type": find("Capital Expenditure Approvals", tag="dd"),
                "category": find("Water", tag="dd"),
                "status": find("Closed", tag="dd"),
                "outcome": find("Directions given", tag="dd"),
                "dates": [
                    {
                        "label": find("Date Received", tag="dt"),
                        "value": find("04/07/2025", tag="dd"),
                    },
                    {
                        "label": find("Decision Date", tag="dt"),
                        "value": find("10/23/2025", tag="dd"),
                    },
                ],
                "categories": [
                    {
                        "category": name,
                        "count": find(f"{name} ({count})", tag="button"),
                        "control": find(f"{name} ({count})", tag="button"),
                    }
                    for name, count in [
                        ("Exhibits", 1),
                        ("Key Documents", 2),
                        ("Other Documents", 12),
                        ("Transcripts", 0),
                        ("Recordings", 0),
                    ]
                ],
            }
        else:
            entry = next(n for n in nodes if n["editable"])
            if entry.get("value") != "M12205":
                result.update(action="fill_matter", target=entry["id"])
            else:
                result.update(action="click", target=find("Find case", tag="button"))
    elif state["active_document"] and not any(n["dialog"] for n in nodes):
        result["action"] = "stop"
    elif state["active_document"]:
        result.update(
            action="download" if state["active_attachment_count"] == 0 else "close_document",
            target=find(
                "Save PDF" if state["active_attachment_count"] == 0 else "Done", tag="button"
            ),
        )
    elif len(state["examined"]) >= min(
        state["limit"],
        {
            "Exhibits": 1,
            "Key Documents": 2,
            "Other Documents": 12,
            "Transcripts": 0,
            "Recordings": 0,
        }[state["document_type"]],
    ):
        result["action"] = "finish"
    else:
        examined = {d["id"] for d in state["examined"]}
        rows = [
            n
            for n in nodes
            if n["tag"] == "article" and not any(identifier in n["text"] for identifier in examined)
        ]
        if not rows:
            result["action"] = "scroll"
        else:
            row = rows[0]["id"]
            fields = [n for n in nodes if row in n["ancestors"] and n["kind"] == "text"]
            identifier = next(n["id"] for n in fields if n["text"].isdigit())
            title = next(n["id"] for n in fields if n["tag"] == "strong")
            direct = any(n["text"] == "Download PDF" for n in nodes)
            result.update(
                action="download" if direct else "open_document",
                target=find(
                    "Download PDF" if direct else "Retrieve attachment", tag="button", within=row
                ),
                document={
                    "row": row,
                    "identifier": identifier,
                    "title": title,
                    "date": find("04/07/2025", tag="time", within=row),
                    "security": next(
                        n["id"] for n in fields if n["text"] in {"Public", "Confidential"}
                    ),
                    "extension": find(".pdf", within=row),
                },
            )
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result)}],
                }
            ],
        },
    )
