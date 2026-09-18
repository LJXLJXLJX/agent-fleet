
import html.parser
import json
import os
import re
import sys
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import (
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)


class Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        values = dict(attrs)
        if "href" in values:
            self.values.append(values)


index = os.environ["PIP_PROBE_INDEX"].rstrip("/")
prefer_fallback = os.environ.get("PIP_PROBE_PREFER_FALLBACK") == "1"
platform = os.environ["PIP_PROBE_PLATFORM"]
maximum = re.search(r"manylinux_(\d+)_(\d+)_", platform)
maximum_glibc = tuple(map(int, maximum.groups())) if maximum else None
tag_order = {tag: position for position, tag in enumerate(sys_tags())}
opener = build_opener(ProxyHandler({}))
pages = {}


def compatible_tag(tag):
    match = re.match(r"manylinux_(\d+)_(\d+)_", tag.platform)
    if match and maximum_glibc and tuple(map(int, match.groups())) > maximum_glibc:
        return False
    return tag in tag_order


def emit(value):
    print("ACG\t" + json.dumps(value, sort_keys=True), flush=True)


def resolve(row):
    package = row["package"]
    requirement_text = row["requirement"]
    try:
        requirement = Requirement(requirement_text)
    except InvalidRequirement as exc:
        emit({"package": package, "requirement": requirement_text, "state": "unsupported", "reason": "invalid requirement: " + str(exc)})
        return
    if requirement.url:
        emit({"package": package, "requirement": requirement_text, "state": "unsupported", "reason": "direct URL requirements are outside the PyPI source"})
        return
    if requirement.marker is not None and not requirement.marker.evaluate():
        emit({"package": package, "requirement": requirement_text, "state": "not-applicable", "reason": "environment marker does not apply"})
        return
    normalized = canonicalize_name(requirement.name)
    page_url = index + "/" + normalized + "/"
    if normalized not in pages:
        headers = {"Accept": "text/html", "User-Agent": "artifact-cache-pip-probe/0.2"}
        if prefer_fallback:
            headers["X-Artifact-Cache-Prefer-Fallback"] = "1"
        request = Request(page_url, headers=headers)
        try:
            with opener.open(request, timeout=float(os.environ["PIP_PROBE_TIMEOUT"])) as response:
                pages[normalized] = response.read(16 * 1024 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:
            emit({"package": package, "requirement": requirement_text, "state": "unavailable", "reason": "simple index request failed: " + type(exc).__name__})
            return
    parser = Links()
    parser.feed(pages[normalized])
    candidates = []
    exact_pin = any(
        specifier.operator in {"==", "==="}
        and "*" not in specifier.version
        for specifier in requirement.specifier
    )
    for attributes in parser.values:
        # Match pip's PEP 592 behavior: yanked releases are ignored during
        # ordinary resolution, but an exact ==/=== pin remains installable.
        if "data-yanked" in attributes and not exact_pin:
            continue
        url = urljoin(page_url, attributes["href"])
        parsed = urlsplit(url)
        filename = unquote(parsed.path.rsplit("/", 1)[-1])
        is_wheel = filename.endswith(".whl")
        try:
            if is_wheel:
                name, version, _build, wheel_tags = parse_wheel_filename(filename)
                ranks = [tag_order[tag] for tag in wheel_tags if compatible_tag(tag)]
                if not ranks:
                    continue
                rank = min(ranks)
            else:
                name, version = parse_sdist_filename(filename)
                rank = len(tag_order) + 1
        except Exception:
            continue
        if canonicalize_name(name) != normalized:
            continue
        if not requirement.specifier.contains(version, prereleases=None):
            continue
        requires_python = attributes.get("data-requires-python")
        if requires_python:
            try:
                if not SpecifierSet(requires_python).contains(os.environ["PIP_PROBE_PYTHON"], prereleases=True):
                    continue
            except InvalidSpecifier:
                continue
        digest = None
        for fragment in parsed.fragment.split("&"):
            if fragment.startswith("sha256=") and re.fullmatch(r"[0-9a-fA-F]{64}", fragment[7:]):
                digest = fragment[7:].lower()
                break
        clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        candidates.append((version, 1 if is_wheel else 0, -rank, filename, clean_url, digest))
    if not candidates:
        emit({"package": package, "requirement": requirement_text, "state": "unavailable", "reason": "no compatible artifact in simple index"})
        return
    version, _wheel, _rank, filename, url, digest = max(candidates)
    emit({"package": package, "requirement": requirement_text, "state": "resolved", "reason": "compatible artifact selected", "version": str(version), "filename": filename, "url": url, "sha256": digest})


with open("/probe/requirements.jsonl", encoding="utf-8") as stream:
    for line in stream:
        if line.strip():
            resolve(json.loads(line))
