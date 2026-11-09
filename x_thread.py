#!/usr/bin/env python3
"""x_thread.py: fetch an X (Twitter) post and its author thread without a browser.

Port of nyanshiba's OpenCode plugin `x-thread`
(harness-bonsai, .config/opencode/plugins/x-thread/index.ts, MIT) to a
standalone Muse helper. The engine is plain HTTPS only:

  - cdn.syndication.twimg.com/tweet-result for post bodies (no auth)
  - x.com status pages (unauthenticated HTML) only as an ID index and as
    the source for note-tweet full text. Raw HTML never leaves this script.

Features (same numbering as the original):
  1. Whole tree: walk up via in_reply_to_status_id_str to the root, then
     extend down along the author's self-reply chain.
  2. Replies by others, separated from the thread body.
  3. Quoted posts, followed until no new quote appears.
  4. Note tweets: full text recovered from the page HTML, t.co expanded.

Usage:
  x_thread.py <status-url | post-id> [--json] [--mode thread|post]
  x_thread.py https://x.com/eris_create_lab/status/1985660000000000000
  x_thread.py 12345 67890 --media-dir media --frames 4

Exit codes: 0 ok, 2 bad target, 3 start post unavailable, 4 fetch failed.
Debug: X_THREAD_DEBUG=1 appends diagnostics to $X_THREAD_LOG
(default in the system temporary directory) and the rendered digest to
$X_THREAD_RENDER (also in the system temporary directory by default).
"""

import argparse
import datetime
import html as htmlmod
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
CDN_HOST = "cdn.syndication.twimg.com"
MAX_HOPS = 12
PAGE_ATTEMPTS = 4
SYND_TIMEOUT = 10
PAGE_TIMEOUT = 20

STATUS_URL = re.compile(
    r"https?://(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/([^/]+)/status(?:es)?/(\d+)"
)
MIRROR_HOST = re.compile(r"^(?:api\.)?(?:fxtwitter|vxtwitter)\.com$")
REST_ID_RE = re.compile(r'\\?"rest_id\\?":\\?"(\d{19})\\?"')
CONVERSATION_RE = re.compile(r"conversationthread-(\d{19})")
FALLBACK_ID_RE = re.compile(r"\b2\d{18}\b")
TEXT_RE = re.compile(r'"text":"((?:[^"\\]|\\.)*)"')
EXPANDED_RE = re.compile(r'"expanded_url":"((?:[^"\\]|\\.)*)"[^{}]{0,240}?"url":"((?:[^"\\]|\\.)*)"')


def dlog(line: str) -> None:
    if os.environ.get("X_THREAD_DEBUG") != "1":
        return
    path = os.environ.get("X_THREAD_LOG", os.path.join(tempfile.gettempdir(), "x-thread.log"))
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def id_cmp_key(post_id: str):
    # Snowflake IDs exceed float/int53 safety in some languages. Python ints
    # are exact, but keep the original length-then-lexicographic rule so the
    # ordering matches the reference implementation bit for bit.
    return (len(post_id), post_id)


def status_target(url: str):
    """Return (author, start_id) for direct, mirror and oembed URLs."""
    m = STATUS_URL.search(url)
    if m:
        return m.group(1), m.group(2)
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.hostname and MIRROR_HOST.match(u.hostname):
        m = re.match(r"^/([^/]+)/status(?:es)?/(\d+)", u.path)
        if m:
            return m.group(1), m.group(2)
        return None
    if u.hostname == "publish.twitter.com":
        inner = parse_qs(u.query).get("url", [None])[0]
        if inner:
            m = STATUS_URL.search(inner)
            if m:
                return m.group(1), m.group(2)
    if re.fullmatch(r"\d{5,25}", url.strip()):
        return None, url.strip()
    return None


# --- syndication -------------------------------------------------------------
# Calls are serialized (one at a time, in order), as in the original, which
# chained them over a single keep-alive connection. urllib is used so the
# VM's HTTPS proxy settings are honored. A plain direct TLS connection from
# this VM fails, so do not swap this back to http.client.


