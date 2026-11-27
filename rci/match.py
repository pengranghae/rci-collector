"""기업명 정규화와 비교기업 Pool 매칭.

Pool은 HTML의 「수집기용 Pool 내보내기」로 만든 pool.json을 쓴다.
없으면 아래 기본 목록을 쓴다.
pool.json 형식: [{"name": "삼성그룹", "group": "삼성", "aliases": ["삼성그룹", "삼성"]}, ...]
별칭 끝의 *는 '그 글자로 시작하는 이름 전체'를 뜻한다(HTML과 같은 규칙).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_POOL = [
    {"name": "삼성그룹", "group": "삼성", "aliases": ["삼성그룹", "삼성"], "members": ["삼성전자", "삼성디스플레이", "삼성SDI", "삼성전기", "삼성SDS", "삼성바이오로직스", "삼성물산", "삼성중공업"]},
    {"name": "SK하이닉스", "group": "SK", "aliases": ["SK하이닉스", "SK hynix"]},
    {"name": "현대자동차", "group": "현대자동차그룹", "aliases": ["현대자동차", "현대차"]},
    {"name": "기아", "group": "현대자동차그룹", "aliases": ["기아"]},
    {"name": "LG전자", "group": "LG", "aliases": ["LG전자"]},
    {"name": "현대모비스", "group": "현대자동차그룹", "aliases": ["현대모비스"]},
    {"name": "포스코그룹", "group": "포스코", "aliases": ["포스코그룹", "포스코", "POSCO"]},
    {"name": "한화에어로스페이스", "group": "한화", "aliases": ["한화에어로스페이스", "한화에어로"]},
    {"name": "HD현대", "group": "HD현대", "aliases": ["HD현대*"]},
]

# 표기 흔들림 보정(정규화 후 비교)
CANON = {"posco": "포스코", "skhynix": "SK하이닉스", "lg": "LG", "sk": "SK"}


def norm(name: str) -> str:
    n = re.sub(r"\(주\)|㈜|주식회사|\s+", "", name or "")
    n = re.sub(r"\([^)]*\)", "", n)
    low = n.lower()
    return CANON.get(low, n)


def load_pool(path: str | None) -> list[dict]:
    if path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data.get("companies", data) if isinstance(data, dict) else data
        builtin = {c["name"]: c for c in DEFAULT_POOL}
        out = []
        for c in items:
            base = builtin.get(c["name"], {})
            al = [c["name"], *(c.get("aliases") or []), *base.get("aliases", [])]
            out.append({"name": c["name"], "group": c.get("group", "") or base.get("group", ""), "tier": c.get("tier", ""), "aliases": list(dict.fromkeys(al)),
                        "members": list(dict.fromkeys([*(c.get("members") or []), *base.get("members", [])]))})
        return out
    return DEFAULT_POOL


def _alias_hit(alias: str, text: str) -> int:
    """text 안에서 alias가 처음 나오는 위치(없으면 -1). 한글 단어 경계를 고려한다."""
    a = alias.rstrip("*")
    if not a:
        return -1
    if alias.endswith("*"):
        m = re.search(re.escape(a), text)
        return m.start() if m else -1
    # '삼성'이 '삼성전자'의 일부로 잡히지 않게: 뒤에 한글/영문이 이어지면 다른 단어로 본다
    for m in re.finditer(re.escape(a), text):
        nxt = text[m.end():m.end() + 1]
        if a in ("삼성", "포스코", "LG", "SK") and nxt and re.match(r"[가-힣A-Za-z]", nxt) and nxt not in "은는이가의,·과와도":
            continue
        return m.start()
    return -1


def match_company(title: str, text: str, pool: list[dict], default: str | None) -> dict:
    """제목에서 먼저 찾고, 없으면 출처 기본 기업. 본문에 나오는 계열사명은 entities로 돌려준다."""
    best = None
    for c in pool:
        # 별칭이 계열사 목록보다 우선(삼성전자가 Pool에 법인으로 있으면 그쪽으로)
        for al, bonus in [*((a, 0.5) for a in c["aliases"]), *((m, 0.0) for m in c.get("members", []))]:
            pos = _alias_hit(al, title or "")
            score = len(al.rstrip("*")) + bonus
            if pos >= 0 and (best is None or pos < best[0] or (pos == best[0] and score > best[2])):
                best = (pos, c, score)
    if best:
        c, by = best[1], "제목"
    elif default:
        c = next((x for x in pool if x["name"] == default), {"name": default, "group": "", "aliases": [default], "members": []})
        by = "출처 기본값"
    else:
        return {"cname": None, "matchedBy": "매칭 실패", "entities": []}
    ents = []
    body = (title or "") + " " + (text or "")
    for mem in c.get("members", []):
        if mem in body:
            ents.append(mem)
    # '모집회사 : A, B, C' 형태가 있으면 그대로 기록
    m = re.search(r"(?:모집\s*회사|참여\s*(?:회사|계열사|관계사))\s*[:은는]?\s*([^\n.]{4,300})", body)
    if m:
        for part in re.split(r"[,·、/]", m.group(1)):
            p = part.strip()
            if 2 <= len(p) <= 20 and p not in ents:
                ents.append(p)
    return {"cname": c["name"], "matchedBy": by, "entities": ents[:30]}
