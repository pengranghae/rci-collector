"""출처 후보 점검 — 사이트마다 robots.txt 허용 여부와 RSS 피드 유무를 확인한다.

사이트당 요청은 최대 4번(robots.txt, 첫 페이지, 피드 후보 최대 2개)이며
요청 간격·robots.txt 규칙은 수집과 똑같이 지킨다. 아무것도 저장소에 쓰지 않는다.
"""
from __future__ import annotations

import csv
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .fetch import FetchError, Fetcher, parse_rss

RECRUIT_RE = re.compile(r"채용|공채|신입사원|인적성|GSAT|SKCT|DCAT|HMAT|서류\s*접수")
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)


def discover_feeds(html: str, base_url: str) -> list[str]:
    """HTML 머리의 <link rel="alternate" type="application/rss+xml"> 주소를 찾는다."""
    out = []
    for tag in _LINK_RE.findall(html or ""):
        if not re.search(r"""type\s*=\s*["']application/(?:rss|atom)\+xml["']""", tag, re.I):
            continue
        m = re.search(r"""href\s*=\s*["']([^"']+)["']""", tag, re.I)
        if m:
            u = urljoin(base_url, m.group(1).replace("&amp;", "&"))
            # 댓글 피드는 제외
            if "comments" not in u and u not in out:
                out.append(u)
    return out


def _guess_feeds(url: str) -> list[str]:
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    path = p.path if p.path.endswith("/") else (p.path + "/" if "." not in p.path.rsplit("/", 1)[-1] else p.path.rsplit("/", 1)[0] + "/")
    cands = [urljoin(root, path + "feed/"), urljoin(root, "/feed/"), urljoin(root, path + "rss")]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def probe_site(fetcher: Fetcher, site: dict, lookback_days: int = 450) -> dict:
    url = site["url"]
    r = {"name": site.get("name", url), "covers": site.get("covers", ""), "url": url, "page": "", "feed": "",
         "items": 0, "recruit_items": 0, "newest": "", "recruit_titles": "", "verdict": "", "note": ""}
    feeds: list[str] = []
    try:
        html = fetcher.get_text(url)
        r["page"] = "접속됨"
        feeds = discover_feeds(html, url)
    except FetchError as e:
        msg = str(e)
        r["page"] = "실패"
        r["note"] = msg
        if "robots.txt가" in msg:
            r["verdict"] = "제외(robots.txt 불허)"
            return r
        if "robots.txt 확인 실패" in msg:
            r["verdict"] = "확인 불가"
            return r
    tried = 0
    for f in feeds + [g for g in _guess_feeds(url) if g not in feeds]:
        if tried >= 2:
            break
        tried += 1
        try:
            items = parse_rss(fetcher.get_text(f))
        except FetchError as e:
            if not r["note"]:
                r["note"] = str(e)
            continue
        if not items:
            continue
        since = date.today() - timedelta(days=lookback_days)
        rec = [it for it in items if RECRUIT_RE.search(it["title"] + " " + " ".join(it["categories"]))]
        r["feed"] = f
        r["items"] = len(items)
        r["recruit_items"] = len(rec)
        dates = [it["pub"] for it in items if it["pub"]]
        r["newest"] = max(dates).isoformat() if dates else ""
        r["recruit_titles"] = " / ".join(it["title"][:40] for it in rec[:3])
        r["note"] = ""
        break
    if r["feed"]:
        r["verdict"] = "추가 가능" if r["items"] else "피드 비어 있음"
        if r["items"] and not r["recruit_items"]:
            r["note"] = "피드는 정상. 지금 보이는 최근 글에 채용 글이 없을 뿐일 수 있음"
    elif r["page"] == "접속됨":
        r["verdict"] = "RSS 없음(목록 페이지 방식 필요)"
    elif not r["verdict"]:
        r["verdict"] = "접속 실패"
    return r


def run_probe(fetcher: Fetcher, sites: list[dict], out_csv: Path, log) -> list[dict]:
    results = []
    for s in sites:
        log.info("점검: %s (%s)", s.get("name"), s["url"])
        r = probe_site(fetcher, s)
        log.info("  → %s | 피드 %s | 글 %d건 중 채용 %d건 | 최근 %s %s", r["verdict"], r["feed"] or "-", r["items"], r["recruit_items"], r["newest"] or "-", ("| " + r["note"]) if r["note"] else "")
        results.append(r)
    cols = [("name", "출처"), ("covers", "대상 기업"), ("verdict", "판정"), ("url", "주소"), ("feed", "피드"), ("items", "피드 글 수"),
            ("recruit_items", "채용 관련 글"), ("newest", "최근 글 날짜"), ("recruit_titles", "채용 글 예시"), ("note", "비고")]
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as fp:  # 엑셀에서 한글이 깨지지 않게 BOM 포함
        w = csv.writer(fp)
        w.writerow([c[1] for c in cols])
        for r in results:
            w.writerow([r[c[0]] for c in cols])
    return results
