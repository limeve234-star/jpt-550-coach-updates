#!/usr/bin/env python3
"""JPT 공개 시험일정의 사실정보만 저빈도로 확인해 exam_info.json을 갱신합니다.

- 공식 시험일정 페이지: 하루 1회 요청
- 공식 고사장 페이지: 하루 1회 요청(정적 데이터가 노출될 때만 참고용 추출)
- 파싱 실패 시 기존 JSON을 덮어쓰지 않음
- 문제/해설/이미지/사이트 문구 등 저작물은 수집하지 않음
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

SCHEDULE_URL = "https://www.jpt.co.kr/receipt/examSchList.php"
CENTER_URL = "https://www.jpt.co.kr/receipt/centerMap.php"
OUT = Path(__file__).resolve().parents[1] / "exam_info.json"
KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "AlpsJPTReadingExamInfoBot/1.0 (+public schedule checker; one request/day)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def _get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    if len(r.content) > 2_000_000:
        raise RuntimeError(f"response too large: {len(r.content)}")
    # requests가 EUC-KR/UTF-8을 잘못 추정하는 경우 apparent_encoding으로 보정
    if not r.encoding or r.encoding.lower() in {"iso-8859-1", "ascii"}:
        r.encoding = r.apparent_encoding
    return r.text


def _iso_date(text: str) -> str:
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not m:
        raise ValueError(f"date not found: {text!r}")
    y, mo, d = map(int, m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_schedules(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict] = []
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        row = " | ".join(cells)
        rm = re.search(r"제\s*(\d+)\s*회", row)
        if not rm:
            continue
        # 데스크톱 표의 일반적인 열: 회차 / 시험일시 / 성적발표일시 / 접수기간 / 상태
        date_cells = [c for c in cells if re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", c)]
        if len(date_cells) < 3:
            continue
        exam_display = date_cells[0]
        score_release = date_cells[1]
        reg_cell = next((c for c in cells if "정기접수" in c), date_cells[2] if len(date_cells) > 2 else "")
        regular = ""
        extra = ""
        m = re.search(r"정기접수\s*[:：]?\s*(.*?)(?=특별추가|$)", reg_cell)
        if m:
            regular = _clean(m.group(1))
        m = re.search(r"특별추가\s*[:：]?\s*(.*?)(?=접수하기|접수예정|$)", reg_cell)
        if m:
            extra = _clean(m.group(1))
        # 라벨이 분리된 DOM에 대한 보조 파싱
        if not regular or not extra:
            m = re.search(r"정기접수\s*[:：]?\s*(.*?)(?=특별추가|$)", row)
            if m and not regular:
                regular = _clean(m.group(1).split("|")[0])
            m = re.search(r"특별추가\s*[:：]?\s*(.*?)(?=\||접수하기|접수예정|$)", row)
            if m and not extra:
                extra = _clean(m.group(1))
        result.append({
            "round": f"제{rm.group(1)}회",
            "examDate": _iso_date(exam_display),
            "displayDate": exam_display,
            "regular": regular,
            "extra": extra,
            "scoreRelease": score_release,
        })

    # 모바일/구조 변경 대비: 표 파싱이 실패하면 페이지 텍스트에서 최소 정보만 회수
    if not result:
        text = _clean(soup.get_text(" ", strip=True))
        # 회차가 없는 모바일 텍스트는 정확한 회차 매핑이 불가능하므로 실패시킴.
        # 잘못된 데이터를 만드는 것보다 기존 정상 JSON 유지가 안전하다.
        raise RuntimeError("시험일정 표 구조를 인식하지 못했습니다. 기존 JSON을 유지합니다.")

    # 중복 제거 + 날짜순
    dedup = {x["round"]: x for x in result}
    result = sorted(dedup.values(), key=lambda x: x["examDate"])
    if not (1 <= len(result) <= 30):
        raise RuntimeError(f"unexpected schedule count: {len(result)}")
    return result


def parse_centers(html: str) -> list[dict]:
    """정적으로 노출된 고사장 행만 보수적으로 추출한다.

    현재 공식 페이지는 일부 정보를 동적으로 불러올 수 있으므로, 인식이 불확실하면
    빈 목록을 반환하고 앱은 공식 고사장 페이지 바로가기를 사용한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    centers: list[dict] = []
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if len(cells) < 3:
            continue
        row = " | ".join(cells)
        if "주소" in row and ("고사장" in row or "접수 가능" in row):
            continue
        # 주소처럼 보이는 한글 지명 + 학교/센터명이 모두 있을 때만 채택
        address = next((c for c in cells if re.search(r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주).*(로|길|동|읍|면)", c)), "")
        name = next((c for c in cells if re.search(r"(학교|고등학교|중학교|대학교|센터|학원)", c) and c != address), "")
        if not address or not name:
            continue
        region_match = re.search(r"서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주", address)
        region = region_match.group(0) if region_match else ""
        centers.append({"region": region, "name": name, "address": address, "availableDates": ""})
    unique = {(x["name"], x["address"]): x for x in centers}
    return sorted(unique.values(), key=lambda x: (x["region"], x["name"]))


def comparable(obj: dict) -> dict:
    # updatedAt은 실제 데이터 변경 시에만 바꾸기 위해 비교에서 제외
    return {k: v for k, v in obj.items() if k != "updatedAt"}


def main() -> int:
    old = {}
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))

    schedule_html = _get(SCHEDULE_URL)
    schedules = parse_schedules(schedule_html)

    centers = old.get("centers", []) if isinstance(old.get("centers"), list) else []
    try:
        center_html = _get(CENTER_URL)
        parsed_centers = parse_centers(center_html)
        if parsed_centers:
            centers = parsed_centers
    except Exception as exc:
        print(f"[warn] center check failed; keeping previous centers: {exc}", file=sys.stderr)

    new = {
        "schemaVersion": 1,
        "updatedAt": old.get("updatedAt", ""),
        "source": "JPT 공식 홈페이지",
        "sourceUrl": SCHEDULE_URL,
        "centerUrl": CENTER_URL,
        "schedules": schedules,
        "centers": centers,
        "centerNote": "고사장은 매회차 변동될 수 있으므로 공식 고사장 안내 페이지를 최종 기준으로 확인하세요.",
    }

    if comparable(new) == comparable(old):
        print("No schedule/center changes. exam_info.json unchanged.")
        return 0

    new["updatedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    OUT.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {OUT}: schedules={len(schedules)}, centers={len(centers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
