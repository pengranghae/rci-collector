"""로컬 가짜 서버로 수집기 전체 흐름을 시험한다(외부 접속 없음).

  python -m unittest discover -s tests -v
"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402
from rci.extract import extract_schedule  # noqa: E402
from rci.match import DEFAULT_POOL, match_company  # noqa: E402


class Site:
    gsat_version = "v1"
    hits: list = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        Site.hits.append(self.path)
        base = f"http://127.0.0.1:{self.server.server_port}"
        routes = {
            "/robots.txt": ("robots.txt", "text/plain"),
            "/kr/feed": ("samsung_feed.xml", "application/rss+xml"),
            "/hynix/feed": ("hynix_feed.xml", "application/rss+xml"),
            "/kr/2026-h2-gsat": (f"gsat_{Site.gsat_version}.html", "text/html; charset=utf-8"),
            "/news/": ("probe_home.html", "text/html; charset=utf-8"),
            "/news/rss.xml": ("samsung_feed.xml", "application/rss+xml"),
            "/plain/": ("probe_plain.html", "text/html; charset=utf-8"),
        }
        r = routes.get(self.path.split("?")[0])
        if not r:
            self.send_response(404)
            self.end_headers()
            return
        body = (FX / r[0]).read_text(encoding="utf-8").replace("{BASE}", base).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", r[1])
        self.end_headers()
        self.wfile.write(body)


class CollectorFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.dir = Path(tempfile.mkdtemp())
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        cfg.update({"delay_sec": 0, "lookback_days": 100000, "pool_file": ""})
        cfg["sources"] = cfg["sources"][:2]  # 외부 접속 없이: 삼성·하이닉스 두 출처만 가짜 서버로
        cfg["sources"][0]["feeds"] = [base + "/kr/feed"]
        cfg["sources"][1]["feeds"] = [base + "/hynix/feed"]
        cfg["sources"].append({"id": "broken", "name": "고장 난 출처", "type": "공식", "feeds": [base + "/nothing/feed"], "keywords": ["채용"]})
        (cls.dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        cls.cfg = str(cls.dir / "config.json")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def inbox(self):
        return json.loads((self.dir / "inbox.json").read_text(encoding="utf-8"))

    def test_flow(self):
        # 1차 수집
        Site.gsat_version = "v1"
        collect.main(["--config", self.cfg])
        ib = self.inbox()
        by = {c["title"]: c for c in ib["candidates"]}
        self.assertEqual(len(ib["candidates"]), 3, list(by))
        rec = by["삼성, 2026년 하반기 공채 실시"]
        self.assertEqual(rec["cname"], "삼성그룹")
        self.assertEqual((rec["stages"]["post"]["s"], rec["stages"]["doc"]["s"]), ("2026-09-08", "2026-09-15"))
        self.assertEqual(rec["stages"]["test"]["p"], "m")
        self.assertIn("삼성전자", rec["entities"])
        self.assertTrue(rec["url"].endswith("/kr/2026-h2-recruit"))
        self.assertTrue(rec["collectedAt"] and rec["lastCheckedAt"])
        gsat = by["삼성, 2026년 하반기 삼성직무적성검사(GSAT) 실시"]
        self.assertEqual((gsat["stages"]["test"]["s"], gsat["stages"]["test"]["e"]), ("2026-10-24", "2026-10-25"))
        self.assertEqual(gsat["changes"], [])
        hy = by["SK하이닉스, AI 시대 맞춤형 채용…‘반나절 심층면접’ 도입"]
        self.assertEqual(hy["cname"], "SK하이닉스")
        self.assertEqual((hy["stages"]["post"]["s"], hy["stages"]["doc"]["s"], hy["stages"]["join"]["s"]), ("2026-08-20", "2026-08-26", "2027-01-04"))
        # 비채용 기사는 거르고, robots 금지 경로는 요청하지 않음
        self.assertNotIn("/kr/galaxy", Site.hits)
        self.assertNotIn("/private/notice", Site.hits)
        st = {s["source_id"]: s for s in ib["sources"]}
        self.assertEqual(st["samsung_newsroom"]["consecutive_failures"], 0)
        self.assertEqual(st["broken"]["consecutive_failures"], 1)
        self.assertIn("404", st["broken"]["last_error"])
        first_id = gsat["id"]

        # 2차 수집: 같은 페이지의 날짜가 바뀜 → 변경 감지
        Site.gsat_version = "v2"
        collect.main(["--config", self.cfg])
        ib2 = self.inbox()
        g2 = next(c for c in ib2["candidates"] if "GSAT" in c["title"])
        self.assertEqual((g2["stages"]["test"]["s"], g2["stages"]["test"]["e"]), ("2026-10-31", "2026-11-01"))
        self.assertEqual(len(g2["changes"]), 1)
        ch = g2["changes"][0]
        self.assertEqual((ch["stage"], ch["old"]["s"], ch["new"]["s"]), ("test", "2026-10-24", "2026-10-31"))
        self.assertNotEqual(g2["id"], first_id)  # 내용이 바뀌면 후보 id도 바뀐다
        self.assertEqual({c["id"] for c in ib2["candidates"] if "GSAT" not in c["title"]},
                         {c["id"] for c in ib["candidates"] if "GSAT" not in c["title"]})
        st2 = {s["source_id"]: s for s in ib2["sources"]}
        self.assertEqual(st2["broken"]["consecutive_failures"], 2)

    def test_manual_file(self):
        # 저장한 페이지 파일 읽기(접속 없음)
        collect.main(["--config", self.cfg, "--source", "samsung_newsroom", "--url", "https://example.invalid/gsat", "--file", str(FX / "gsat_v1.html")])
        c = next(c for c in self.inbox()["candidates"] if c["url"] == "https://example.invalid/gsat")
        self.assertEqual(c["stages"]["test"]["s"], "2026-10-24")


class Extraction(unittest.TestCase):
    def ex(self, text, pub, title="채용 공고"):
        return {k: (v["s"], v["e"], v["p"]) for k, v in extract_schedule(title, text, pub)["stages"].items()}

    def test_phrases(self):
        self.assertEqual(self.ex("지원서 접수는 9.8(화)~9.15(화) 진행한다.", date(2026, 9, 1))["doc"], ("2026-09-15", "2026-09-15", "d"))
        self.assertEqual(self.ex("인적성검사(PAT)는 10월 10일(토) 온라인으로 실시한다.", date(2026, 9, 1))["test"], ("2026-10-10", "2026-10-10", "d"))
        self.assertEqual(self.ex("1차 면접은 10월 중, 2차 면접은 11월 말 진행한다.", date(2026, 9, 1))["int2"], ("2026-11-21", "2026-11-30", "x"))
        self.assertEqual(self.ex("최종 합격자 발표는 12월 11일이며 입사는 2027년 1월 예정이다.", date(2026, 11, 1))["fin"], ("2026-12-11", "2026-12-11", "d"))
        # 설명회 날짜는 일정으로 잡지 않는다
        self.assertEqual(self.ex("지난 13일부터 채용설명회를 열었다.", date(2026, 8, 20)), {})

    def test_match(self):
        self.assertEqual(match_company("포스코, 하반기 신입사원 채용", "", DEFAULT_POOL, None)["cname"], "포스코그룹")
        self.assertEqual(match_company("HD현대중공업 신입 채용", "", DEFAULT_POOL, None)["cname"], "HD현대")
        self.assertEqual(match_company("삼성전자 DS부문 채용설명회", "", DEFAULT_POOL, None)["matchedBy"], "제목")
        self.assertEqual(match_company("하반기 채용 소식", "", DEFAULT_POOL, None)["matchedBy"], "매칭 실패")


if __name__ == "__main__":
    unittest.main()


class NewsH(BaseHTTPRequestHandler):
    hits: list = []
    api_headers: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        NewsH.hits.append(self.path)
        base = f"http://127.0.0.1:{self.server.server_port}"
        p = self.path.split("?")[0]
        if p == "/v1/search/news.json":
            NewsH.api_headers.append(self.headers.get("X-Naver-Client-Id"))
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query).get("query", [""])[0]
            items = [{"title": "<b>기아</b>, 하반기 신입사원 공개채용", "originallink": base + "/news/n1", "link": "https://n.news.naver.com/x",
                      "description": "<b>기아</b>가 신입 채용을 실시한다", "pubDate": "Wed, 02 Sep 2026 09:00:00 +0900"}] if "기아" in q else []
            body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        else:
            routes = {"/robots.txt": "robots.txt", "/feed/a": "news_feed_a.xml", "/feed/b": "news_feed_b.xml",
                      "/news/a1": "news_article_hyundai.html", "/news/b1": "news_article_hyundai.html", "/news/n1": "news_article_kia.html"}
            if p not in routes:
                self.send_response(404)
                self.end_headers()
                return
            body = (FX / routes[p]).read_text(encoding="utf-8").replace("{BASE}", base).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)


class NewsFlow(unittest.TestCase):
    """언론사 피드·네이버 검색 API·중복 보도 합치기."""

    def setUp(self):
        import os
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), NewsH)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.srv.server_port}"
        self.dir = Path(tempfile.mkdtemp())
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        cfg.update({"delay_sec": 0, "lookback_days": 100000, "pool_file": ""})
        cfg["sources"] = [
            {"id": "news_a", "name": "언론사 A", "preset": "news", "feeds": [base + "/feed/a"]},
            {"id": "news_b", "name": "언론사 B", "preset": "news", "feeds": [base + "/feed/b"]},
            {"id": "naver_news", "name": "네이버", "preset": "news", "kind": "naver_news", "api_url": base + "/v1/search/news.json", "queries": ["{name} 신입 채용"]},
        ]
        (self.dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.cfg = str(self.dir / "config.json")
        NewsH.hits.clear()
        NewsH.api_headers.clear()
        self._env = {k: os.environ.get(k) for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")}

    def tearDown(self):
        import os
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def inbox(self):
        return json.loads((self.dir / "inbox.json").read_text(encoding="utf-8"))

    def test_news_and_naver(self):
        import os
        os.environ["NAVER_CLIENT_ID"], os.environ["NAVER_CLIENT_SECRET"] = "test-id", "test-secret"
        collect.main(["--config", self.cfg])
        ib = self.inbox()
        by = {c["cname"]: c for c in ib["candidates"]}
        # 두 언론사가 같은 일정을 보도 → 후보 하나, 나머지는 alsoReportedBy
        self.assertEqual(sorted(by), ["기아", "현대자동차"], [c["title"] for c in ib["candidates"]])
        hy = by["현대자동차"]
        self.assertEqual(hy["source"]["type"], "언론")
        self.assertEqual((hy["stages"]["post"]["s"], hy["stages"]["doc"]["s"], hy["stages"]["test"]["s"]), ("2026-09-01", "2026-09-14", "2026-10-17"))
        self.assertEqual(len(hy.get("alsoReportedBy", [])), 1)
        # 기업명이 제목에 없는 기사, 채용과 무관한 기사는 읽지 않는다
        self.assertNotIn("/news/a2", NewsH.hits)
        self.assertNotIn("/news/a3", NewsH.hits)
        # 네이버: 원문 주소(originallink)를 읽고, 키를 헤더로 보낸다
        ki = by["기아"]
        self.assertTrue(ki["url"].endswith("/news/n1"))
        self.assertEqual((ki["stages"]["doc"]["s"], ki["stages"]["test"]["s"]), ("2026-09-18", "2026-10-24"))
        self.assertTrue(NewsH.api_headers and set(NewsH.api_headers) == {"test-id"})
        # 두 번째 실행: 이미 읽은 기사는 다시 요청하지 않고, 후보 id는 그대로
        n_a1 = NewsH.hits.count("/news/a1")
        ids = {c["id"] for c in ib["candidates"]}
        collect.main(["--config", self.cfg])
        self.assertEqual(NewsH.hits.count("/news/a1"), n_a1)
        self.assertEqual({c["id"] for c in self.inbox()["candidates"]}, ids)

    def test_naver_without_key_is_skipped(self):
        import os
        os.environ.pop("NAVER_CLIENT_ID", None)
        os.environ.pop("NAVER_CLIENT_SECRET", None)
        code = collect.main(["--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertFalse(any(h.startswith("/v1/") for h in NewsH.hits))
        self.assertNotIn("naver_news", {s["source_id"] for s in self.inbox()["sources"]})


class Probe(unittest.TestCase):
    def test_probe(self):
        from rci.fetch import Fetcher
        from rci.probe import discover_feeds, run_probe
        self.assertEqual(discover_feeds('<link rel="alternate" type="application/rss+xml" href="/a/feed/"><link rel="alternate" type="application/rss+xml" href="/comments/feed/">', "https://x.com/"), ["https://x.com/a/feed/"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            f = Fetcher("t", 0, 5, None, True)
            d = Path(tempfile.mkdtemp())
            import logging
            res = run_probe(f, [{"name": "a", "url": base + "/news/"}, {"name": "b", "url": base + "/plain/"}, {"name": "c", "url": base + "/private/x"}], d / "p.csv", logging.getLogger("t"))
            self.assertEqual(res[0]["verdict"], "추가 가능")
            self.assertEqual(res[0]["feed"], base + "/news/rss.xml")
            self.assertGreater(res[0]["recruit_items"], 0)
            self.assertTrue(res[1]["verdict"].startswith("RSS 없음"))
            self.assertTrue(res[2]["verdict"].startswith("제외"))
            self.assertIn("판정", (d / "p.csv").read_text(encoding="utf-8-sig"))
            shutil.rmtree(d, ignore_errors=True)
        finally:
            srv.shutdown()
            srv.server_close()
