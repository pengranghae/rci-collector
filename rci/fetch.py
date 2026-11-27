"""공개 웹페이지 가져오기 — robots.txt 확인, 요청 간격, 시간 제한.

표준 라이브러리(urllib)만 쓴다. 회사 프록시는 HTTPS_PROXY 환경변수를 자동으로 따른다.
사내 SSL 검사 장비 때문에 인증서 오류가 나면 config.json의 ca_bundle에 회사 인증서 파일 경로를 넣는다.
로그인·쿠키·세션은 다루지 않는다.
"""
from __future__ import annotations

import gzip
import re
import ssl
import time
import urllib.error
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, user_agent: str, delay_sec: float = 3.0, timeout: float = 15.0, ca_bundle: str | None = None, respect_robots: bool = True):
        self.ua = user_agent
        self.delay = delay_sec
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self._robot_err: dict[str, str] = {}

    def _robots_ok(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                raw = self._raw(base + "/robots.txt", check_robots=False)
                rp.parse(raw.decode("utf-8", "replace").splitlines())
                self._robots[base] = rp
            except FetchError as e:
                # robots.txt가 없으면(404) 허용, 접속 자체가 안 되면 그 오류를 그대로 알린다
                if "HTTP 404" in str(e):
                    self._robots[base] = None
                else:
                    self._robots[base] = None
                    self._robot_err[base] = str(e)
        if base in self._robot_err:
            raise FetchError("robots.txt 확인 실패 — " + self._robot_err[base])
        rp = self._robots[base]
        if rp is None:
            return True
        return rp.can_fetch(self.ua, url)

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = time.time() - self._last.get(host, 0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last[host] = time.time()

    def _raw(self, url: str, check_robots: bool = True, headers: dict | None = None) -> bytes:
        if check_robots and not self._robots_ok(url):
            raise FetchError(f"robots.txt가 이 주소의 수집을 허용하지 않음: {url}")
        self._wait(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip", "Accept-Language": "ko-KR,ko;q=0.9", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            raise FetchError(f"HTTP {e.code}: {url}") from e
        except urllib.error.URLError as e:
            raise FetchError(f"접속 실패({e.reason}): {url}") from e
        except Exception as e:  # 시간 초과 등
            raise FetchError(f"{type(e).__name__}: {url}") from e

    def get_text(self, url: str) -> str:
        data = self._raw(url)
        m = re.search(rb'charset=["\']?([\w-]+)', data[:2000])
        enc = m.group(1).decode() if m else "utf-8"
        try:
            return data.decode(enc, "replace")
        except LookupError:
            return data.decode("utf-8", "replace")

    def get_api_json(self, url: str, headers: dict) -> dict:
        """공식 검색 API(예: 네이버 검색 API) 호출. 웹페이지 크롤링이 아니라 발급받은 키로 쓰는
        공개 API이므로 robots.txt 대상이 아니다. 요청 간격은 똑같이 지킨다."""
        import json
        try:
            return json.loads(self._raw(url, check_robots=False, headers=headers).decode("utf-8", "replace"))
        except ValueError as e:
            raise FetchError(f"API 응답이 JSON이 아님: {url}") from e


# ---------- RSS ----------
NS = {"content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}


def parse_rss(xml_text: str) -> list[dict]:
    """RSS 2.0(워드프레스 기본 피드) 항목 목록."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as e:
        raise FetchError(f"RSS 형식이 아님: {e}") from e
    items = []
    for it in root.iter("item"):
        def g(tag: str) -> str:
            el = it.find(tag, NS)
            return (el.text or "").strip() if el is not None and el.text else ""
        pub = g("pubDate")
        try:
            pub_d = parsedate_to_datetime(pub).date() if pub else None
        except (TypeError, ValueError):
            pub_d = None
        items.append({"title": g("title"), "link": g("link"), "pub": pub_d, "description": g("description"),
                      "content": g("content:encoded"), "categories": [c.text or "" for c in it.findall("category")]})
    return items


# ---------- HTML → 본문 평문 ----------
class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article", "td", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_article = 0
        self.all: list[str] = []
        self.article: list[str] = []
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta" and a.get("content"):
            key = a.get("property") or a.get("name") or ""
            if key in ("og:title", "article:published_time", "og:url", "description"):
                self.meta[key] = a["content"]
        cls = (a.get("class") or "") + " " + (a.get("id") or "")
        if tag == "article" or re.search(r"entry-content|article[-_]?(body|content|view)|post[-_]content|news[-_]?(view|content)", cls):
            self.in_article += 1
        if tag in self.BLOCK:
            self.all.append("\n")
            if self.in_article:
                self.article.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "article" and self.in_article:
            self.in_article -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.skip:
            return
        self.all.append(data)
        if self.in_article:
            self.article.append(data)


def html_to_text(html: str) -> dict:
    p = _TextParser()
    p.feed(html)
    art = re.sub(r"\n\s*\n+", "\n", "".join(p.article)).strip()
    whole = re.sub(r"\n\s*\n+", "\n", "".join(p.all)).strip()
    text = art if len(art) >= 200 else whole
    return {"title": (p.meta.get("og:title") or p.title).strip(), "text": text,
            "published": p.meta.get("article:published_time", "")}
