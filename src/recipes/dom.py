"""A small DOM and CSS-selector engine, on the standard library only.

Why hand-rolled instead of BeautifulSoup/lxml:

* the extraction hot path is the one place a crawler cannot afford a
  dependency surprise — this runs identically on a laptop with no wheels and
  on a worker with lxml installed;
* recipes ship as *data*, so the selector language has to be small, documented
  and stable. A subset we control beats a full engine whose behaviour changes
  under a new release;
* it keeps ``pip install -r requirements.txt`` from being the difference
  between "extraction works" and "extraction silently degraded".

Supported selectors: ``tag``, ``.class``, ``#id``, ``[attr]``, ``[attr=value]``,
``[attr*=value]``, ``a[href]``, ``a@href`` (read an attribute), descendant
`` `` and child ``>`` combinators, and comma-separated alternatives.

Not supported (on purpose): pseudo-classes beyond ``:first``/``:last``, sibling
combinators, and CSS3 namespaces. Recipes that need them should use several
extractors plus ``fallback_labels`` — which is what the healing path is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Iterator

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"""
    (?P<tag>[a-zA-Z][\w:-]*)
  | \.(?P<cls>[\w-]+)
  | \#(?P<id>[\w-]+)
  | \[(?P<attr>[\w:-]+)\s*(?:(?P<op>[~^$*|]?=)\s*["']?(?P<aval>[^"'\]]*)["']?)?\]
