"""게시물 본문에서 채용 전형 일정을 찾아 날짜로 정규화한다.

입력: 제목, 본문(평문), 게시일(date)
출력: 전형별 일정 목록 [{stage, s, e, p, raw}]
  s, e : 'YYYY-MM-DD' (시작·종료)
  p    : 정밀도 d(일) w(주) x(순: 초·중순·말) m(월)
  raw  : 근거가 된 원문 문장(최대 200자)

연도·월이 생략된 표현('24~25일', '다음 달 20일부터')은 게시일을 기준으로 채운다.
요일이 함께 적혀 있으면('8일(화)') 요일이 맞는 달을 고른다.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

WD = "월화수목금토일"  # date.weekday(): 월=0

# 전형 키워드 → 단계. 앞에 있는 것이 우선(더 구체적인 것부터).
STAGE_KEYWORDS = [
    ("int2", r"2차\s*면접|최종\s*면접|임원\s*면접|종합\s*면접|경영진\s*면접|인성\s*면접"),
    ("int1", r"1차\s*면접|실무\s*면접|직무\s*면접|역량\s*면접|하프\s*데이|Half[- ]?Day|원데이\s*면접|면접"),
    ("test", r"GSAT|SKCT|HMAT|PAT\b|L-TAB|DCAT|Way\s*Fit|인적성|직무적성|적성검사|인성검사|역량검사|필기|코딩\s*테스트"),
    ("fin", r"최종\s*합격|합격자\s*발표|최종\s*발표"),
    ("join", r"최종\s*입사|입사\s*(?:일|예정|시기|한다|하게|해|순)"),
    ("doc", r"(?:수시|신입|정기)\s*채용|채용\s*시작|서류\s*접수|지원서\s*접수|접수|지원서|원서|모집|공채\s*절차|채용\s*절차|지원\s*기간"),
]
IGNORE_SENTENCE = re.compile(r"설명회|박람회|해커톤|공모전|간담회|시상식|행사")
RECRUIT_HINT = re.compile(r"채용|공채|신입|GSAT|SKCT|인적성|지원서|입사")


def _eom(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"[ \t ]+", " ", text)
    parts = re.split(r"(?<=[다요음함])\.\s+|\n+|(?<=[.!?])\s+(?=[가-힣A-Z▲△■●-])", text)
    return [p.strip() for p in parts if p and len(p.strip()) > 4]


class DateFinder:
    """문장 하나 안의 날짜 표현을 찾는다."""

    def __init__(self, pub: date):
        self.pub = pub

    def _year_for_month(self, m: int, explicit_year: int | None) -> int:
        if explicit_year:
            return explicit_year
        y = self.pub.year
        # 게시일보다 6개월 넘게 앞서면 다음 해로 본다(예: 12월 게시글의 '1월 입사')
        if m < self.pub.month - 6:
            return y + 1
        if m > self.pub.month + 6:
            return y - 1
        return y

    def _month_for_day(self, day: int, base_month: int | None, rel: str | None, weekday: str | None) -> tuple[int, int] | None:
        """월이 생략된 'D일'의 연·월을 정한다."""
        y, m = self.pub.year, self.pub.month
        if base_month:
            y, m = self._year_for_month(base_month, None), base_month
        elif rel in ("다음 달", "내달", "다음달"):
            m += 1
        elif rel in ("지난달", "지난 달"):
            m -= 1
        elif rel == "지난" and day > self.pub.day:
            m -= 1
        if m > 12:
            y, m = y + 1, m - 12
        if m < 1:
            y, m = y - 1, m + 12
        cands = [(y, m)]
        if weekday and not base_month and not rel:
            # 요일이 맞지 않으면 앞뒤 달도 본다
            for dm in (1, -1):
                mm, yy = m + dm, y
                if mm > 12:
                    yy, mm = yy + 1, 1
                if mm < 1:
                    yy, mm = yy - 1, 12
                cands.append((yy, mm))
            for yy, mm in cands:
                dt = _mk(yy, mm, day)
                if dt and WD[dt.weekday()] == weekday:
                    return yy, mm
        dt = _mk(y, m, day)
        return (y, m) if dt else None

    def find(self, s: str) -> list[dict]:
        out: list[dict] = []
        used: list[tuple[int, int]] = []

        def free(a: int, b: int) -> bool:
            return all(b <= u0 or a >= u1 for u0, u1 in used)

        def add(m: re.Match, s_: date | None, e_: date | None, p: str):
            if not s_ or not e_ or e_ < s_ or not free(m.start(), m.end()):
                return
            used.append((m.start(), m.end()))
            out.append({"pos": m.start(), "end": m.end(), "s": s_, "e": e_, "p": p, "text": m.group(0)})

        wdp = r"(?:\s*\(([월화수목금토일])\))?"

        def yr(pos: int) -> int | None:  # 연도는 바로 앞에 'YYYY년'이 붙은 날짜에만 적용
            mm = re.search(r"(20\d\d)\s*년\s*$", s[max(0, pos - 10):pos])
            return int(mm.group(1)) if mm else None

        # 0) 'D일(요)부터 M월 D일까지' (월이 끝에만 있는 범위)
        for m in re.finditer(r"(?<![\d월])(\d{1,2})일" + wdp + r"\s*(?:부터|~|∼|-|–)\s*(\d{1,2})월\s*(\d{1,2})일", s):
            d1, mo2, d2 = int(m.group(1)), int(m.group(3)), int(m.group(4))
            y2 = self._year_for_month(mo2, None)
            mo1, y1 = (mo2, y2) if d1 <= d2 else ((mo2 - 1) or 12, y2 - (1 if mo2 == 1 else 0))
            add(m, _mk(y1, mo1, d1), _mk(y2, mo2, d2), "d")

        # 1) 'M월 D일(요) ~ M월 D일' / 'M월 D일 ~ D일' / 'M월 D~D일'
        for m in re.finditer(r"(\d{1,2})월\s*(\d{1,2})일" + wdp + r"\s*(?:부터|~|∼|-|–)\s*(?:(\d{1,2})월\s*)?(\d{1,2})일", s):
            mo1, d1 = int(m.group(1)), int(m.group(2))
            mo2 = int(m.group(4)) if m.group(4) else mo1
            y1 = self._year_for_month(mo1, yr(m.start()))
            y2 = y1 + (1 if mo2 < mo1 else 0)
            add(m, _mk(y1, mo1, d1), _mk(y2, mo2, int(m.group(5))), "d")
        for m in re.finditer(r"(\d{1,2})월\s*(\d{1,2})\s*(?:~|∼|-|–)\s*(\d{1,2})일", s):
            mo, y = int(m.group(1)), self._year_for_month(int(m.group(1)), yr(m.start()))
            add(m, _mk(y, mo, int(m.group(2))), _mk(y, mo, int(m.group(3))), "d")
        # 2) 'M.D ~ M.D', 'M/D ~ M/D', 'M/D~D'
        for m in re.finditer(r"(?<![\d.])(\d{1,2})[./](\d{1,2})\.?" + wdp + r"\s*(?:~|∼|-|–)\s*(?:(\d{1,2})[./])?(\d{1,2})(?![\d.]*\d{3})", s):
            mo1 = int(m.group(1))
            if not 1 <= mo1 <= 12:
                continue
            mo2 = int(m.group(4)) if m.group(4) else mo1
            y1 = self._year_for_month(mo1, yr(m.start()))
            add(m, _mk(y1, mo1, int(m.group(2))), _mk(y1 + (1 if mo2 < mo1 else 0), mo2, int(m.group(5))), "d")
        # 3) 단일 'M월 D일'
        for m in re.finditer(r"(\d{1,2})월\s*(\d{1,2})일" + wdp, s):
            mo = int(m.group(1))
            dt = _mk(self._year_for_month(mo, yr(m.start())), mo, int(m.group(2)))
            add(m, dt, dt, "d")
        # 4) 단일 'M/D', 'M.D(요)'
        for m in re.finditer(r"(?<![\d.])(\d{1,2})[./](\d{1,2})" + wdp + r"(?![\d.]*\d)", s):
            mo = int(m.group(1))
            if not 1 <= mo <= 12:
                continue
            dt = _mk(self._year_for_month(mo, yr(m.start())), mo, int(m.group(2)))
            add(m, dt, dt, "d")
        # 5) 월 없는 'D일(요)부터 D일까지', 'D~D일', 'D일(요)'
        relp = r"(?:(다음\s?달|내달|지난\s?달|지난|이달|이번\s?달)\s*)?"
        for m in re.finditer(relp + r"(\d{1,2})일" + wdp + r"\s*(?:부터|~|∼|-|–)\s*(\d{1,2})일", s):
            rel = (m.group(1) or "").replace(" ", "") or None
            rel = {"다음달": "다음 달", "지난달": "지난달"}.get(rel, rel)
            ym = self._month_for_day(int(m.group(2)), None, rel, m.group(3))
            if ym:
                y, mo = ym
                d2 = int(m.group(4))
                e_ = _mk(y, mo, d2) if d2 >= int(m.group(2)) else _mk(y + (mo == 12), mo % 12 + 1, d2)
                add(m, _mk(y, mo, int(m.group(2))), e_, "d")
        for m in re.finditer(relp + r"(\d{1,2})\s*(?:~|∼|-|–)\s*(\d{1,2})일", s):
            rel = (m.group(1) or "").replace(" ", "") or None
            ym = self._month_for_day(int(m.group(2)), None, {"다음달": "다음 달"}.get(rel, rel), None)
            if ym:
                y, mo = ym
                add(m, _mk(y, mo, int(m.group(2))), _mk(y, mo, int(m.group(3))), "d")
        for m in re.finditer(relp + r"(?<![\d월])(\d{1,2})일" + wdp, s):
            rel = (m.group(1) or "").replace(" ", "") or None
            day = int(m.group(2))
            if day < 1 or day > 31:
                continue
            ym = self._month_for_day(day, None, {"다음달": "다음 달"}.get(rel, rel), m.group(3))
            if ym:
                dt = _mk(ym[0], ym[1], day)
                add(m, dt, dt, "d")
        # 6) 월 단위 'M월 초/중순/말/중', 'M~N월'
        for m in re.finditer(r"(\d{1,2})\s*~\s*(\d{1,2})월", s):
            a, b = int(m.group(1)), int(m.group(2))
            if 1 <= a <= 12 and 1 <= b <= 12:
                ya = self._year_for_month(a, yr(m.start()))
                yb = ya + (1 if b < a else 0)
                add(m, _mk(ya, a, 1), _mk(yb, b, _eom(yb, b)), "m")
        for m in re.finditer(r"(\d{1,2})월\s*(초순|초|중순|하순|말|중)?(?!\s*\d)", s):
            mo = int(m.group(1))
            if not 1 <= mo <= 12:
                continue
            y, q = self._year_for_month(mo, yr(m.start())), m.group(2) or ""
            if q in ("초", "초순"):
                add(m, _mk(y, mo, 1), _mk(y, mo, 10), "x")
            elif q == "중순":
                add(m, _mk(y, mo, 11), _mk(y, mo, 20), "x")
            elif q in ("말", "하순"):
                add(m, _mk(y, mo, 21), _mk(y, mo, _eom(y, mo)), "x")
            else:
                add(m, _mk(y, mo, 1), _mk(y, mo, _eom(y, mo)), "m")
        out.sort(key=lambda x: x["pos"])
        return out


def _stage_positions(s: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for stage, pat in STAGE_KEYWORDS:
        for m in re.finditer(pat, s, flags=re.I):
            if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), stage))
    return sorted(hits)


PREC_RANK = {"d": 4, "w": 3, "x": 2, "m": 1}


def extract_schedule(title: str, text: str, pub: date) -> dict:
    """게시물 하나에서 연도·반기·유형·전형 일정을 뽑는다."""
    full = (title or "") + "\n" + (text or "")
    year, half = pub.year, ("하" if pub.month >= 7 else "상")
    m = re.search(r"(20\d\d)\s*년\s*(상|하)반기", full) or re.search(r"(20\d\d)\s*(상|하)반기", full)
    if m:
        year, half = int(m.group(1)), m.group(2)
    else:
        m2 = re.search(r"(상|하)반기", full)
        if m2:
            half = m2.group(1)
    if re.search(r"인턴", title or "") and not re.search(r"신입|공채", title or ""):
        kind = "인턴"
    elif re.search(r"산학|장학", title or ""):
        kind = "산학"
    else:
        kind = "신입"

    finder = DateFinder(pub)
    stages: dict[str, dict] = {}
    for sent in split_sentences(full):
        if IGNORE_SENTENCE.search(sent) and not re.search(r"서류\s*접수|지원서\s*접수", sent):
            continue
        if not RECRUIT_HINT.search(sent) and not re.search(r"면접|접수", sent):
            continue
        dates = finder.find(sent)
        if not dates:
            continue
        kws = _stage_positions(sent)
        if not kws:
            continue
        for dt in dates:
            # 기사 첫머리의 발표일('30일 SK하이닉스는 …')은 일정이 아니다
            if dt["pos"] == 0 and re.match(r"\d{1,2}일\s", sent) and dt["s"] == dt["e"]:
                continue
            # 날짜와 가장 가까운 전형 키워드(뒤에 오는 키워드는 같은 절 안일 때만)
            best, bestd = None, 10**9
            for pos, stage in kws:
                dist = dt["pos"] - pos if pos <= dt["pos"] else (pos - dt["end"]) * 1.5
                if dist < bestd:
                    best, bestd = stage, dist
            # 문장 안 전형 키워드가 한 종류뿐이면 거리 제한을 넓힌다
            # (예: '8일부터 15일까지 … 홈페이지에서 … 지원서를 접수')
            limit = 150 if len({st for _, st in kws}) == 1 else 60
            if best is None or bestd > limit:
                continue
            stage = best
            sd, ed, p = dt["s"], dt["e"], dt["p"]
            raw = sent[:200]
            if stage == "doc":
                # 접수 기간(범위)이면 시작=공고 시작, 끝=서류 마감
                if ed > sd:
                    _put(stages, "post", sd, sd, p, raw)
                    _put(stages, "doc", ed, ed, p, raw)
                elif (re.search(re.escape(dt["text"]) + r"\s*(?:부터|에)", sent) and re.search(r"진행|시작|실시|접수", sent)) or re.search(re.escape(dt["text"]) + r"[^.…]{0,15}시작", sent):
                    _put(stages, "post", sd, sd, p, raw)
                else:
                    _put(stages, "doc", sd, ed, p, raw)
            else:
                _put(stages, stage, sd, ed, p, raw)
    return {"year": year, "half": half, "kind": kind, "stages": stages}


def _put(stages: dict, k: str, s: date, e: date, p: str, raw: str) -> None:
    cur = stages.get(k)
    if cur and PREC_RANK[cur["p"]] >= PREC_RANK[p]:
        return
    stages[k] = {"s": s.isoformat(), "e": e.isoformat(), "p": p, "raw": raw}
