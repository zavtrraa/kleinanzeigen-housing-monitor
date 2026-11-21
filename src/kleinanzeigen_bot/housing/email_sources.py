import asyncio
import imaplib
import re
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from .models import Candidate, SourceBlocked, SourceResult


def _canonical_offer(url: str):
    # Unwrap tracking parameters as data only; never fetch email URLs.
    for _ in range(3):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        path = unquote(parsed.path)
        if parsed.scheme == "https" and parsed.port in (None, 443) and not parsed.username:
            if host in {"www.immobilienscout24.de", "immobilienscout24.de"}:
                match = re.fullmatch(r"/expose/(\d+)/?", path)
                if match:
                    return (
                        "immoscout24",
                        match[1],
                        f"https://www.immobilienscout24.de/expose/{match[1]}",
                    )
            if host in {"www.immowelt.de", "immowelt.de"}:
                match = re.fullmatch(r"/expose/([a-zA-Z0-9-]{6,})/?", path)
                if match:
                    return "immowelt", match[1], f"https://www.immowelt.de/expose/{match[1]}"
        query = parse_qs(parsed.query)
        wrapped = next(
            (
                query[key][0]
                for key in ("url", "redirect", "target", "redirectUrl")
                if query.get(key)
            ),
            None,
        )
        if wrapped is None:
            return None
        url = wrapped
    return None


def canonical_offer(url: str):
    try:
        return _canonical_offer(url)
    except (ValueError, UnicodeError):
        return None


def parse_alert(raw: bytes) -> list[Candidate]:
    if len(raw) > 2_000_000:
        raise ValueError("Email exceeds configured parsing limit")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From", "")))[1]
    domain = sender.rsplit("@", 1)[-1].casefold()
    source = None
    for expected, names in {
        "immowelt": ("immowelt.de",),
        "immoscout24": ("immobilienscout24.de", "immoscout24.de"),
    }.items():
        if any(domain == name or domain.endswith("." + name) for name in names):
            source = expected
    if source is None:
        return []
    body = message.get_body(preferencelist=("html", "plain"))
    if body is None:
        raise ValueError("Trusted alert has no supported message body")
    content = body.get_content()
    if body.get_content_type() == "text/plain":
        # A plain-text alert still yields links; unknown fields stay for manual review.
        links = re.findall(r'https://[^\s<>"\']+', content)
        html = "".join(f'<p><a href="{u.replace(chr(34), "&quot;")}">{u}</a></p>' for u in links)
        soup = BeautifulSoup(html, "html.parser")
    else:
        soup = BeautifulSoup(content, "html.parser")
    result = {}
    for link in soup.select("a[href]"):
        parsed = canonical_offer(link["href"])
        if not parsed or parsed[0] != source:
            continue
        _, external_id, url = parsed
        best = link
        for ancestor in list(link.parents)[:6]:
            targets = {
                found[:2]
                for item in ancestor.select("a[href]")
                if (found := canonical_offer(item["href"])) is not None
            }
            if len(targets) != 1:
                break
            best = ancestor
        text = best.get_text(" ", strip=True)[:6000]
        heading = best.find(["h1", "h2", "h3", "h4"])
        title = heading.get_text(" ", strip=True) if heading else link.get_text(" ", strip=True)
        if (
            not title
            or title.startswith("http")
            or title.casefold() in {"ansehen", "zum exposé", "angebot ansehen"}
        ):
            title = str(message.get("Subject", "New listing"))
        city = (
            "Hannover"
            if re.search(r"\b(?:\d{5}\s+)?Hannover\b", text, re.I)
            and not re.search(r"\b(?:Region|Landkreis)\s+Hannover\b", text, re.I)
            else None
        )
        size = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:m²|m2|qm)\b", text)
        rooms = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:Zimmer|Zi\.?)\b", text)
        available = re.search(
            r"\b(?:ab|frei ab|verfügbar ab)\s+(\d{2})\.(\d{2})\.(\d{4})", text, re.I
        )
        when = f"{available[3]}-{available[2]}-{available[1]}" if available else None
        candidate = Candidate(
            source=source,
            external_id=external_id,
            url=url,
            title=title[:240],
            city=city,
            size_sqm=size[1].replace(",", ".") if size else None,
            rooms=rooms[1].replace(",", ".") if rooms else None,
            description=text,
            available_from=when,
        )
        # Prefer the richest occurrence, not a later generic "open" link.
        if candidate.key not in result or len(text) > len(result[candidate.key].description):
            result[candidate.key] = candidate
    if not result:
        raise ValueError("Trusted alert contains no supported listing link; inspect manually")
    return list(result.values())


class GmailSource:
    name = "gmail"

    def __init__(self, config, address: str, app_password: str):
        self.config, self.address, self.app_password = config, address, app_password
        self.interval = config.gmail_interval_seconds

    def _read(self, checkpoint):
        if not self.address or not self.app_password:
            raise ValueError("Gmail connection is not configured")
        connection = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
        try:
            try:
                connection.login(self.address, self.app_password.replace(" ", ""))
            except imaplib.IMAP4.error as exc:
                raise SourceBlocked("Gmail authorization requires manual verification") from exc
            mailbox = self.config.gmail_folder
            if any(character in mailbox for character in ('"', "\\", "\r", "\n")):
                raise ValueError("Unsupported mailbox name")
            status, _ = connection.select('"' + mailbox + '"', readonly=True)
            if status != "OK":
                raise ValueError("Gmail Housing folder not found")
            validity_data = connection.response("UIDVALIDITY")[1]
            validity = (validity_data[0] or b"").decode()
            if not validity:
                raise ValueError("Gmail did not provide UIDVALIDITY")
            old = checkpoint or {}
            same = old.get("uidvalidity") == validity
            last = int(old.get("uid", 0)) if same else 0
            status, data = connection.uid("search", None, "ALL")
            if status != "OK":
                raise ValueError("Gmail UID search failed")
            uids = [int(value) for value in data[0].split() if int(value) > last]
            uids = uids[:100] if same else uids[-self.config.gmail_initial_messages :]
            candidates, failed, parse_failed = [], [], []
            for uid in uids:
                status, size_data = connection.uid("fetch", str(uid), "(RFC822.SIZE)")
                if status != "OK":
                    raise ValueError("Gmail message size query failed")
                size_repr = repr(size_data)
                size_match = re.search(r"RFC822.SIZE (\d+)", size_repr)
                if not size_match or int(size_match[1]) > 2_000_000:
                    failed.append(uid)
                    continue
                status, parts = connection.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise ValueError("Gmail message fetch failed")
                raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
                if raw is None:
                    raise ValueError("Gmail message body missing")
                try:
                    candidates.extend(parse_alert(raw))
                except (ValueError, LookupError, UnicodeError):
                    parse_failed.append(uid)
            return SourceResult(
                candidates,
                {"uidvalidity": validity, "uid": max(uids) if uids else last},
                {
                    "messages": len(uids),
                    "oversized_skipped": failed,
                    "parse_failed_uids": parse_failed,
                    "uidvalidity_reset": not same,
                },
            )
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    async def collect(self, checkpoint=None):
        return await asyncio.to_thread(self._read, checkpoint)
