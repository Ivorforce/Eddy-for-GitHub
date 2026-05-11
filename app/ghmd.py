"""Minimal inline GitHub-flavoured markdown → safe HTML.

Deliberately tiny — only the shorthand that shows up constantly in issue/PR
prose and reads as noise without rendering:

  - `` `code` ``                        → <code>…</code>
  - github.com/owner/repo/pull/NN URLs  → a short ref the way github.com shows it
  - #NN                                 → a link to the *current* thread's repo
  - repo#NN / owner/repo#NN             → a cross-repo ref (bare `repo` resolves
                                          against the current thread's owner),
                                          collapsed to the shortest unambiguous form
  - @mention                            → a profile link wired into the
                                          tracked-entity machinery (entity-trigger)

Nothing else: no bold/italic, no [text](url) links, no fenced blocks, no
@org/team. The scanner walks the *raw* string and HTML-escapes everything
that isn't a recognised token, so tokenising never trips over escape entities.

Used by app/web.py for chat bubbles, AI-verdict descriptions, and (code only,
via render_title) row titles.
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

__all__ = ["render", "render_title", "parse_github_item_url"]

# GitHub usernames: 1–39 chars, alphanumerics and single internal hyphens,
# never leading/trailing a hyphen.
_USER = r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"

# owner / repo / kind / number ( / #anchor )?  — owners are [A-Za-z0-9-];
# repo names also allow '.' and '_' but not as the first char.
_URL_RE = re.compile(
    r"https?://github\.com/"
    r"([A-Za-z0-9-]+)/([A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(issues|pull|discussions)/(\d{1,12})(#[A-Za-z0-9_-]+)?"
)

_CODE_RE = re.compile(r"`([^`\n]+)`")

# repo#NN or owner/repo#NN — owners are [A-Za-z0-9-]; repo names also allow
# '.' and '_' but not as the first char. Used both inside _TOKEN_RE and to
# re-parse a matched token into its parts.
_XREF_RE = re.compile(
    r"(?:([A-Za-z0-9][A-Za-z0-9-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#(\d{1,12})"
)

# Combined scanner. Alternation order = precedence: a `code` span wins over a
# bare # inside it; a full URL wins over a repo#NN at its tail; `xref` (which
# requires a name before the #) wins over the bare #NN `ref`. `xref`/`ref`/
# `mention` only fire at a non-word, non-slash, non-@ boundary so they don't
# match inside emails (a@b), paths, css ids, or @owner/repo team handles.
_TOKEN_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<url>https?://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"/(?:issues|pull|discussions)/\d{1,12}(?:#[A-Za-z0-9_-]+)?)"
    r"|(?P<xref>(?<![\w/@])(?:[A-Za-z0-9][A-Za-z0-9-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*#\d{1,12}\b)"
    r"|(?P<ref>(?<![\w/@])#\d{1,12}\b)"
    r"|(?P<mention>(?<![\w/@])@" + _USER + r")"
)

# A URL anchor that points at a specific comment / review — github appends
# "(comment)" to the rendered ref in that case.
_COMMENT_ANCHOR_HINTS = ("comment", "pullrequestreview", "discussion_r")


def parse_github_item_url(url: str | None):
    """(owner, repo, kind, number, anchor|None) for a github issue/PR/discussion
    URL, or None if it isn't one. `match` semantics — the URL must start the
    string (true for stored html_url values)."""
    if not url:
        return None
    m = _URL_RE.match(url.strip())
    if not m:
        return None
    return (m.group(1), m.group(2), m.group(3), int(m.group(4)), m.group(5))


def _split_repo(repo: str | None) -> tuple[str, str]:
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name
    return "", repo or ""


def _esc(s: str) -> str:
    return str(escape(s))


def _shortref(owner: str, repo: str, num: int, cur_owner: str, cur_name: str) -> str:
    if cur_owner and cur_name and owner.lower() == cur_owner.lower() and repo.lower() == cur_name.lower():
        return f"#{num}"
    if cur_owner and owner.lower() == cur_owner.lower():
        return f"{repo}#{num}"
    return f"{owner}/{repo}#{num}"


def _render_url(tok: str, cur_owner: str, cur_name: str, interactive: bool) -> str:
    m = _URL_RE.match(tok)
    if not m:  # belt-and-suspenders; _TOKEN_RE's url group is a subset of _URL_RE
        return _esc(tok)
    owner, repo, _kind, num, anchor = m.group(1), m.group(2), m.group(3), int(m.group(4)), m.group(5)
    label = _shortref(owner, repo, num, cur_owner, cur_name)
    if anchor and any(h in anchor for h in _COMMENT_ANCHOR_HINTS):
        label += " (comment)"
    if not interactive:
        return f'<span class="gh-ref">{_esc(label)}</span>'
    return f'<a class="gh-ref" href="{_esc(tok)}" target="_blank" rel="noopener">{_esc(label)}</a>'


def _render_ref(num: int, cur_owner: str, cur_name: str, interactive: bool) -> str:
    if not (cur_owner and cur_name):
        return f"#{num}"  # no thread context to resolve against — leave as text
    if not interactive:
        return f'<span class="gh-ref">#{num}</span>'
    href = f"https://github.com/{cur_owner}/{cur_name}/issues/{num}"
    return f'<a class="gh-ref" href="{_esc(href)}" target="_blank" rel="noopener">#{num}</a>'


def _render_xref(tok: str, cur_owner: str, cur_name: str, interactive: bool) -> str:
    m = _XREF_RE.match(tok)
    if not m:  # belt-and-suspenders; _TOKEN_RE's xref group is a subset of _XREF_RE
        return _esc(tok)
    owner, repo, num = m.group(1), m.group(2), int(m.group(3))
    if not owner:
        if not cur_owner:
            return f"{repo}#{num}"  # bare repo with no thread owner — leave as text
        owner = cur_owner
    label = _shortref(owner, repo, num, cur_owner, cur_name)
    if not interactive:
        return f'<span class="gh-ref">{_esc(label)}</span>'
    href = f"https://github.com/{owner}/{repo}/issues/{num}"
    return f'<a class="gh-ref" href="{_esc(href)}" target="_blank" rel="noopener">{_esc(label)}</a>'


def _render_mention(login: str, interactive: bool,
                    tracked_people, people_notes: dict) -> str:
    tracked = login in tracked_people
    cls = "mention is-tracked" if tracked else "mention"
    if not interactive:
        return f'<span class="{cls}">@{_esc(login)}</span>'
    attrs = [
        f'class="entity-trigger {cls}"',
        f'href="https://github.com/{_esc(login)}"',
        'target="_blank"', 'rel="noopener"',
        'data-tracked-trigger', 'data-kind="person"', f'data-key="{_esc(login)}"',
    ]
    note = people_notes.get(login)
    if note:
        attrs.append(f'data-tip="{_esc(note)}"')
    return f'<a {" ".join(attrs)}>@{_esc(login)}</a>'


def render(text: str | None, *, cur_repo: str | None = None,
           interactive: bool = False,
           tracked_people=frozenset(), people_notes: dict | None = None) -> Markup:
    """Render the full inline subset. `interactive=False` (e.g. a hover tooltip
    that can't host clickable links) renders refs/mentions as styled <span>s
    instead of <a>s; `code` always renders. `cur_repo` ("owner/name") resolves
    bare #NN and bare repo#NN, and shortens same-repo URLs / cross-repo refs."""
    text = text or ""
    cur_owner, cur_name = _split_repo(cur_repo)
    people_notes = people_notes or {}
    out: list[str] = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(_esc(text[pos:m.start()]))
        group = m.lastgroup
        tok = m.group()
        if group == "code":
            out.append(f"<code>{_esc(tok[1:-1])}</code>")
        elif group == "url":
            out.append(_render_url(tok, cur_owner, cur_name, interactive))
        elif group == "xref":
            out.append(_render_xref(tok, cur_owner, cur_name, interactive))
        elif group == "ref":
            out.append(_render_ref(int(tok[1:]), cur_owner, cur_name, interactive))
        else:  # mention
            out.append(_render_mention(tok[1:], interactive, tracked_people, people_notes))
        pos = m.end()
    if pos < len(text):
        out.append(_esc(text[pos:]))
    return Markup("".join(out))


def render_title(text: str | None) -> Markup:
    """Just `` `code` `` spans — used for issue/PR titles, which github renders
    code in but nothing else."""
    text = text or ""
    out: list[str] = []
    pos = 0
    for m in _CODE_RE.finditer(text):
        if m.start() > pos:
            out.append(_esc(text[pos:m.start()]))
        out.append(f"<code>{_esc(m.group(1))}</code>")
        pos = m.end()
    if pos < len(text):
        out.append(_esc(text[pos:]))
    return Markup("".join(out))
