"""A source-scoped browser with evidence references and verified file transfers."""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Download, Locator, Page, Route

from senpilot.archive import safe_filename, validate_file
from senpilot.models import DownloadedFile, SourceDocument
from senpilot.uarb import SiteError

OBSERVE = r"""token => {
 const visible = el => {
   const r = el.getBoundingClientRect(), s = getComputedStyle(el);
   return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
     && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
 };
 const control = el => el.matches('button,a[href],input:not([type=password]),textarea,' +
   '[contenteditable=true],[role=button],[role=tab],[role=textbox],[tabindex],[onclick]');
 const selected = new Set();
 for (const el of document.body.querySelectorAll('*')) {
   if (!visible(el) || el.closest('script,style,input[type=password]')) continue;
   const text = [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent).join(' ').trim();
   if (control(el) || text) selected.add(el);
 }
 const leaves = [...selected].slice(0, 220);
 for (const el of leaves) {
   const row=el.closest('tr,[role=row],article,li,[role=listitem]');
   if (row && visible(row)) selected.add(row);
   let parent = el.parentElement;
   for (let depth=0; parent && parent !== document.body && depth<5; depth++) {
     if (visible(parent)) selected.add(parent);
     parent = parent.parentElement;
   }
 }
 const elements = [...selected];
 const ids = new Map(elements.map((el,i) => [el,i]));
 for (const [el,id] of ids) el.setAttribute('data-senpilot-ref', token + ':' + id);
 return elements.map((el,id) => {
   const kind = control(el) ? 'control' : leaves.includes(el) ? 'text' : 'group';
   const full = (kind === 'control' || kind === 'group' ? el.innerText :
     [...el.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join(' '))
     .replace(/\s+/g,' ').trim();
   const text = full || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
   const ancestors = [];
   for (let p=el.parentElement; p; p=p.parentElement) if (ids.has(p)) ancestors.push(ids.get(p));
   const r=el.getBoundingClientRect();
   const hit=document.elementFromPoint(Math.min(innerWidth-1,Math.max(0,r.x+r.width/2)),
     Math.min(innerHeight-1,Math.max(0,r.y+r.height/2)));
   return {id,kind,text:text.slice(0,1000),truncated:text.length>1000,
     path:(() => {const parts=[]; for(let p=el;p && p!==document.body;p=p.parentElement)
       parts.unshift(p.tagName+':'+[...p.parentElement.children].indexOf(p));
       return parts.join('/');})(),
     bbox:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],
     label:el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
       (el.parentElement?.innerText || '').replace(/\s+/g,' ').trim().slice(0,160),
     clickable:!!hit && (hit===el || el.contains(hit)),
     scrollable:el.scrollHeight>el.clientHeight+10 &&
       ['auto','scroll'].includes(getComputedStyle(el).overflowY),
     tag:el.tagName.toLowerCase(),role:el.getAttribute('role') || '',ancestors,
     peers:el.parentElement ? [...el.parentElement.children].filter(peer => ids.has(peer)
       && peer.tagName===el.tagName && [...peer.children].map(c=>c.tagName).join(',')
         ===[...el.children].map(c=>c.tagName).join(',')) .map(peer=>ids.get(peer)) : [],
     value:el.matches('input:not([type=password]),textarea') ? el.value :
       el.matches('[contenteditable=true]') ? el.innerText : '',
     editable:el.matches('input:not([type=password]):not([readonly]),textarea:not([readonly]),[contenteditable=true]'),
     disabled:el.matches(':disabled,[aria-disabled=true]'),
     dialog:!!el.closest('dialog[open],[role=dialog]')};
 });
}"""


class BrowserNode(TypedDict):
    id: int
    kind: Literal["control", "text", "group"]
    text: str
    truncated: bool
    path: str
    bbox: list[int]
    label: str
    clickable: bool
    scrollable: bool
    tag: str
    role: str
    ancestors: list[int]
    peers: list[int]
    value: str
    editable: bool
    disabled: bool
    dialog: bool


