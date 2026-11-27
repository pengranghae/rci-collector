"""SQLite 저장소 — 가져온 문서, 추출한 일정, 변경 이력, 출처별 상태."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  url TEXT PRIMARY KEY, source_id TEXT, title TEXT, pub_date TEXT,
  content_hash TEXT, first_seen TEXT, last_checked TEXT, cname TEXT, matched_by TEXT,
  entities TEXT, year INTEGER, half TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS extractions (
  url TEXT, stage TEXT, s TEXT, e TEXT, p TEXT, raw TEXT, first_seen TEXT, last_seen TEXT,
  PRIMARY KEY (url, stage)
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, stage TEXT, old TEXT, new TEXT, changed_at TEXT
);
CREATE TABLE IF NOT EXISTS source_status (
  source_id TEXT PRIMARY KEY, name TEXT, last_run TEXT, last_success TEXT,
  consecutive_failures INTEGER DEFAULT 0, last_error TEXT, last_items INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checked (
  url TEXT PRIMARY KEY, source_id TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, summary TEXT
);
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.commit()
        self.db.close()

    # ---- 문서 ----
    def upsert_document(self, url: str, source_id: str, title: str, pub: str, h: str, info: dict) -> bool:
        """새 문서면 True."""
        t = now()
        row = self.db.execute("SELECT url FROM documents WHERE url=?", (url,)).fetchone()
        vals = (source_id, title, pub, h, t, info.get("cname"), info.get("matchedBy"), json.dumps(info.get("entities", []), ensure_ascii=False), info.get("year"), info.get("half"), info.get("kind"))
        if row:
            self.db.execute("UPDATE documents SET source_id=?, title=?, pub_date=?, content_hash=?, last_checked=?, cname=?, matched_by=?, entities=?, year=?, half=?, kind=? WHERE url=?", (*vals, url))
            return False
        self.db.execute("INSERT INTO documents (source_id,title,pub_date,content_hash,last_checked,cname,matched_by,entities,year,half,kind,url,first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*vals, url, t))
        return True

    def document(self, url: str):
        return self.db.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()

    # ---- 추출값과 변경 감지 ----
    def merge_extractions(self, url: str, stages: dict) -> list[dict]:
        """이번 추출값을 저장하고, 이전과 달라진 전형 목록을 돌려준다."""
        t = now()
        changes = []
        old = {r["stage"]: r for r in self.db.execute("SELECT * FROM extractions WHERE url=?", (url,))}
        for k, v in stages.items():
            o = old.get(k)
            if o is None:
                self.db.execute("INSERT INTO extractions VALUES (?,?,?,?,?,?,?,?)", (url, k, v["s"], v["e"], v["p"], v["raw"], t, t))
            elif (o["s"], o["e"], o["p"]) != (v["s"], v["e"], v["p"]):
                ov = {"s": o["s"], "e": o["e"], "p": o["p"]}
                nv = {"s": v["s"], "e": v["e"], "p": v["p"]}
                self.db.execute("INSERT INTO history (url,stage,old,new,changed_at) VALUES (?,?,?,?,?)", (url, k, json.dumps(ov), json.dumps(nv), t))
                self.db.execute("UPDATE extractions SET s=?, e=?, p=?, raw=?, last_seen=? WHERE url=? AND stage=?", (v["s"], v["e"], v["p"], v["raw"], t, url, k))
                changes.append({"stage": k, "old": ov, "new": nv, "changedAt": t})
            else:
                self.db.execute("UPDATE extractions SET last_seen=?, raw=? WHERE url=? AND stage=?", (t, v["raw"], url, k))
        return changes

    def extractions(self, url: str) -> dict:
        return {r["stage"]: {"s": r["s"], "e": r["e"], "p": r["p"], "raw": r["raw"], "firstSeen": r["first_seen"], "lastSeen": r["last_seen"]}
                for r in self.db.execute("SELECT * FROM extractions WHERE url=?", (url,))}

    def history(self, url: str) -> list[dict]:
        return [{"stage": r["stage"], "old": json.loads(r["old"]), "new": json.loads(r["new"]), "changedAt": r["changed_at"]}
                for r in self.db.execute("SELECT * FROM history WHERE url=? ORDER BY id", (url,))]

    def all_documents_with_extractions(self):
        return self.db.execute("SELECT d.* FROM documents d WHERE EXISTS (SELECT 1 FROM extractions x WHERE x.url=d.url) ORDER BY d.pub_date DESC").fetchall()

    # ---- 이미 읽은 기사(일정 유무와 상관없이) ----
    def was_checked(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM checked WHERE url=?", (url,)).fetchone() is not None

    def mark_checked(self, url: str, source_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO checked (url, source_id, checked_at) VALUES (?,?,?)", (url, source_id, now()))

    # ---- 출처 상태 ----
    def source_result(self, source_id: str, name: str, ok: bool, error: str | None, items: int):
        t = now()
        row = self.db.execute("SELECT * FROM source_status WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            self.db.execute("INSERT INTO source_status (source_id,name,consecutive_failures) VALUES (?,?,0)", (source_id, name))
        if ok:
            self.db.execute("UPDATE source_status SET name=?, last_run=?, last_success=?, consecutive_failures=0, last_error=NULL, last_items=? WHERE source_id=?", (name, t, t, items, source_id))
        else:
            self.db.execute("UPDATE source_status SET name=?, last_run=?, consecutive_failures=consecutive_failures+1, last_error=?, last_items=? WHERE source_id=?", (name, t, error, items, source_id))

    def statuses(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM source_status ORDER BY source_id")]

    def log_run(self, started: str, summary: dict):
        self.db.execute("INSERT INTO runs (started_at, finished_at, summary) VALUES (?,?,?)", (started, now(), json.dumps(summary, ensure_ascii=False)))
