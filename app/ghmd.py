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

__all__ = ["render", "render_title", "parse_github_item_url", "author_badge_svg"]


# Octicon path data for the four author-badge variants. Shared between Jinja
# (via the `author_badge_svg` global) and `_render_mention` so the badge
# rendering lives in exactly one place.
_BADGE_PATH_PERSON = (
    "M10.561 8.073a6.005 6.005 0 0 1 3.432 5.142.75.75 0 1 1-1.498.07 4.5 4.5 0 "
    "0 0-8.99 0 .75.75 0 0 1-1.498-.07 6.004 6.004 0 0 1 3.431-5.142 3.999 "
    "3.999 0 1 1 5.123 0ZM10.5 5a2.5 2.5 0 1 0-5 0 2.5 2.5 0 0 0 5 0Z"
)
_BADGE_PATH_SHIELD = (
    "m8.533.133 5.25 1.68A1.75 1.75 0 0 1 15 3.48V7c0 1.566-.32 3.182-1.303 "
    "4.682-.983 1.498-2.585 2.813-5.032 3.855a1.697 1.697 0 0 1-1.33 0c-2.447"
    "-1.042-4.049-2.357-5.032-3.855C1.32 10.182 1 8.566 1 7V3.48a1.75 1.75 0 "
    "0 1 1.217-1.667l5.25-1.68a1.748 1.748 0 0 1 1.066 0Zm-.61 1.429.001.001"
    "-5.25 1.68a.251.251 0 0 0-.174.237V7c0 1.36.275 2.666 1.057 3.859.784 "
    "1.194 2.121 2.342 4.366 3.298a.196.196 0 0 0 .154 0c2.245-.957 3.582"
    "-2.103 4.366-3.297C13.225 9.666 13.5 8.358 13.5 7V3.48a.25.25 0 0 0"
    "-.174-.238l-5.25-1.68a.25.25 0 0 0-.153 0ZM11.28 6.28l-3.5 3.5a.75.75 0 "
    "0 1-1.06 0l-1.5-1.5a.749.749 0 0 1 .326-1.275.749.749 0 0 1 .734.215l.97"
    ".97 2.97-2.97a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 1.042Z"
)
_BADGE_PATH_STAR = (
    "M8.5.75a.75.75 0 0 0-1.5 0v5.19L4.391 3.33a.75.75 0 1 0-1.06 1.061L5.939 "
    "7H.75a.75.75 0 0 0 0 1.5h5.19l-2.61 2.609a.75.75 0 1 0 1.061 1.06L7 9.561"
    "v5.189a.75.75 0 0 0 1.5 0V9.56l2.609 2.61a.75.75 0 1 0 1.06-1.061L9.561 "
    "8.5h5.189a.75.75 0 0 0 0-1.5H9.56l2.61-2.609a.75.75 0 0 0-1.061-1.06L8.5 "
    "5.939V.75Z"
)


def author_badge_svg(
    badge_class: str | None,
    assoc: str | None = None,
    *,
    self_title: str = "you",
) -> Markup:
    """Inline `<svg>` for the four author-badge variants. Single source of
    truth for both Jinja templates (via Jinja global) and ghmd._render_mention.

    `badge_class` mirrors web._author_badge_class: 'self' / 'member' /
    'first-time' / '' (anything else → muted generic icon). The generic
    icon's tooltip is the lowercase `assoc` when it's a meaningful value
    (anything other than empty / 'NONE'); silent otherwise."""
    cls = (badge_class or "").strip()
    if cls == "self":
        return Markup(
            '<svg class="author-icon icon-self" viewBox="0 0 16 16" '
            'fill="currentColor"><title>{title}</title>'
            '<path d="{d}"/></svg>'
        ).format(title=self_title, d=_BADGE_PATH_PERSON)
    if cls == "member":
        title = (assoc or "member").lower()
        return Markup(
            '<svg class="author-icon icon-member" viewBox="0 0 16 16" '
            'fill="currentColor"><title>{title}</title>'
            '<path d="{d}"/></svg>'
        ).format(title=title, d=_BADGE_PATH_SHIELD)
    if cls == "first-time":
        return Markup(
            '<svg class="author-icon icon-firsttime" viewBox="0 0 16 16" '
            'fill="currentColor"><title>first-time contributor</title>'
            '<path d="{d}"/></svg>'
        ).format(d=_BADGE_PATH_STAR)
    # Generic person glyph; only carry a tooltip when the association says
    # something useful (NONE / empty stay silent so the icon doesn't
    # advertise a meaningless label).
    tip = (assoc or "").lower() if assoc and assoc.upper() != "NONE" else ""
    if tip:
        return Markup(
            '<svg class="author-icon icon-generic" viewBox="0 0 16 16" '
            'fill="currentColor"><title>{title}</title>'
            '<path d="{d}"/></svg>'
        ).format(title=tip, d=_BADGE_PATH_PERSON)
    return Markup(
        '<svg class="author-icon icon-generic" viewBox="0 0 16 16" '
        'fill="currentColor" aria-hidden="true">'
        '<path d="{d}"/></svg>'
    ).format(d=_BADGE_PATH_PERSON)

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
                    tracked_people, people_notes: dict,
                    author_assocs: dict | None = None) -> str:
    tracked = login in tracked_people
    cls = "mention is-tracked" if tracked else "mention"
    # Badge from the per-thread roster (web._build_author_roster). When the
    # mentioned login also commented/reviewed on this thread (or is the
    # current user), the roster carries their badge_class + assoc; otherwise
    # author_badge_svg falls through to the muted generic icon. Wrap
    # badge + link in an inline-flex .author-chip so they stay glued
    # together when a markdown bubble (pre-wrap) wraps mid-sentence.
    info = (author_assocs or {}).get(login) or {}
    badge = author_badge_svg(info.get("badge_class") or "", info.get("assoc") or "")
    # The login text sits in its own .mention-handle so the underline
    # (the GitHub-style "this is a mention" cue) is drawn on the name
    # only — the leading `@` reads as a sigil, not as link text.
    handle = f'<span class="mention-handle">{_esc(login)}</span>'
    if not interactive:
        return f'<span class="author-chip">{badge}<span class="{cls}">@{handle}</span></span>'
    attrs = [
        f'class="entity-trigger {cls}"',
        f'href="https://github.com/{_esc(login)}"',
        'target="_blank"', 'rel="noopener"',
        'data-tracked-trigger', 'data-kind="person"', f'data-key="{_esc(login)}"',
    ]
    note = people_notes.get(login)
    if note:
        attrs.append(f'data-tip="{_esc(note)}"')
    return f'<span class="author-chip">{badge}<a {" ".join(attrs)}>@{handle}</a></span>'


def render(text: str | None, *, cur_repo: str | None = None,
           interactive: bool = False,
           tracked_people=frozenset(), people_notes: dict | None = None,
           author_assocs: dict | None = None) -> Markup:
    """Render the full inline subset. `interactive=False` (e.g. a hover tooltip
    that can't host clickable links) renders refs/mentions as styled <span>s
    instead of <a>s; `code` always renders. `cur_repo` ("owner/name") resolves
    bare #NN and bare repo#NN, and shortens same-repo URLs / cross-repo refs.

    `author_assocs`: {login: {'assoc': str, 'badge_class': str}} — the
    per-thread author roster built by web._build_author_roster. Lets
    @mentions pick up the same self/member/first-time badge that the chip
    macro uses; unknown logins fall through to the muted generic icon."""
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
            out.append(_render_mention(
                tok[1:], interactive, tracked_people, people_notes, author_assocs,
            ))
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
