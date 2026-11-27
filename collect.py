"""채용 캘린더 인사이트 — 일정 수집기 (MVP)

사용법
  python collect.py                      config.json의 출처를 모두 수집하고 inbox.json을 만든다
  python collect.py --source samsung_newsroom      한 출처만
  python collect.py --url https://...  --source samsung_newsroom
                                         공개 페이지 하나를 직접 읽는다(공식 채용 공고 등)
  python collect.py --file 저장한페이지.html --url https://... --source manual
                                         브라우저에서 저장한 페이지 파일을 읽는다(접속 없이)
  python collect.py --export-only        수집 없이 저장소 내용으로 inbox.json만 다시 만든다

결과
  data/collector.sqlite   수집 문서, 추출 일정, 변경 이력, 출처별 상태
  inbox.json              분석 도구 「수집함」 탭에서 불러올 파일
  logs/collect-날짜.log    실행 기록·오류
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

# 설치 없는 Python(embeddable)에서도 rci 폴더를 찾도록 스크립트 위치를 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rci.extract import extract_schedule
from rci.fetch import FetchError, Fetcher, html_to_text, parse_rss
from rci.match import load_pool, match_company
from rci.store import Store, now

VERSION = "0.2.0"
HERE = Path(__file__).resolve().parent
log = logging.getLogger("rci")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_dir / f"collect-{date.today():%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    for old in log.handlers:
        old.close()
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.INFO)


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("delay_sec", 3)
    cfg.setdefault("timeout_sec", 15)
    cfg.setdefault("lookback_days", 450)
    cfg.setdefault("max_items_per_source", 30)
    cfg.setdefault("respect_robots", True)
    # 출처의 "preset": "news" → presets.news 설정을 기본값으로 깔고 출처에 적은 값이 덮어쓴다
    presets = cfg.get("presets", {})
    cfg["sources"] = [{**presets.get(s.get("preset"), {}), **s} for s in cfg.get("sources", [])]
    return cfg


def keyword_hit(text: str, kws: list[str]) -> bool:
    return any(k.lower() in (text or "").lower() for k in kws)


def strip_html(s: str) -> str:
    return html_to_text(s)["text"] if s and "<" in s else (s or "")


def process_page(store: Store, pool: list[dict], src: dict, url: str, title: str, text: str, pub: date | None) -> dict | None:
    """문서 하나 처리: 일정 추출 → 기업 매칭 → 저장·변경 감지."""
    if not pub:
        pub = date.today()
    ex = extract_schedule(title, text, pub)
    if not ex["stages"]:
        log.info("  일정 없음: %s", title[:60])
        return None
    m = match_company(title, text, pool, src.get("default_company"))
    info = {**m, "year": ex["year"], "half": ex["half"], "kind": ex["kind"]}
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    is_new = store.upsert_document(url, src["id"], title, pub.isoformat(), h, info)
    changes = store.merge_extractions(url, ex["stages"])
    log.info("  %s %s | %s | %s", "신규" if is_new else ("변경" if changes else "동일"), m["cname"], title[:50], ", ".join(f"{k}={v['s']}" for k, v in ex["stages"].items()))
    return {"new": is_new, "changes": len(changes)}


NAVER_API = "https://openapi.naver.com/v1/search/news.json"


def _naver_clean(s: str) -> str:
    return html.unescape(re.sub(r"</?b>", "", s or "")).strip()


def naver_queries(src: dict, pool: list[dict]) -> list[tuple[str, str]]:
    """(검색어, 기업명) 목록. Pool 기업마다 검색어 틀(queries)을 채운다. tiers가 있으면 그 등급만."""
    tiers = set(src.get("tiers") or [])
    out = []
    for c in pool:
        if tiers and c.get("tier") and c["tier"] not in tiers:
            continue
        name = re.sub(r"\(.*?\)", "", c["name"]).strip()  # 'HL그룹(HL만도)' → 'HL그룹'
        for q in src.get("queries", ["{name} 신입 채용"]):
            out.append((q.format(name=name), c["name"]))
    return out


def naver_items(fetcher: Fetcher, src: dict, pool: list[dict], stat: dict) -> tuple[list[dict], int]:
    """네이버 뉴스 검색 API로 기사 목록을 모은다. 키는 환경변수(NAVER_CLIENT_ID / NAVER_CLIENT_SECRET)."""
    cid, secret = os.environ.get("NAVER_CLIENT_ID", "").strip(), os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return [], -1
    api = src.get("api_url", NAVER_API)
    hdr = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    items, ok = [], 0
    for q, cname in naver_queries(src, pool):
        url = f"{api}?query={quote(q)}&display={int(src.get('display', 20))}&sort=date"
        try:
            data = fetcher.get_api_json(url, hdr)
            ok += 1
        except FetchError as e:
            stat["errors"].append(f"[{q}] {e}")
            log.warning("검색 실패 [%s]: %s", q, e)
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                break  # 키 문제면 나머지도 똑같이 실패하므로 멈춘다
            continue
        got = data.get("items", [])
        log.info("검색 [%s]: %d건", q, len(got))
        for it in got:
            try:
                pub = parsedate_to_datetime(it.get("pubDate", "")).date()
            except (TypeError, ValueError):
                pub = None
            items.append({"title": _naver_clean(it.get("title")), "link": it.get("originallink") or it.get("link") or "",
                          "pub": pub, "description": _naver_clean(it.get("description")), "content": "", "categories": [],
                          "query_company": cname})
    return items, ok


def feed_items(fetcher: Fetcher, src: dict, stat: dict) -> tuple[list[dict], int]:
    items, ok = [], 0
    for feed in src.get("feeds", []):
        try:
            got = parse_rss(fetcher.get_text(feed))
            ok += 1
        except FetchError as e:
            stat["errors"].append(str(e))
            log.warning("피드 실패: %s", e)
            continue
        log.info("피드 %s: %d건", feed, len(got))
        items.extend(got)
    return items, ok


def collect_source(store: Store, fetcher: Fetcher, pool: list[dict], src: dict, cfg: dict) -> dict:
    """출처 하나 수집.
    kind 'rss'(기본)       : 피드(feeds)의 글 중 키워드에 맞는 것
    kind 'naver_news'      : 네이버 뉴스 검색 API로 Pool 기업마다 검색
    require_company: true  : 제목에 Pool 기업명이 있는 글만(언론사 피드처럼 여러 기업 기사가 섞인 출처용)
    """
    stat = {"items": 0, "kept": 0, "new": 0, "changed": 0, "errors": []}
    since = date.today() - timedelta(days=cfg["lookback_days"])
    limit = int(src.get("max_items", cfg["max_items_per_source"]))
    if src.get("kind") == "naver_news":
        items, feeds_ok = naver_items(fetcher, src, pool, stat)
        if feeds_ok < 0:
            log.info("  네이버 검색 키(NAVER_CLIENT_ID / NAVER_CLIENT_SECRET)가 없어 건너뜀")
            stat["skipped"] = "키 없음"
            return stat
    else:
        items, feeds_ok = feed_items(fetcher, src, stat)
    seen: set[str] = set()
    for it in items:
        if it["link"] in seen or not it["link"]:
            continue
        seen.add(it["link"])
        if it["pub"] and it["pub"] < since:
            continue
        blob = " ".join([it["title"], it["description"], " ".join(it["categories"])])
        if not keyword_hit(blob, src.get("keywords", [])):
            continue
        if any(k in it["title"] for k in src.get("exclude_title", [])):
            continue
        if src.get("require_company") and not match_company(it["title"], "", pool, None)["cname"]:
            continue
        # 뉴스 기사는 한 번 읽으면 일정이 바뀌지 않으므로 다시 읽지 않는다(요청 수 절약)
        if src.get("read_once") and store.was_checked(it["link"]):
            continue
        stat["items"] += 1
        if stat["items"] > limit:
            break
        text = strip_html(it["content"])
        if len(text) < 200:  # 피드에 본문이 없으면 기사 페이지를 읽는다
            try:
                page = html_to_text(fetcher.get_text(it["link"]))
                text = page["text"]
            except FetchError as e:
                stat["errors"].append(str(e))
                log.warning("  본문 실패: %s", e)
                continue
        psrc = {**src, "default_company": src.get("default_company") or it.get("query_company")}
        try:
            r = process_page(store, pool, psrc, it["link"], it["title"], text, it["pub"])
        except Exception as e:  # 한 건의 오류가 전체를 멈추지 않게
            stat["errors"].append(f"처리 오류 {it['link']}: {type(e).__name__} {e}")
            log.exception("  처리 오류")
            continue
        store.mark_checked(it["link"], src["id"])
        if r:
            stat["kept"] += 1
            stat["new"] += r["new"]
            stat["changed"] += 1 if r["changes"] else 0
    ok = feeds_ok > 0
    store.source_result(src["id"], src["name"], ok, None if ok else "; ".join(stat["errors"])[:500], stat["kept"])
    return stat


def candidate_id(url: str, cname: str | None, stages: dict) -> str:
    key = json.dumps({"u": url, "c": cname, "s": {k: [v["s"], v["e"], v["p"]] for k, v in sorted(stages.items())}}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def merge_duplicates(cands: list[dict]) -> list[dict]:
    """같은 기업·시즌·일정을 여러 매체가 보도하면 후보 하나로 합친다.
    대표: 공식 출처 → 먼저 수집된 것 → 주소 순. 나머지는 대표의 alsoReportedBy에 남긴다."""
    groups: dict = {}
    order = []
    for c in cands:
        if not c["cname"]:
            order.append([c])
            continue
        key = (c["cname"], c["year"], c["half"], c["kind"], tuple(sorted((k, v["s"], v["e"], v["p"]) for k, v in c["stages"].items())))
        if key not in groups:
            groups[key] = []
            order.append(groups[key])
        groups[key].append(c)
    out = []
    for g in order:
        g.sort(key=lambda c: (c["source"].get("type") != "공식", c["collectedAt"] or "", c["url"]))
        rep = g[0]
        if len(g) > 1:
            rep["alsoReportedBy"] = [{"url": c["url"], "title": c["title"], "source": c["source"]["name"], "pubDate": c["pubDate"]} for c in g[1:]]
        out.append(rep)
    return out


def export_inbox(store: Store, cfg: dict, path: Path) -> int:
    srcs = {s["id"]: s for s in cfg["sources"]}
    since = (date.today() - timedelta(days=cfg["lookback_days"])).isoformat()
    cands = []
    for d in store.all_documents_with_extractions():
        if d["pub_date"] and d["pub_date"] < since:
            continue
        stages = store.extractions(d["url"])
        s = srcs.get(d["source_id"], {"id": d["source_id"], "name": d["source_id"], "type": "공식"})
        cands.append({
            "id": candidate_id(d["url"], d["cname"], stages),
            "url": d["url"], "title": d["title"], "pubDate": d["pub_date"],
            "source": {"id": s["id"], "name": s["name"], "type": s.get("type", "공식")},
            "collectedAt": d["first_seen"], "lastCheckedAt": d["last_checked"],
            "cname": d["cname"], "matchedBy": d["matched_by"], "entities": json.loads(d["entities"] or "[]"),
            "year": d["year"], "half": d["half"], "kind": d["kind"],
            "stages": {k: {"s": v["s"], "e": v["e"], "p": v["p"], "raw": v["raw"]} for k, v in stages.items()},
            "changes": store.history(d["url"]),
            "status": "자동 수집",
        })
    cands = merge_duplicates(cands)
    out = {"schema": "rci-inbox/1", "generatedAt": now(), "collector": VERSION, "sources": store.statuses(), "candidates": cands}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return len(cands)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="채용 일정 수집기")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--source", help="출처 id (config.json)")
    ap.add_argument("--url", help="공개 페이지 주소 하나를 직접 읽기")
    ap.add_argument("--file", help="저장한 HTML 파일(--url과 함께)")
    ap.add_argument("--pub", help="게시일 YYYY-MM-DD (--url/--file에서 날짜를 못 찾을 때)")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--probe", nargs="?", const="probe_sites.json", help="출처 후보 점검(robots.txt·RSS 유무)만 하고 결과를 probe_result.csv로 저장")
    a = ap.parse_args(argv)

    cfg = load_config(Path(a.config))
    base = Path(a.config).resolve().parent
    setup_logging(base / cfg.get("log_dir", "logs"))
    (base / "data").mkdir(exist_ok=True)
    store = Store(str(base / cfg.get("db", "data/collector.sqlite")))
    pool = load_pool(str(base / cfg["pool_file"]) if cfg.get("pool_file") else None)
    fetcher = Fetcher(cfg["user_agent"], cfg["delay_sec"], cfg["timeout_sec"], cfg.get("ca_bundle") or None, cfg["respect_robots"])
    if a.probe:
        from rci.probe import run_probe
        sites = json.loads((base / a.probe).read_text(encoding="utf-8"))["sites"]
        out = base / "probe_result.csv"
        res = run_probe(fetcher, sites, out, log)
        log.info("점검 끝: 추가 가능 %d곳 / 전체 %d곳 → %s", sum(1 for r in res if r["verdict"] == "추가 가능"), len(res), out.name)
        store.close()
        return 0
    started = now()
    summary: dict = {}
    code = 0
    try:
        if a.url:
            src = next((s for s in cfg["sources"] if s["id"] == a.source), None) or {"id": a.source or "manual", "name": "직접 지정 페이지", "type": "공식", "default_company": None}
            try:
                html = Path(a.file).read_text(encoding="utf-8", errors="replace") if a.file else fetcher.get_text(a.url)
            except FetchError as e:
                log.error("페이지를 읽지 못함: %s", e)
                return 2
            page = html_to_text(html)
            pub = date.fromisoformat(a.pub) if a.pub else (date.fromisoformat(page["published"][:10]) if re.match(r"\d{4}-\d{2}-\d{2}", page["published"] or "") else date.today())
            r = process_page(store, pool, src, a.url, page["title"], page["text"], pub)
            summary = {"manual": a.url, "result": r}
        elif not a.export_only:
            for src in cfg["sources"]:
                if not src.get("enabled", True) or (a.source and src["id"] != a.source):
                    continue
                log.info("== %s", src["name"])
                st = collect_source(store, fetcher, pool, src, cfg)
                summary[src["id"]] = st
                log.info("== %s: 대상 %d · 일정 있음 %d · 신규 %d · 변경 %d · 오류 %d", src["name"], st["items"], st["kept"], st["new"], st["changed"], len(st["errors"]))
                if st["errors"] and not st["kept"]:
                    code = 1
        n = export_inbox(store, cfg, base / cfg.get("inbox_file", "inbox.json"))
        log.info("inbox.json 작성: 후보 %d건", n)
        store.log_run(started, summary)
    finally:
        store.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
