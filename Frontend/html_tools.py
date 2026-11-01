"""Build escaped HTML and SVG elements."""

import html
import re
import types

_TAG_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:.-]*$")
_URL_ATTRIBUTES = ("href", "action", "src")

VOID_ELEMENTS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
SVG_EMPTY_ELEMENTS = frozenset({"circle", "ellipse", "line", "path", "polygon", "polyline", "rect"})


class Markup(str):
    """Text that is already safe HTML (produced by this module only)."""

    __slots__ = ()


def escape(value) -> Markup:
    """Escape &, <, >, quotes so the value can only ever appear as text."""
    if isinstance(value, Markup):
        return value
    return Markup(html.escape(str(value), quote=True))


def _collect(child, parts: list[str]) -> None:
    if child is None or child is False:
        return
    if isinstance(child, Markup):
        parts.append(child)
    elif isinstance(child, (list, tuple, types.GeneratorType)):
        for item in child:
            _collect(item, parts)
    else:
        parts.append(html.escape(str(child), quote=True))


def render(*children) -> Markup:
    """Join children into markup."""
    parts: list[str] = []
    for child in children:
        _collect(child, parts)
    return Markup("".join(parts))


def _attribute_name(key: str) -> str:

    name = key.rstrip("_").replace("_", "-")
    if not _ATTRIBUTE_NAME.match(name) or name.lower().startswith("on"):
        raise ValueError(f"Attribute {name!r} is not allowed.")
    return name


def tag(element: str, /, *children, **attributes) -> Markup:
    """One element, e.g."""
    if not _TAG_NAME.match(element):
        raise ValueError(f"Tag {element!r} is not allowed.")
    pieces = [element]
    for key, value in attributes.items():
        if value is None or value is False:
            continue
        attribute = _attribute_name(key)
        if value is True:
            pieces.append(attribute)
            continue
        text = str(value)
        if attribute in _URL_ATTRIBUTES and not text.startswith(("/", "#")):
            raise ValueError(f"{attribute} must point inside RESQ.")
        pieces.append(f'{attribute}="{html.escape(text, quote=True)}"')
    opening = "<" + " ".join(pieces)
    if element in VOID_ELEMENTS:
        return Markup(opening + ">")
    if not children and element in SVG_EMPTY_ELEMENTS:
        return Markup(opening + "/>")
    return Markup(opening + ">" + render(*children) + "</" + element + ">")


def document(title: str, *body, stylesheet: str = "/static/style.css") -> Markup:
    """A complete HTML5 page with the shared stylesheet."""
    head = tag(
        "head",
        tag("meta", charset="utf-8"),
        tag("meta", name="viewport", content="width=device-width, initial-scale=1"),
        tag("title", title),
        tag("link", rel="stylesheet", href=stylesheet),
    )
    return render(Markup("<!DOCTYPE html>\n"), tag("html", head, tag("body", *body), lang="en"))