class Syndication:
    def fetch(self, post_id: str):
        """Return the tweet JSON dict, {'__status': ...} for HTTP errors,
        or None for transport/parse failures."""
        url = f"https://{CDN_HOST}/tweet-result?id={post_id}&lang=en&token=0"
        req = urllib.request.Request(
            url,
            headers={"user-agent": USER_AGENT, "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=SYND_TIMEOUT) as res:
                text = res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                dlog(f'syndication HTTP 404 for {post_id}')
            else:
                print(f'warning: syndication HTTP {e.code} for {post_id}', file=sys.stderr)
            return {"__status": e.code}
        except (OSError, http.client.HTTPException) as e:
            print(f'warning: syndication fetch failed: {e}', file=sys.stderr)
            return None
        if len(text) <= 2:
            dlog(f'syndication empty response for {post_id}')
            return {"__status": "empty"}
        try:
            data = json.loads(text)
        except ValueError as e:
            print(f'warning: syndication response invalid: {e}', file=sys.stderr)
            return None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id_str"), str)
            or not data["id_str"].isdigit()
        ):
            return None
        return data


SYND = Syndication()


def fetch_page(post_id: str, author: str) -> str:
    """Fetch the unauthenticated x.com status page HTML, or '' on failure."""
    url = f"https://x.com/{author}/status/{post_id}"
    req = urllib.request.Request(
        url,
        headers={
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "accept-language": "en",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT) as res:
            ct = (res.headers.get("content-type") or "").lower()
            if ct and "html" not in ct:
                return ""
            return res.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        print(f'warning: page fetch failed: {e}', file=sys.stderr)
        return ""


# --- payload shaping ---------------------------------------------------------


def links_of(j) -> list:
    out = []
    entities = (j or {}).get("entities") or {}
    for e in list(entities.get("urls") or []) + list(entities.get("media") or []):
        if not isinstance(e, dict):
            continue
        expanded = e.get("expanded_url") or e.get("media_url_https")
        if e.get("url") and expanded:
            out.append({"tco": e["url"], "url": expanded})
    return out


def expand_links(text: str, links: list) -> str:
    out = text
    for link in sorted(links, key=lambda l: -len(l["tco"])):
        if link["tco"] in out:
            out = out.replace(link["tco"], link["url"])
    return out


def to_tweet(j) -> dict:
    quoted = (j or {}).get("quoted_tweet") or {}
    quotes = []
    if quoted.get("id_str"):
        quotes.append(
            {
                "id": str(quoted["id_str"]),
                "author": (quoted.get("user") or {}).get("screen_name") or "?",
                "text": quoted.get("text") or "",
                "links": links_of(quoted),
                **synd_extra(quoted),
            }
        )
    article = (j or {}).get("article") or {}
    return {
        "id": j["id_str"],
        "author": (j.get("user") or {}).get("screen_name") or "?",
        "created_at": j.get("created_at"),
        "text": j.get("text") or "",
        "is_note": "note_tweet" in j,
        "is_article": bool(article),
        "article_title": article.get("title"),
        "article_preview": article.get("preview_text"),
        "full_text": None,
        "parent": j.get("in_reply_to_status_id_str") or None,
        "quotes": quotes,
        "links": links_of(j),
        **synd_extra(j),
    }


def candidates_in(html: str, known: set, min_id: str) -> list:
    primary = REST_ID_RE.findall(html) + CONVERSATION_RE.findall(html)
    found = list(dict.fromkeys(primary))
    if found:
        return sorted(
            (c for c in found if id_cmp_key(c) > id_cmp_key(min_id) and c not in known),
            key=id_cmp_key,
        )
    return sorted(
        (
            c
            for c in dict.fromkeys(FALLBACK_ID_RE.findall(html))
            if id_cmp_key(c) > id_cmp_key(min_id) and c not in known
        ),
        key=id_cmp_key,
    )


# --- note full text ----------------------------------------------------------

_norm_re = re.compile(r"\s+")


def norm(s: str) -> str:
    return _norm_re.sub(" ", s).strip()


def json_str(raw: str):
    try:
        v = json.loads(f'"{raw}"')
        return v if isinstance(v, str) else None
    except Exception:
        return None


def decode_json_strings(html: str) -> list:
    unescaped = html.replace('\\"', '"').replace("\\\\", "\\")
    out = []
    for m in TEXT_RE.finditer(unescaped):
        v = json_str(m.group(1))
        if v is not None:
            out.append(v)
    return out


def note_key(text: str) -> str:
    normalized = norm(text)
    normalized = re.sub(r"\s*https?://t\.co/\w+\s*$", "", normalized, flags=re.I)
    normalized = re.sub(r"…\s*$", "", normalized).strip()
    return normalized[:120]


def note_full_text(html: str, truncated: str):
    texts = [t for t in decode_json_strings(html) if len(t) > len(truncated) + 20]
    if not texts:
        return None
    key = note_key(truncated)
    if not key:
        return None
    starts = [t for t in texts if norm(t).startswith(key)]
    if not starts:
        return None
    return max(starts, key=len)


def links_from_html(html: str) -> list:
    un = html.replace('\\"', '"').replace("\\\\", "\\")
    out, seen = [], set()
    for m in EXPANDED_RE.finditer(un):
        url = json_str(m.group(1))
        tco = json_str(m.group(2))
        if url is None or tco is None or not tco.startswith("https://t.co/"):
            continue
        if tco in seen:
            continue
        seen.add(tco)
        out.append({"tco": tco, "url": url})
    return out


# --- article full text (addition in this port, not in the original) -----------
# Article posts carry only a title and preview in the syndication payload.
# The page HTML embeds the article as Draft.js style content_state blocks,
# so the body can be rebuilt exactly, block by block, in order.


def _extract_json_array(text: str, start: int):
    """Return the JSON array starting at text[start] == '[', bracket matched
    with string awareness, or None."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except Exception:
                    return None
    return None


def article_code_blocks(page_html: str) -> list:
    """Code blocks in an article render as x-article-code divs in the page
    HTML, outside content_state. Extract their plain text in order."""
    un = page_html.replace('\\"', '"').replace("\\\\", "\\")
    codes = []
    for m in re.finditer(r'class="x-article-code group', un):
        pre_start = un.find("<pre", m.start())
        if pre_start < 0:
            continue
        pre_end = un.find("</pre>", pre_start)
        if pre_end < 0:
            continue
        text = htmlmod.unescape(re.sub(r"<[^>]+>", "", un[pre_start:pre_end])).strip()
        if text and text not in codes:
            codes.append(text)
    return codes


def article_full_text(html: str, title, preview):
    un = html.replace('\\"', '"').replace("\\\\", "\\")
    idx = un.find('"content_state"')
    if idx < 0:
        return None
    bidx = un.find('"blocks":', idx)
    if bidx < 0:
        return None
    start = un.find("[", bidx)
    if start < 0:
        return None
    blocks = _extract_json_array(un, start)
    if not blocks:
        return None
    parts = []  # ("text", str) or ("atomic", None) placeholders, in order
    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type") or "unstyled"
        text = b.get("text") or ""
        if btype == "atomic":
            parts.append(("atomic", None))
            continue
        if not text.strip():
            continue
        parts.append(("text", f"## {text}" if btype.startswith("header") else text))
    texts = [v for kind, v in parts if kind == "text"]
    if not texts:
        return None
    if preview:
        plain = re.sub(r"^## ", "", "\n\n".join(texts), flags=re.M)
        if not norm(plain).startswith(norm(preview)[:40]):
            return None
    # Splice code blocks in. With exactly one, it belongs at the last
    # atomic block (the usual "paste this prompt" position). With several,
    # append them at the end in order rather than guess positions.
    codes = article_code_blocks(html)
    atomic_at = [i for i, (kind, _) in enumerate(parts) if kind == "atomic"]
    if len(codes) == 1 and atomic_at:
        parts[atomic_at[-1]] = ("text", f"```\n{codes[0]}\n```")
    elif codes:
        for code in codes:
            parts.append(("text", f"```\n{code}\n```"))
    body = "\n\n".join(v for kind, v in parts if kind == "text")
    if title and norm(title) not in norm(body):
        body = f"# {title}\n\n{body}"
    return body


# --- thread assembly ---------------------------------------------------------


class StartUnavailable(LookupError):
    """The starting post could not be fetched from syndication."""


class Thread:
    def __init__(self, start_id: str):
        self.start_id = start_id
        self.posts = []
        self.ancestors = []
        self.replies = []
        self.quoted = []
        self.excluded = 0
        self.source = "syndication"


def adopt(t: Thread, post: dict, known: set) -> bool:
    if post["author"] == t.posts[0]["author"]:
        if post["parent"] and any(p["id"] == post["parent"] for p in t.posts):
            t.posts.append(post)
            known.add(post["id"])
            return True
    elif post["parent"] and post["parent"] in known:
        t.replies.append(post)
        known.add(post["id"])
        return False
    t.excluded += 1
    known.add(post["id"])
    return False


def probe(t: Thread, ids: list, known: set) -> int:
    added = 0
    for post_id in ids:
        j = SYND.fetch(post_id)
        if not j or "__status" in j:
            t.excluded += 1
            known.add(post_id)
            continue
        if adopt(t, to_tweet(j), known):
            added += 1
    return added


def build(start_id: str) -> Thread:
    t0 = time.time()
    t = Thread(start_id)
    known = set()
    pages = {}

    def load_page(post_id: str, author: str) -> str:
        key = f"{author}/{post_id}"
        if key in pages:
            return pages[key]
        html = fetch_page(post_id, author)
        if html:
            pages[key] = html
        return html

    # Feature 1, up: walk to the root.
    cur = start_id
    while cur and cur not in known and len(t.posts) < 500:
        known.add(cur)
        j = SYND.fetch(cur)
        if not j or "__status" in j:
            t.excluded += 1
            break
        t.posts.append(to_tweet(j))
        cur = t.posts[-1]["parent"]
    if not t.posts:
        raise StartUnavailable("start post unavailable")
    t.posts.sort(key=lambda p: id_cmp_key(p["id"]))
    dlog(f"up-walk: start={start_id} posts={len(t.posts)} root={t.posts[0]['id']}")

    # Split other authors on the start path out of the body.
    root_author = t.posts[0]["author"]
    t.ancestors = [p for p in t.posts if p["author"] != root_author]
    if t.ancestors:
        t.posts = [p for p in t.posts if p["author"] == root_author]

    # The start page is fetched once here and reused below (down-walk seed,
    # reply enumeration and note completion all read from the same cache).
    start_post = next((p for p in t.posts if p["id"] == start_id), None) or next(
        (p for p in t.ancestors if p["id"] == start_id), None
    )
    start_html = ""
    if start_post:
        start_html = load_page(start_id, start_post["author"])

    # Feature 1, down: extend the author's self-chain to its end.
    for hop in range(MAX_HOPS):
        if not t.posts:
            break
        newest = max(t.posts, key=lambda p: id_cmp_key(p["id"]))["id"]
        cands = []
        for attempt in range(PAGE_ATTEMPTS):
            html = load_page(newest, t.posts[0]["author"])
            cands = candidates_in(html, known, newest)
            if cands:
                break
            dlog(f"thin page for {newest} len={len(html)} attempt={attempt}")
            time.sleep(1.5 * (attempt + 1))
        if not cands:
            break
        added = probe(t, cands, known)
        dlog(f"hop{hop}: cands={len(cands)} added={added}")
        if not added:
            break
    dlog(f"down-walk: posts={len(t.posts)} replies={len(t.replies)} excluded={t.excluded}")

    # Feature 2: replies visible on the start page.
    if start_html:
        extra = candidates_in(start_html, known, "0")
        if extra:
            probe(t, extra, known)

    # Feature 4: complete note tweets from their pages. Article posts take
    # the parallel path added in this port (title and preview only arrive
    # via syndication, the body lives in the page's content_state blocks).
    for p in t.posts:
        if p["is_article"]:
            page_html = load_page(p["id"], p["author"])
            full = article_full_text(page_html, p["article_title"], p["article_preview"])
            if full:
                p["full_text"] = full
                dlog(f"article completed: {p['id']} -> {len(full)} chars")
            continue
        if not p["is_note"]:
            continue
        page_html = load_page(p["id"], p["author"])
        full = note_full_text(page_html, p["text"])
        if full and len(full) > len(p["text"]):
            p["full_text"] = full
            seen = {l["tco"] for l in p["links"]}
            for link in links_from_html(page_html):
                if link["tco"] not in seen:
                    p["links"].append(link)
            dlog(f"note completed: {p['id']} {len(p['text'])} -> {len(full)}")

    t.posts.sort(key=lambda p: id_cmp_key(p["id"]))
    t.replies.sort(key=lambda p: id_cmp_key(p["id"]))

    # Feature 3: follow quotes until no new quote appears.
    post_ids = {p["id"] for p in t.posts}
    info = {}
    for p in t.posts:
        for q in p["quotes"]:
            info[q["id"]] = {"q": q, "quote_of": p["id"]}
    order = list(info.keys())
    done = set()
    i = 0
    while i < len(order):
        qid = order[i]
        i += 1
        if qid in done:
            continue
        done.add(qid)
        j = SYND.fetch(qid)
        if j and "__status" not in j:
            entry = info.get(qid)
            if entry:
                entry["q"]["author"] = (j.get("user") or {}).get("screen_name") or entry["q"]["author"]
                entry["q"]["text"] = j.get("text") or entry["q"]["text"]
                entry["q"]["links"] = links_of(j)
                entry["q"].update(synd_extra(j))
            qt = j.get("quoted_tweet") or {}
            if qt.get("id_str"):
                nid = str(qt["id_str"])
                if nid not in info:
                    info[nid] = {
                        "q": {
                            "id": nid,
                            "author": (qt.get("user") or {}).get("screen_name") or "?",
                            "text": qt.get("text") or "",
                            "links": links_of(qt),
                            **synd_extra(qt),
                        },
                        "quote_of": qid,
                    }
                    order.append(nid)
    dlog(f"quoted walk: {len(info)} unique, {len(done)} refetched")
    for qid in order:
        if qid in post_ids or any(c["id"] == qid for c in t.quoted):
            continue
        entry = info.get(qid)
        if entry:
            t.quoted.append({**entry["q"], "quote_of": entry["quote_of"]})

    dlog(f"build total: ms={int((time.time() - t0) * 1000)}")
    return t


# --- rendering ---------------------------------------------------------------


def post_text(p: dict) -> str:
    return expand_links(p.get("full_text") or p["text"], p["links"])


def render(t: Thread) -> str:
    head = (
        f"[x-thread] @{t.posts[0]['author']} / {len(t.posts)} posts / "
        f"root={t.posts[0]['id']} / start={t.start_id} / source={t.source}"
    )
    lines = [head]
    for i, p in enumerate(t.posts):
        lines += ["", f"{i + 1}. {p['id']} · {p.get('created_at') or '?'}{counts(p)}", post_text(p)]
        if (p.get('is_note') or p.get('is_article')) and not p.get('full_text'):
            lines.append('   [x-thread] long-form post: text may be truncated (full text not recovered)')
        lines.extend(render_extra(p))
    if t.quoted:
        lines += ["", f"[x-thread] quoted by the thread ({len(t.quoted)}):"]
        for q in t.quoted:
            lines.append(f"- {q['id']} · @{q['author']} · quoted by {q['quote_of']}{counts(q)}")
            lines.append(f"  {expand_links(q['text'], q['links']).replace(chr(10), ' ')}")
            lines.extend(render_extra(q))
    if t.ancestors:
        lines += [
            "",
            f"[x-thread] start path between the start post and the root, "
            f"by other authors ({len(t.ancestors)}):",
        ]
        for p in t.ancestors:
            lines.append(f"- {p['id']} · @{p['author']} · {p.get('created_at') or '?'} "
                         f"→ {p['parent'] or '-'}{counts(p)}")
            lines.append(f"  {post_text(p).replace(chr(10), ' ')}")
            lines.extend(render_extra(p))
    if t.replies:
        lines += ["", f"[x-thread] replies by others ({len(t.replies)}):"]
        for p in t.replies:
            lines.append(f"- {p['id']} · @{p['author']} · {p.get('created_at') or '?'} → {p['parent']}{counts(p)}")
            lines.append(f"  {post_text(p).replace(chr(10), ' ')}")
            lines.extend(render_extra(p))
    return "\n".join(lines)


def to_json(t: Thread) -> dict:
    def shape(p, full=False):
        d = {
            "id": p["id"],
            "author": p["author"],
            "created_at": p.get("created_at"),
            "text": post_text(p),
            "is_note": p["is_note"],
            "is_article": p.get("is_article", False),
            "article_title": p.get("article_title"),
            "note_completed": bool(p.get("full_text")),
            "parent": p["parent"],
            "url": f"https://x.com/{p['author']}/status/{p['id']}",
        }
        d.update({k: p.get(k) for k in ("media", "card", "likes", "replies_count")})
        return d

    chain_ok = all(
        i == 0 or any(q["id"] == p["parent"] for q in t.posts)
        for i, p in enumerate(t.posts)
    )
    return {
        "source": t.source,
        "start_id": t.start_id,
        "root_id": t.posts[0]["id"] if t.posts else None,
        "author": t.posts[0]["author"] if t.posts else None,
        "posts": [shape(p) for p in t.posts],
        "ancestors": [shape(p) for p in t.ancestors],
        "replies": [shape(p) for p in t.replies],
        "quoted": [
            {
                "id": q["id"],
                "author": q["author"],
                "text": expand_links(q["text"], q["links"]),
                "quote_of": q["quote_of"],
                **{k: q.get(k) for k in ("media", "card", "likes", "replies_count")},
                "url": f"https://x.com/{q['author']}/status/{q['id']}",
            }
            for q in t.quoted
        ],
        "excluded": t.excluded,
        "diagnostics": {
            "chain_ok": chain_ok,
            "start_included": any(
                p["id"] == t.start_id for p in t.posts + t.ancestors + t.replies
            ),
            "notes_completed": sum(1 for p in t.posts if p.get("full_text")),
            "tco_remaining": len(re.findall(r"https://t\.co/\w+", render(t))),
        },
    }


def variants_urls(variants):
    """Select the highest-quality and small MP4 URLs, or None for each."""
    mp4s = [v for v in variants
            if (v.get('content_type') == 'video/mp4' or v.get('container') == 'mp4') and v.get('url')]
    mp4s.sort(key=lambda v: v.get('bitrate') or 0)
    if not mp4s:
        return None, None
    small = next((v for v in mp4s if (v.get('bitrate') or 0) >= 600000), mp4s[-1])
    return mp4s[-1]['url'], small['url']


def synd_extra(j):
    """Extract media, card and counts from syndication data as a dict."""
    media = []
    for m in j.get('mediaDetails') or []:
        kind = m.get('type')
        info = m.get('original_info') or {}
        item = dict(type=kind, width=info.get('width'), height=info.get('height'), alt=m.get('ext_alt_text'))
        if kind == 'photo':
            item['url'] = m.get('media_url_https')
        elif kind in ('video', 'animated_gif'):
            video = m.get('video_info') or {}
            mp4, small = variants_urls(video.get('variants') or [])
            item.update(poster=m.get('media_url_https'), duration_ms=video.get('duration_millis'),
                        mp4=mp4, mp4_small=small)
        else:
            continue
        media.append(item)
    card_data = j.get('card')
    card = None
    if card_data:
        fields = {k: v['string_value'] for k, v in sorted((card_data.get('binding_values') or {}).items())
                  if 'string_value' in v}
        url = fields.get('card_url')
        for link in (j.get('entities') or {}).get('urls') or []:
            if link.get('url') == url:
                url = link.get('expanded_url') or url
                break
        card = dict(title=fields.get('title'), description=fields.get('description'),
                    domain=fields.get('domain') or fields.get('vanity_url'), url=url)
        if not card['title'] and not card['description']:
            card['fields'] = fields
    return dict(media=media, card=card, likes=j.get('favorite_count'), replies_count=j.get('conversation_count'))


def fx_tweet(j):
    """Convert an fxtwitter tweet into the common post dict."""
    article = j.get('article') or {}
    title = article.get('title')
    blocks = (article.get('content') or {}).get('blocks') or []
    parts = [('## ' if (b.get('type') or '').startswith('header') else '') + b['text']
             for b in blocks if b.get('type') != 'atomic' and (b.get('text') or '').strip()]
    body = '\n\n'.join(parts)
    if title and norm(title) not in norm(body):
        body = '# ' + title + '\n\n' + body
    stamp = j.get('created_timestamp')
    created_at = (datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                  if stamp is not None else None)
    p = dict(id=str(j['id']), author=(j.get('author') or {}).get('screen_name') or '?', created_at=created_at,
             text=j.get('text') or '', is_note=bool(j.get('is_note_tweet')), is_article=bool(article),
             article_title=title, article_preview=article.get('preview_text'), full_text=body if article else None,
             parent=j.get('replying_to_status'), quotes=[], links=[], media=[], card=None,
             likes=j.get('likes'), replies_count=j.get('replies'))
    for m in (j.get('media') or {}).get('all') or []:
        kind = m.get('type')
        if kind == 'gif':
            kind = 'animated_gif'
        item = dict(type=kind, width=m.get('width'), height=m.get('height'), alt=m.get('altText'))
        if kind == 'photo':
            item['url'] = m.get('url')
        elif kind in ('video', 'animated_gif'):
            mp4, small = variants_urls(m.get('variants') or m.get('formats') or [])
            mp4 = mp4 or m.get('url')
            duration = round(m['duration'] * 1000) if m.get('duration') is not None else None
            item.update(poster=m.get('thumbnail_url'), duration_ms=duration, mp4=mp4, mp4_small=small or mp4)
        else:
            continue
        p['media'].append(item)
    if j.get('card'):
        p['card'] = {k: j['card'].get(k) for k in ('title', 'description', 'domain', 'url')}
    return p


def fetch_fx(author, post_id):
    """Fetch an fxtwitter tweet dict, or return None on failure."""
    url = f'https://api.fxtwitter.com/{author or "i"}/status/{post_id}'
    try:
        req = urllib.request.Request(url, headers={'user-agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=SYND_TIMEOUT) as response:
            return json.load(response).get('tweet')
    except (OSError, http.client.HTTPException, ValueError) as e:
        print(f'warning: fxtwitter fetch failed: {e}', file=sys.stderr)
        return None


def safe_url(url):
    """Return whether a media URL uses HTTPS and a trusted twimg host."""
    try:
        u = urlsplit(url or '')
        host = u.hostname or ''
        return (u.scheme == 'https' and (host == 'twimg.com' or host.endswith('.twimg.com'))
                and not u.username and not u.password)
    except ValueError:
        return False


class MediaRedirect(urllib.request.HTTPRedirectHandler):
    """Validate media redirects and return the standard redirect request."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl):
            raise urllib.error.URLError(f'unsafe redirect: {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_bytes(url):
    """Download trusted media and return its bytes."""
    if not safe_url(url):
        raise ValueError('unsafe media URL')
    opener = urllib.request.build_opener(MediaRedirect())
    with opener.open(urllib.request.Request(url, headers={'user-agent': USER_AGENT}), timeout=PAGE_TIMEOUT) as response:
        return response.read()


def make_frames(source, duration_ms, n, output):
    """Create a tiled frame image with ffmpeg; return whether it succeeded."""
    if '://' in source and not safe_url(source):
        print(f'warning: unsafe video URL: {source}', file=sys.stderr)
        return False
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        print('warning: ffmpeg not found', file=sys.stderr)
        return False
    count = n if duration_ms else 1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(count):
                pos = duration_ms / 1000 * (i + .5) / count if duration_ms else 0
                subprocess.run([ffmpeg, '-loglevel', 'error', '-y', '-ss', str(pos), '-i', source,
                                '-frames:v', '1', '-vf', "scale='min(640,iw)':-2", os.path.join(tmp, f'{i:03d}.png')],
                               check=True, timeout=120, capture_output=True)
            cols = min(count, 3)
            rows = (count + cols - 1) // cols
            subprocess.run([ffmpeg, '-loglevel', 'error', '-y', '-framerate', '1', '-i', os.path.join(tmp, '%03d.png'),
                            '-vf', f'tile={cols}x{rows}', '-frames:v', '1', '-q:v', '3', output],
                           check=True, timeout=120, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        print(f'warning: ffmpeg failed: {e}', file=sys.stderr)
        return False


def save_media(posts, directory, frames=0, make_video_frames=True):
    """Save post media and optional frames, updating posts in place; return None."""
    os.makedirs(directory, exist_ok=True)
    for p in posts:
        if not re.fullmatch(r'[0-9]+', str(p['id'])):
            raise ValueError('post ID must contain only digits')
        for i, m in enumerate(p.get('media') or [], 1):
            url = m.get('url') if m['type'] == 'photo' else m.get('poster')
            path = os.path.join(directory, f"{p['id']}_{i}.jpg")
            if not safe_url(url):
                print(f'warning: unsafe media URL: {url}', file=sys.stderr)
            elif os.path.isfile(path):
                m['file'] = path
            else:
                u = urlsplit(url)
                if m['type'] == 'photo' and u.netloc.lower() == 'pbs.twimg.com' and u.path.startswith('/media/'):
                    query = [(k, v) for k, v in parse_qsl(u.query) if k != 'name'] + [('name', 'large')]
                    url = urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), u.fragment))
                try:
                    data = download_bytes(url)
                    fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
                    try:
                        with os.fdopen(fd, 'wb') as out:
                            out.write(data)
                        os.replace(tmp, path)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    m['file'] = path
                except (OSError, http.client.HTTPException) as e:
                    print(f'warning: media download failed: {e}', file=sys.stderr)
            if frames and make_video_frames and m['type'] in ('video', 'animated_gif'):
                dest = os.path.join(directory, f"{p['id']}_{i}_frames.jpg")
                video = m.get('mp4_small')
                if video is None:
                    print(f"warning: no mp4 variant for {p['id']}_{i}", file=sys.stderr)
                elif not safe_url(video):
                    print(f'warning: unsafe video URL: {video}', file=sys.stderr)
                elif os.path.isfile(dest) or make_frames(video, m.get('duration_ms'), frames, dest):
                    m['frames_file'] = dest


def counts(p):
    """Format available post counts as a header suffix."""
    values = [f'{label}={p[key]}' for key, label in [('likes', 'likes'), ('replies_count', 'replies')]
              if p.get(key) is not None]
    return ' · ' + ' '.join(values) if values else ''


def render_extra(p):
    """Return formatted media and card lines for a post."""
    lines = []
    for i, m in enumerate(p.get('media') or [], 1):
        kind = m['type']
        size = f"{m.get('width')}x{m.get('height')}"
        if kind == 'photo':
            line = f"   [photo {i}] {m.get('url')} {size}"
            if m.get('alt') is not None:
                line += ' alt=' + json.dumps(m['alt'], ensure_ascii=False)
        else:
            duration = f"{m['duration_ms']/1000:.1f}s " if m.get('duration_ms') is not None else ''
            line = f"   [{kind} {i}] {duration}{size} mp4={m.get('mp4')}  poster={m.get('poster')}"
        lines.append(line)
        if m.get('file') or m.get('frames_file'):
            if kind == 'photo':
                value = m.get('file', '')
            else:
                value = (('poster=' + m['file']) if m.get('file') else '')
                value += ((' frames=' + m['frames_file']) if m.get('frames_file') else '')
            lines.append(f'   [{kind} {i} file] {value}')
    c = p.get('card')
    if c:
        lines.append('   [card] ' + ' | '.join(str(c.get(k) or '')
                                               for k in ('title', 'description', 'domain', 'url')))
    return lines


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description='Fetch X posts without a browser.')
    ap.add_argument('target', nargs='+', help='status URLs or post IDs')
    ap.add_argument('--json', action='store_true', help='output JSON')
    ap.add_argument('--mode', choices=['thread', 'post'], default='thread', help='fetch a thread or one post')
    ap.add_argument('--media-dir', help='directory for saved media')
    ap.add_argument('--frames', type=int, help='video frames (1–12); requires --media-dir')
    args = ap.parse_args(argv)
    if args.frames is not None and (not 1 <= args.frames <= 12 or not args.media_dir):
        ap.error('--frames must be 1 through 12 and requires --media-dir')
    results = []
    exit_code = 0
    for target in args.target:
        parsed = status_target(target)
        if not parsed:
            print(f'error: not an X status URL or post ID: {target}', file=sys.stderr)
            exit_code = exit_code or 2
            continue
        author, start = parsed
        try:
            if args.mode == 'thread':
                t = build(start)
            else:
                j = SYND.fetch(start)
                if not j or '__status' in j:
                    raise StartUnavailable('start post unavailable')
                t = Thread(start)
                t.posts = [to_tweet(j)]
                t.quoted = [{**q, 'quote_of': start} for q in t.posts[0]['quotes']]
        except StartUnavailable:
            j = fetch_fx(author, start)
            if not j:
                print(f'error: start post {start} unavailable', file=sys.stderr)
                exit_code = exit_code or 3
                continue
            t = Thread(start)
            t.source = 'fxtwitter'
            t.posts = [fx_tweet(j)]
            parent = start
            q = j.get('quote')
            for _ in range(5):
                if not q:
                    break
                qp = fx_tweet(q)
                qp['quote_of'] = parent
                t.quoted.append(qp)
                parent = qp['id']
                q = q.get('quote')
        if args.media_dir:
            save_media(t.posts + t.quoted, args.media_dir, args.frames)
            save_media(t.ancestors, args.media_dir, 0)
        results.append(to_json(t) if args.json else render(t))
        if not args.json and os.environ.get('X_THREAD_DEBUG') == '1':
            path = os.environ.get('X_THREAD_RENDER', os.path.join(tempfile.gettempdir(), 'x-thread-render.txt'))
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(f'\n########## start={start} ##########\n{results[-1]}\n')
    if args.json and results:
        print(json.dumps(results[0] if len(args.target) == 1 else results, ensure_ascii=False, indent=2))
    elif results:
        print('\n\n=====\n\n'.join(results))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