""", re.VERBOSE)


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element"] = field(default_factory=list)
    parent: "Element | None" = None
    text_parts: list[str] = field(default_factory=list)

    # -- queries ------------------------------------------------------------ #
    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    @property
    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.text)
        return _WS.sub(" ", " ".join(p for p in parts if p)).strip()

    def iter(self) -> Iterator["Element"]:
        for child in self.children:
            yield child
            yield from child.iter()

    def ancestors(self) -> Iterator["Element"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def to_dict(self, *, depth: int = 3) -> dict:
        row = {"tag": self.tag, "attrs": self.attrs, "text": self.text[:200]}
        if depth > 0 and self.children:
            row["children"] = [c.to_dict(depth=depth - 1) for c in self.children[:20]]
        return row


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element(tag="#document")
        self.stack: list[Element] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.stack.append(Element(tag="__skip__"))
            return
        node = Element(tag=tag, attrs={k: (v or "") for k, v in attrs},
                       parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            return
        node = Element(tag=tag, attrs={k: (v or "") for k, v in attrs},
                       parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag in (tag, "__skip__"):
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        # Data inside <script>/<style>/<template> belongs to no node we expose:
        # leaking it would put JavaScript into extracted field values.
        if self.stack and self.stack[-1].tag == "__skip__":
            return
        self.stack[-1].text_parts.append(data)


def parse(html: str) -> Element:
    """Parse HTML into a tree. Malformed markup is tolerated, never fatal."""
    tree = _Tree()
    try:
        tree.feed(html or "")
        tree.close()
    except Exception:                      # noqa: BLE001 - parser must never raise
        pass
    return tree.root


# --------------------------------------------------------------------------- #
# selectors
# --------------------------------------------------------------------------- #
@dataclass
class _Simple:
    tag: str = ""
    classes: tuple[str, ...] = ()
    elem_id: str = ""
    attrs: tuple[tuple[str, str, str], ...] = ()   # (name, op, value)
    first: bool = False
    last: bool = False

    def matches(self, node: Element) -> bool:
        if node.tag in ("#document", "__skip__"):
            return False
        if self.tag and node.tag != self.tag:
            return False
        if self.elem_id and node.attrs.get("id") != self.elem_id:
            return False
        if self.classes and not set(self.classes).issubset(node.classes):
            return False
        for name, op, value in self.attrs:
            actual = node.attrs.get(name)
            if actual is None:
                return False
            if not value:
                continue
            if op in ("", "=") and actual != value:
                return False
            if op == "*=" and value.lower() not in actual.lower():
                return False
            if op == "^=" and not actual.lower().startswith(value.lower()):
                return False
            if op == "$=" and not actual.lower().endswith(value.lower()):
                return False
            if op == "~=" and value not in actual.split():
                return False
        return True


def _parse_simple(part: str) -> _Simple:
    simple = _Simple()
    if ":" in part:
        base, _, pseudo = part.partition(":")
        part = base
        if pseudo == "first":
            simple.first = True
        elif pseudo == "last":
            simple.last = True
    position = 0
    for match in _TOKEN.finditer(part):
        if match.start() != position and part[position:match.start()].strip():
            break
        position = match.end()
        if match.group("tag"):
            simple.tag = match.group("tag").lower()
        elif match.group("cls"):
            simple.classes = (*simple.classes, match.group("cls"))
        elif match.group("id"):
            simple.elem_id = match.group("id")
        elif match.group("attr"):
            simple.attrs = (*simple.attrs,
                            (match.group("attr").lower(), match.group("op") or "",
                             match.group("aval") or ""))
    return simple


def _parse_selector(selector: str) -> list[tuple[str, _Simple]]:
    """Return ``[(combinator, simple), ...]`` for one comma-alternative."""
    out: list[tuple[str, _Simple]] = []
    combinator = " "
    position = 0
    text = selector.strip()
    while position < len(text):
        char = text[position]
        if char == ">":
            combinator = ">"
            position += 1
            continue
        if char.isspace():
            position += 1
            continue
        start = position
        depth = 0
        while position < len(text):
            char = text[position]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            elif depth == 0 and (char.isspace() or char == ">"):
                break
            position += 1
        out.append((combinator, _parse_simple(text[start:position])))
        combinator = " "
    return out


def _strip_attribute(selector: str) -> tuple[str, str]:
    """``a@href`` / ``a::attr(href)`` → ``("a", "href")``."""
    match = re.search(r"::attr\(([\w:-]+)\)", selector)
    if match:
        return selector[:match.start()] + selector[match.end():], match.group(1)
    match = re.search(r"@([\w:-]+)(?::(first|last))?$", selector.strip())
    if match:
        return selector[:match.start()], match.group(1)
    return selector, ""


def select_all(root: Element, selector: str, *, limit: int = 200) -> list[Element]:
    """All elements matching ``selector``, in document order."""
    base_selector, attribute = _strip_attribute(selector)
    if attribute:
        base_selector = base_selector.rstrip("@")
    results: list[Element] = []
    for alternative in _split_alternatives(base_selector):
        chain = _parse_selector(alternative)
        if not chain:
            continue
        candidates = list(root.iter()) if root.tag == "#document" else [
            root, *root.iter()]
        current = candidates
        for combinator, simple in chain:
            matched: list[Element] = []
            for node in current:
                pool = node.children if combinator == ">" else (
                    list(node.iter()) if combinator == " " else [node])
                if combinator == ">":
                    pool = list(node.children)
                for candidate in pool:
                    if simple.matches(candidate) and candidate not in matched:
                        matched.append(candidate)
            if simple.first and matched:
                matched = matched[:1]
            if simple.last and matched:
                matched = matched[-1:]
            current = matched
        for node in current:
            if node not in results:
                results.append(node)
    return results[:limit]


def _split_alternatives(selector: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in selector:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    parts.append(current.strip())
    return [p for p in parts if p]


def select_text(root: Element, selector: str, *, limit: int = 50) -> list[str]:
    """Extract values: element text, or the attribute named after ``@``/``::attr``."""
    base, attribute = _strip_attribute(selector)
    nodes = select_all(root, base or selector, limit=limit)
    out: list[str] = []
    for node in nodes:
        value = node.attrs.get(attribute, "") if attribute else node.text
        value = _WS.sub(" ", value or "").strip()
        if value:
            out.append(value)
    return out


def first_text(root: Element, selectors: Iterable[str], *, limit: int = 20) -> str:
    for selector in selectors:
        values = select_text(root, selector, limit=limit)
        if values:
            return values[0]
    return ""


def find_by_text(root: Element, needle: str, *, tags: Iterable[str] = ("div", "span",
                                                                       "td", "li", "p"),
                limit: int = 20) -> list[Element]:
    """Every element whose own text starts with ``needle`` (label lookup)."""
    wanted = needle.strip().casefold()
    out: list[Element] = []
    for node in root.iter():
        if tags and node.tag not in set(tags):
            continue
        if node.text.casefold().startswith(wanted) and len(node.text) <= 160:
            out.append(node)
            if len(out) >= limit:
                break
    return out