@dataclass(frozen=True)
class Observation:
    token: str
    nodes: list[BrowserNode]

    def node(self, ref: int) -> BrowserNode:
        if type(ref) is not int or not 0 <= ref < len(self.nodes):
            raise SiteError("AI selected an unknown evidence reference")
        node = self.nodes[ref]
        return node

    def text(self, ref: int) -> str:
        node = self.node(ref)
        if node["truncated"]:
            raise SiteError("source evidence exceeds the supported observation size")
        return node["text"]

    def contains(self, parent: int, child: int) -> bool:
        return parent == child or parent in self.node(child)["ancestors"]

    def values(self, parent: int | None = None) -> tuple[str, ...]:
        return tuple(
            str(node["text"])
            for node in self.nodes
            if not node["editable"]
            and not node["truncated"]
            and (
                node["kind"] == "text"
                or (
                    node["kind"] == "control"
                    and node["tag"] in {"div", "span", "td", "dd"}
                    and node["role"] not in {"button", "tab"}
                )
            )
            and (parent is None or self.contains(parent, int(node["id"])))
        )

    def _seen_refs(self, refs: list[int], examined: list[SourceDocument]) -> set[int]:
        texts = {ref: set(self.values(ref)) for ref in refs}
        seen = set()
        for document in examined:
            if document.identifier_from_source:
                matches = [ref for ref, values in texts.items() if document.identifier in values]
            else:
                evidence = set(document.source_fields) - {""}
                matches = [ref for ref, values in texts.items() if evidence and evidence <= values]
            if len(matches) > 1:
                raise SiteError("completed source entry identity is ambiguous")
            seen.update(matches)
        return seen

    def pending_attachments(self, attempted: set[str]) -> list[int]:
        return [
            n["id"]
            for n in self.nodes
            if n["dialog"]
            and n["kind"] == "control"
            and (n["tag"] in {"button", "a"} or n["role"] == "button")
            and n["path"] not in attempted
            and re.search(
                r"\.[A-Za-z0-9]{2,8}$|\b(save|download|get)\b.*\b(file|document|attachment|pdf|audio)\b",
                n["text"],
                re.I,
            )
        ]

    def pending_entries(self, examined: list[SourceDocument]) -> list[int]:
        candidates = []
        for node in self.nodes:
            if node["kind"] == "text" or node["dialog"]:
                continue
            ref = int(node["id"])
            texts = set(self.values(ref))
            has_control = any(
                n["kind"] == "control"
                and int(n["id"]) != ref
                and (n["tag"] in {"button", "a"} or n["role"] == "button")
                and self.contains(ref, int(n["id"]))
                for n in self.nodes
            )
            shaped = node["tag"] in {"tr", "article", "li"} or node["role"] in {"row", "listitem"}
            shaped = shaped or (isinstance(node["peers"], list) and len(node["peers"]) > 1)
            has_fields = any(re.search(r"\d{2}/\d{2}/\d{4}|\.[A-Za-z0-9]+$", t) for t in texts)
            if not shaped or not has_control or not has_fields:
                continue
            candidates.append(ref)
        leaves = [
            ref
            for ref in candidates
            if not any(other != ref and self.contains(ref, other) for other in candidates)
        ]
        leaves.sort(key=lambda ref: (self.node(ref)["bbox"][1], self.node(ref)["bbox"][0]))
        seen = self._seen_refs(leaves, examined)
        return [ref for ref in leaves if ref not in seen]

    def require_first_entry(self, ref: int, examined: list[SourceDocument]) -> None:
        pending = self.pending_entries(examined)
        if pending and pending[0] != ref:
            raise SiteError("recovery must retrieve source entries in displayed order")
        peers = self.node(ref)["peers"]
        if not isinstance(peers, list) or ref not in peers:
            raise SiteError("document entry has no verifiable source ordering")
        seen = self._seen_refs(peers, examined)
        for peer in sorted(
            peers, key=lambda ref: (self.node(ref)["bbox"][1], self.node(ref)["bbox"][0])
        ):
            if peer in seen:
                continue
            if peer != ref:
                raise SiteError("recovery must retrieve source entries in displayed order")
            return
        raise SiteError("recovery selected an already examined source entry")


