"""RSS feed parsing (pure: no network)."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from .payloads import EpisodeRef

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_HPR = re.compile(r"^HPR(\d+)\s*:\s*(.*)$", re.I)


def episode_key(title: str, guid: str) -> str:
    m = _HPR.match(title.strip())
    if m:
        return f"hpr{int(m.group(1))}"
    return "ep-" + hashlib.sha256((guid or title).encode()).hexdigest()[:10]


def clean_author(raw: str) -> str:
    """RSS <author> is often 'email (Name)'. Keep the name, never an address."""
    raw = (raw or "").strip()
    m = re.search(r"\(([^()]+)\)\s*$", raw)
    if m:
        raw = m.group(1).strip()
    return "" if "@" in raw else raw


def parse_feed(xml_text: str) -> list[EpisodeRef]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("not an RSS 2.0 feed: no <channel>")
    out: list[EpisodeRef] = []
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        title = (item.findtext("title") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        summary = (item.findtext(f"{ITUNES}summary") or item.findtext(f"{ITUNES}subtitle") or "").strip()
        author = clean_author(item.findtext(f"{ITUNES}author") or item.findtext("author") or "")
        out.append(
            EpisodeRef(
                key=episode_key(title, guid),
                title=title,
                audio_url=enc.get("url", ""),
                page_url=(item.findtext("link") or "").strip(),
                published=(item.findtext("pubDate") or "").strip(),
                author=author,
                summary=re.sub(r"<[^>]+>", " ", summary)[:500].strip(),
            )
        )
    return out


def select(episodes: list[EpisodeRef], only: list[str], limit: int) -> list[EpisodeRef]:
    if only:
        wanted = {k.lower() for k in only}
        picked = [e for e in episodes if e.key.lower() in wanted]
        missing = wanted - {e.key.lower() for e in picked}
        if missing:
            raise ValueError(f"episodes not in feed: {sorted(missing)}")
        return picked
    return episodes[: max(0, limit)]
