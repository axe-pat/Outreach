from __future__ import annotations

from html.parser import HTMLParser
from urllib.request import Request, urlopen


class HtmlSegmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_href: str | None = None
        self._ignored_depth = 0
        self._current_link_text: list[str] = []
        self.segments: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag_name == "a":
            self._current_href = dict(attrs).get("href")
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag_name == "a" and self._current_href is not None:
            text = self._normalize(" ".join(self._current_link_text))
            self.segments.append({"kind": "link", "text": text, "href": self._current_href})
            self._current_href = None
            self._current_link_text = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = self._normalize(data)
        if not text:
            return
        if self._current_href is not None:
            self._current_link_text.append(text)
            return
        self.segments.append({"kind": "text", "text": text})

    def _normalize(self, value: str) -> str:
        return " ".join(value.split()).strip()


class HttpTextDownloader:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_text(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36 OutreachEngine/0.1"
                )
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")


def extract_html_segments(html: str) -> list[dict[str, str]]:
    parser = HtmlSegmentParser()
    parser.feed(html)
    return parser.segments