class RecoveryBrowser:
    def __init__(self, page: Page, base_url: str, transfer_timeout_ms: int) -> None:
        self.page = page
        self.origin = self._origin(base_url)
        self.transfer_timeout_ms = transfer_timeout_ms
        self.blocked = False
        self._initialized = False
        self.export_filename: str | None = None

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url.removeprefix("blob:"))
        return parsed.scheme, parsed.hostname or "", parsed.port

    def permitted(self, url: str) -> bool:
        return self._origin(url) == self.origin and not urlsplit(url).username

    async def guard(self, route: Route) -> None:
        if not self.permitted(route.request.url):
            self.blocked = True
            await route.abort()
        else:
            await route.continue_()

    async def observe(self) -> Observation:
        if self.blocked or not self.permitted(self.page.url):
            raise SiteError("recovery stopped an unapproved source request")
        await self.page.wait_for_function(
            """() => document.body && (document.body.innerText.trim() ||
              [...document.querySelectorAll('input,button,[contenteditable=true],[role=button]')]
                .some(el=>el.getBoundingClientRect().width>0))""",
            timeout=15000,
        )
        if not self._initialized:
            await self.page.wait_for_timeout(300)
            previous = None
            stable = 0
            for _ in range(8):
                signature = await self.page.evaluate(
                    "() => document.body.innerText+'|'+document.body.querySelectorAll('*').length"
                )
                stable = stable + 1 if signature == previous else 0
                if stable >= 3:
                    break
                previous = signature
                await self.page.wait_for_timeout(150)
            self._initialized = True
        token = uuid4().hex
        # The constant observation script is the only JavaScript executed by the recovery driver.
        nodes = cast(list[BrowserNode], await self.page.evaluate(OBSERVE, token))
        if not nodes:
            raise SiteError("source has no usable visible evidence")
        return Observation(token, nodes)

    def locator(self, observation: Observation, ref: int) -> Locator:
        observation.node(ref)
        return self.page.locator(f'[data-senpilot-ref="{observation.token}:{ref}"]')

    async def click(self, observation: Observation, ref: int) -> None:
        node = observation.node(ref)
        if node["kind"] != "control" or node["disabled"]:
            raise SiteError("AI selected an unavailable source control")
        if re.search(
            r"\b(delete|remove|upload|sign in|log in|purchase|pay|submit payment)\b",
            str(node["text"]),
            re.I,
        ):
            raise SiteError("recovery selected an action outside public document retrieval")
        if node["dialog"] and str(node["text"]).casefold() in {"ok", "export", "save"}:
            dialog = self.locator(observation, ref).locator(
                "xpath=ancestor::*[@role='dialog' or self::dialog][1]"
            )
            fields = dialog.get_by_role("textbox")
            if await fields.count() == 1:
                value = await fields.input_value()
                if "," in value and re.fullmatch(r"[^/\\\r\n]{1,220}\.[A-Za-z0-9]+", value):
                    self.export_filename = value
                    await fields.fill(value.replace(",", "_"))
                    await fields.press("Tab")
        await self.locator(observation, ref).click()
        await self.page.wait_for_timeout(100)

    async def fill_matter(self, observation: Observation, ref: int, matter: str) -> None:
        locator = self.locator(observation, ref)
        if not observation.node(ref)["editable"]:
            await self.click(observation, ref)
            await self.page.wait_for_function(
                """() => {
                  const fields=[...document.querySelectorAll(
                    'input:not([type=password]):not([readonly]),textarea:not([readonly]),[contenteditable=true]')]
                    .filter(el=>el.getBoundingClientRect().width>0
                      && el.getBoundingClientRect().height>0);
                  return fields.includes(document.activeElement) || fields.length===1;
                }""",
                timeout=5000,
            )
            focused = self.page.locator(
                "input:not([type=password]):focus,textarea:focus,[contenteditable=true]:focus"
            )
            if await focused.count() == 1 and await focused.is_editable():
                locator = focused
            else:
                editors = self.page.locator(
                    "input:not([type=password]):visible,textarea:visible,[contenteditable=true]:visible"
                )
                if await editors.count() != 1 or not await editors.is_editable():
                    raise SiteError("source matter field did not become editable")
                locator = editors
        if await locator.get_attribute("type") == "password":
            raise SiteError("recovery cannot enter credentials")
        await locator.fill("")
        await locator.press_sequentially(matter, delay=20)
        await locator.press("Tab")

    async def scroll(self, observation: Observation, ref: int | None) -> None:
        if ref is not None:
            await self.locator(observation, ref).hover()
        else:
            await self.page.mouse.move(1100, 600)
        await self.page.mouse.wheel(0, 450)
        await self.page.wait_for_timeout(100)

    async def transfer(
        self,
        observation: Observation,
        ref: int,
        document: SourceDocument,
        destination: Path,
        attachment_number: int,
    ) -> DownloadedFile:
        download: Download | None = None
        path: Path | None = None
        try:
            async with self.page.expect_download(timeout=self.transfer_timeout_ms) as event:
                await self.click(observation, ref)
            download = await event.value
            if self.blocked or not self.permitted(download.url):
                raise SiteError("download left the approved source origin")
            original = download.suggested_filename
            candidate = destination / safe_filename(
                f"{document.identifier}-{attachment_number}", original
            )
            if candidate.exists():
                raise SiteError("recovery download conflicts with an existing source file")
            path = candidate
            await asyncio.wait_for(download.save_as(path), self.transfer_timeout_ms / 1000)
            size, digest = validate_file(path, document.extension or path.suffix)
            source_name = self.export_filename or original
            self.export_filename = None
            return DownloadedFile(document, source_name, path.name, size, digest)
        except BaseException:
            if download is not None:
                await download.cancel()
            if path is not None:
                path.unlink(missing_ok=True)
            raise
