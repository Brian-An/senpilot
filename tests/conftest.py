import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

PDF = b"%PDF-1.7\npublic regulatory evidence\n%%EOF\n"

FIXTURE_PAGE = r"""<!doctype html>
<html><head><style>
body { margin: 0; font-family: sans-serif; }
.iwps_body, .iwps_header { position: relative; height: 300px; }
.iwps_text_box, .fm-textarea { position: absolute; }
.v-grid-scroller { height: 230px; overflow-y: auto; width: 100%; }
table { position: relative; width: 100%; border-collapse: collapse; }
tr[role=row] { position: absolute; width: 100%; height: 68px; }
td { height: 68px; }
tr .fm-textarea { position: static; display: inline-block; margin-right: 12px; }
[role=dialog] { position: fixed; inset: 80px 20%; background: white; z-index: 10; }
.placeholder { cursor: text; }
.search-box { top: 50px; left: 20px; }
.search-box .text { position: absolute; inset: 0; z-index: 2; min-width: 140px; height: 24px; }
</style></head><body>
<div id="main"><div class="fm-textarea search-box">
<div class="text" tabindex="0"></div><div class="placeholder">eg M01234</div>
</div><button id="search" style="position:absolute;top:90px">Search</button></div>
<script>
const scenario = new URLSearchParams(location.search).get('case') || 'normal';
const counts = {'Exhibits':3, 'Key Documents':2, 'Other Documents':12,
                'Transcripts':0, 'Recordings':1};
if (scenario === 'export') counts['Transcripts'] = 2;
let committed = '';
const editor = document.querySelector('.text');
document.querySelector('.search-box').onclick = function () {
  document.querySelector('.placeholder').style.display = 'none';
  editor.contentEditable = 'true'; editor.focus();
};
editor.onblur = () => { committed = editor.textContent.trim(); };
document.querySelector('#search').onclick = () => {
  setTimeout(() => {
    if (committed !== 'M12205') {
      const dialog = document.createElement('div'); dialog.setAttribute('role','dialog');
      dialog.textContent = 'No records matched your search request.';
      document.body.append(dialog); return;
    }
    showMatter();
  }, 30);
};
function field(value, left, top) {
  return `<div class="fm-textarea" style="left:${left}px;top:${top}px">
    <div class="text">${value}</div></div>`;
}
function box(value,left,top,width=150) {
  return `<div class="iwps_text_box" style="left:${left}px;top:${top}px;width:${width}px">
    ${value}</div>`;
}
function showMatter(category) {
  const matter = scenario === 'wrong' ? 'M12383' : 'M12205';
  document.querySelector('#main').innerHTML = `
  <div class="${category ? 'iwps_header' : 'iwps_body'}">
    <div style="position:absolute;top:250px">${Object.entries(counts).map(([name,n]) =>
      `<button onclick="showMatter('${name}')">${name} - ${n}</button>`).join('')}</div>
    ${field(matter,20,140)}${field('Capital Expenditure Approvals',520,180)}
    ${field('Closed',20,180)}${field('04/07/2025',700,140)}
    ${field('10/23/2025',820,140)}${field('Directions given',950,140)}
    ${field('Water',520,140)}
    ${box('Halifax Regional Water Commission - Windsor Street Project',150,140,360)}
    ${box('Matter No',20,100)}${box('Title - Description',150,100)}
    ${box('Date Received',700,100)}${box('Decision Date',820,100)}
  </div><div id="list"></div>`;
  if (category && scenario !== 'stalled') {
    document.querySelector('#list').innerHTML = '<div class="v-grid v-grid-scroller">' +
      `<table role="grid" style="height:${counts[category]*68}px"><tbody></tbody></table></div>`;
    const scroller = document.querySelector('.v-grid-scroller');
    scroller.onscroll = () => renderRows(category, Math.floor(scroller.scrollTop/68));
    renderRows(category,0);
  }
}
function renderRows(category,start) {
  const rows = [];
  for (let i=start; i<Math.min(counts[category],start+4); i++) {
    const security = category === 'Exhibits' && i === 1 ? 'Confidential' : 'Public';
    let values;
    if (category === 'Exhibits') {
      values = [`Application ${i+1}`, '04/07/2025', security, `H-${i+1}`, '.pdf'];
    } else if (category === 'Key Documents') {
      values = [`Key document ${i+1}`, '04/07/2025', security, `${100000+i}`, '.pdf'];
    } else if (category === 'Recordings') {
      values = ['04/07/2025', 'Hearing recording', '.mp3'];
    } else if (category === 'Transcripts') {
      values = ['09/12/2022', 'September 12, 2022', 'Pdf', '', '', '', 'Public'];
    } else {
      const identifier = scenario === 'idcollision' ? '100000' : `${100000+i}`;
      values = [identifier,`Board document ${i+1}`,'04/07/2025',security,'.pdf'];
    }
    rows.push(`<tr role="row" style="transform:translateY(${i*68}px)"><td>` +
      values.map(value=>field(value,0,0)).join('') +
      `<button onclick="downloadDialog(${i},'${category}')">GO GET IT</button></td></tr>`);
  }
  document.querySelector('tbody').innerHTML = rows.join('');
}
function downloadDialog(index,category) {
  if (scenario === 'export') {
    const dialog = document.createElement('div'); dialog.setAttribute('role','dialog');
    dialog.setAttribute('aria-label','Export Field to File');
    dialog.className = 'v-window';
    const filename = category === 'Recordings' ? 'hearing.mp3' : 'transcript.pdf';
    dialog.innerHTML = 'Specify a filename to export. Save As:' +
      `<input type="text" value="${filename}">` +
      `<button onclick="this.parentElement.remove();showDownloadDialog(${index},'${category}')">
        OK</button><button onclick="this.parentElement.remove()">Cancel</button>`;
    document.body.append(dialog); return;
  }
  showDownloadDialog(index,category);
}
function showDownloadDialog(index,category) {
  const dialog = document.createElement('div'); dialog.className = 'v-window';
  dialog.setAttribute('role','dialog'); dialog.setAttribute('aria-label','Download Files');
  let filenames = [['duplicate','idcollision'].includes(scenario)
    ? 'same.pdf' : `${100000+index}.pdf`];
  if (category === 'Recordings') filenames = ['hearing.mp3'];
  if (category === 'Transcripts') filenames = ['transcript.pdf'];
  if (scenario === 'multiple') filenames = [`${index}-a.pdf`, `${index}-b.pdf`];
  dialog.innerHTML = 'Your files are ready for download. '
    + 'Please click the button to download each file:'
    + filenames.map((name,j)=>`<button class="fm-download-button"
      onclick="fetchFile(${index},${j},'${name}')">${name}</button>`).join('')
    + '<button onclick="closeDialog(this.parentElement)">Close</button>';
  document.body.append(dialog);
}
function closeDialog(dialog) {
  if (scenario !== 'closeblocked') dialog.remove();
}
function fetchFile(index,attachment,name) {
  if (scenario === 'missing' && index === 1) return;
  const link = document.createElement('a');
  link.href = `/file?index=${index}&attachment=${attachment}&case=${scenario}&name=${name}`;
  link.download = name; link.click();
}
</script></body></html>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/file":
            query = parse_qs(url.query)
            scenario = query.get("case", ["normal"])[0]
            index = int(query.get("index", ["0"])[0])
            name = query["name"][0]
            content = PDF + str(index).encode()
            if scenario == "partial" and index == 1:
                content = b"<!doctype html><html>Session expired</html>"
            if name.endswith(".mp3"):
                content = b"ID3\x04\x00\x00hearing recording"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            content = FIXTURE_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="session")
def fixture_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()
    worker.join()
