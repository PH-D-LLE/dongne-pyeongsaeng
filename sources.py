"""
동네 평생학습 MCP — 데이터 소스 모듈

두 기관을 다룬다.
  1. 경주시 평생학습가족관  : 공식 API 없음 → 강좌 목록 페이지를 실시간 조회해 파싱
  2. 대구평생학습플랫폼      : 공공데이터포털 공식 오픈API 사용 (인증키 필요)

설계 원칙
  - 실시간 조회. 로컬 DB에 쌓아두지 않는다. 파일럿 단계에서는 신선도가 최우선.
  - 짧은 메모리 캐시(기본 10분)로 같은 질의 반복 시 기관 서버를 다시 때리지 않는다.
  - 파싱은 HTML 태그·클래스명이 아니라 '화면에 보이는 라벨 텍스트'를 기준으로 한다.
    지자체 사이트는 개편 시 마크업이 바뀌어도 라벨 문구는 잘 안 바뀌기 때문.
  - 데이터를 못 가져온 것과 강좌가 없는 것을 반드시 구분해서 돌려준다.
"""

from __future__ import annotations

import os
import re
import time
import json
import html
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Any, Iterable

import httpx

# ---------------------------------------------------------------------------
# 공통 설정
# ---------------------------------------------------------------------------

UA = "DongnePyeongsaeng-MCP/0.1 (pilot; lifelong-learning course search)"
TIMEOUT = 20.0
CACHE_TTL_SEC = int(os.environ.get("DPS_CACHE_TTL", "600"))
MAX_PAGES = int(os.environ.get("DPS_MAX_PAGES", "8"))
MAX_ENGN = int(os.environ.get("DPS_MAX_ENGN", "25"))
POLITE_DELAY = float(os.environ.get("DPS_DELAY", "0.4"))
KEEP_TEST = os.environ.get("DPS_KEEP_TEST", "").strip() in ("1", "true", "True")
ROWS_PER_PAGE = int(os.environ.get("DPS_ROWS", "100"))

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


# ---------------------------------------------------------------------------
# 강좌 표준 레코드
# ---------------------------------------------------------------------------

@dataclass
class Course:
    source: str                      # "경주" | "대구"
    course_id: str                   # 소스 내 고유 식별자
    title: str
    institution: str = ""
    region: str = ""
    weekdays: list[str] = field(default_factory=list)
    start_time: str | None = None     # "10:00"
    end_time: str | None = None       # "13:00"
    fee: int | None = None            # 원. 0=무료, None=미확인
    capacity: int | None = None
    applied: int | None = None
    apply_from: str | None = None     # "2026-08-03 10:00"
    apply_to: str | None = None
    edu_from: str | None = None       # "2026-08-20"
    edu_to: str | None = None
    status: str = ""                  # 접수전 | 접수중 | 접수완료 ...
    method: str = ""                  # 접수방법 / 선정방법
    target: str = ""
    topic: str = ""
    link: str = ""
    note: str = ""

    # --- 파생 정보 ---------------------------------------------------------

    @property
    def fee_label(self) -> str:
        if self.fee is None:
            return "수강료 미확인"
        if self.fee == 0:
            return "무료"
        return f"{self.fee:,}원"

    @property
    def seats_label(self) -> str:
        if self.capacity is None:
            return "정원 미확인"
        if self.applied is None:
            return f"정원 {self.capacity}명"
        left = self.capacity - self.applied
        if left > 0:
            return f"{self.applied}/{self.capacity}명 (남은 자리 {left})"
        return f"{self.applied}/{self.capacity}명 (정원 초과·마감 가능)"

    def days_until_deadline(self, today: date | None = None) -> int | None:
        """접수 마감까지 남은 일수. 음수면 이미 지났다."""
        if not self.apply_to:
            return None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", self.apply_to)
        if not m:
            return None
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (d - (today or date.today())).days

    @property
    def deadline_label(self) -> str:
        n = self.days_until_deadline()
        if n is None:
            return "접수기간 미확인"
        if n < 0:
            return f"접수 종료 ({-n}일 지남)"
        if n == 0:
            return "오늘 마감"
        return f"마감 D-{n}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fee_label"] = self.fee_label
        d["seats_label"] = self.seats_label
        d["deadline_label"] = self.deadline_label
        return d


# ---------------------------------------------------------------------------
# 아주 작은 TTL 캐시
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}


def cache_get(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > CACHE_TTL_SEC:
        _cache.pop(key, None)
        return None
    return val


def cache_put(key: str, val: Any):
    _cache[key] = (time.time(), val)


def cache_clear():
    _cache.clear()


# ---------------------------------------------------------------------------
# 텍스트 유틸
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")


def strip_tags(fragment: str) -> str:
    """태그를 제거하되 블록 경계는 공백으로 살려 라벨이 붙지 않게 한다."""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(td|th|tr|li|p|div|dd|dt|span)>", " ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = unicodedata.normalize("NFC", s)
    s = _WS.sub(" ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def to_int(s: str | None) -> int | None:
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


class SourceError(RuntimeError):
    """기관 서버에서 데이터를 가져오지 못했다. '강좌 없음'과 구분하기 위한 예외."""


# ---------------------------------------------------------------------------
# 평생교육기관 (강좌가 아니라 '어디서 배울 수 있는가')
# ---------------------------------------------------------------------------

@dataclass
class Institution:
    source: str
    code: str
    name: str
    category: str
    region: str = ""
    link: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 이름만 보고 유형을 나눈다. 순서가 중요하다 — 위에서부터 먼저 걸리는 것을 쓴다.
INST_RULES: list[tuple[str, str]] = [
    (r"평생학습사랑방|학습포석정", "평생학습사랑방·학습포석정"),
    (r"주민자치센터", "주민자치센터"),
    (r"도서관", "도서관"),
    (r"대학교?\s*평생교육원|대학\s*부설", "대학 평생교육원"),
    (r"복지관|가족지원센터|자원봉사센터", "복지·가족 시설"),
    (r"청소년|화랑마을", "청소년 시설"),
    # '문화센타'는 오탈자가 아니라 기관이 등록한 실제 표기다. 둘 다 받는다.
    (r"문화원|예술의전당|문화센[터타]|생활문화센[터타]|박물관", "문화 시설"),
    (r"평생학습관|평생학습가족관|평생학습원|평생학습센터", "지자체 평생학습관"),
    (r"학원|스쿨|아카데미", "민간 학원"),
    (r"평생교육원|교육원|사회교육원|양성원", "민간 평생교육원"),
]


def classify_institution(name: str) -> str:
    for pattern, label in INST_RULES:
        if re.search(pattern, name):
            return label
    return "기타"


# ---------------------------------------------------------------------------
# 게시물 (공지사항·소식으로 알리는 강좌 정보)
# ---------------------------------------------------------------------------
#
# 정형 강좌 목록을 갖춘 지자체는 오히려 소수다. 많은 곳이 공지사항 게시판에
# "○○ 수강생 모집" 같은 글로만 알린다. 경주도 관내 기관 강좌는 정형 데이터가
# 없지만 공지사항에는 모집 공고가 올라온다. 그래서 게시판을 별도 소스로 다룬다.

@dataclass
class Notice:
    source: str
    board: str
    post_id: str
    title: str
    posted: str | None = None       # "2026-07-24"
    link: str = ""
    kind: str = ""                  # 모집 | 안내 | 일반
    deadline_hint: str = ""         # 제목에 적힌 마감 표기 (예: "~6/30")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_days"] = self.age_days
        return d

    @property
    def age_days(self) -> int | None:
        if not self.posted:
            return None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", self.posted)
        if not m:
            return None
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (date.today() - d).days


# 강한 신호 — 실제로 사람을 모으는 글
RE_RECRUIT = re.compile(
    r"모집|수강생|교육생|참여자|참가자|신청|접수|선착순|추가모집|연장"
)
# 약한 신호 — 강좌 관련이지만 모집은 아닐 수 있는 글
RE_COURSE_ISH = re.compile(
    r"강좌|프로그램|교육|아카데미|특강|과정|학습|워크숍|워크샵|클래스|캠프"
)
# 모집과 무관한 행정 공고 — 제외 대상
RE_ADMIN = re.compile(
    r"강사\s*모집|강사\s*위촉|채용|입찰|계약|공고에\s*따른|합격자|심사\s*결과"
    r"|시스템\s*작업|점검\s*안내|휴관|개인정보|정기\s*휴무"
)
# 제목에 적힌 마감 표기: (~6/30), ~6/30, 6/30까지, 6.30.까지
RE_DEADLINE_HINT = re.compile(
    r"(\d{1,2})\s*[./월]\s*(\d{1,2})\s*[일.]?\s*까지"
    r"|~\s*(\d{1,2})\s*[./월]\s*(\d{1,2})"
)


def classify_notice(title: str) -> str:
    if RE_ADMIN.search(title):
        return "행정"
    if RE_RECRUIT.search(title):
        return "모집"
    if RE_COURSE_ISH.search(title):
        return "안내"
    return "일반"


def deadline_hint(title: str) -> str:
    m = RE_DEADLINE_HINT.search(title)
    if not m:
        return ""
    g = [x for x in m.groups() if x]
    if len(g) >= 2:
        return f"~{int(g[0])}/{int(g[1])}"
    return ""


# ===========================================================================
# 1. 경주시 평생학습가족관
# ===========================================================================

class GyeongjuSource:
    """
    경주시 평생학습가족관 강좌 목록 실시간 조회.

    목록 페이지가 서버에서 완전한 표로 렌더링되므로 그 표를 그대로 읽는다.
    확인된 필드: 강좌코드·강좌명·교육기관·요일·시간·수강료·모집현황·접수방법
                 ·신청기간·우선접수·교육기간·상태

    목록 화면은 lectureManagement 폼을 index.do 로 POST 하는 구조다.
    실제 사이트 HTML에서 확인한 필드:
        viewPage      페이지 번호   (hidden, 기본 1)
        rowCount      페이지당 건수 (hidden, 기본 10)
        menu_idx      메뉴 식별자   (126 = 평생학습 강좌)
        program_type  A2000 = 일반강좌. 그 외 값이면 otherLects.do(특성화 프로그램)
        search_type   검색 기준     (lect_nm = 강좌명)

    rowCount를 키우면 한 번의 요청으로 여러 건을 받을 수 있어
    기관 서버 부담과 응답 시간이 함께 줄어든다.
    """

    NAME = "경주"
    REGION = "경상북도 경주시"
    BASE = "https://www.gyeongju.go.kr"
    LIST = "/gjlll/main/lecture/index.do"
    MENU_IDX = "126"
    PROGRAM_TYPE = "A2000"
    CONTACT = "054-779-8925~8"

    # 경주 사이트는 강좌를 세 갈래 메뉴로 나눠 놓았다. 셋 다 봐야 지역 전체가 보인다.
    #   (표시명, 경로, menu_idx, program_type)
    CHANNELS: list[tuple[str, str, str, str]] = [
        ("가족관 강좌", "/gjlll/main/lecture/index.do", "126", "A2000"),
        ("특성화 프로그램", "/gjlll/main/lecture/otherLects.do", "203", ""),
    ]

    # 관내 평생교육기관 화면(indexEngn.do)은 기관 82곳이 명부로 등록돼 있으나
    # 강좌는 한 건도 연계돼 있지 않다(2026-07 실측: 기관을 바꿔도 결과 0건).
    # 그래서 강좌 수집 대상에서는 빼고, 기관 명부만 따로 가져온다.
    ENGN_PATH = "/gjlll/main/lecture/indexEngn.do"
    ENGN_MENU = "125"

    # 폼을 되돌려 보낼 때 빼야 하는 필드.
    # 개인정보·결제·단건 조회용 값들이라 목록 조회에 불필요하고, 보내면 오히려 방해가 된다.
    SKIP_FIELDS = {
        "usr_id", "name", "personalNum1", "personalNum2", "citizen",
        "lect_no", "rq_seq", "is_discount", "od_price", "od_dc_price",
        "od_dc_file_count", "checkType", "alert_change",
    }

    RE_HIDDEN = re.compile(r"""<input[^>]*type\s*=\s*["']hidden["'][^>]*>""", re.I)
    RE_ATTR = re.compile(r"""([\w-]+)\s*=\s*["']([^"']*)["']""")
    RE_OPTION = re.compile(
        r"""<option[^>]*\bvalue\s*=\s*["']([^"']*)["'][^>]*>(.*?)</option>""",
        re.I | re.S,
    )

    def __init__(self, client: httpx.Client | None = None,
                 rows_per_page: int = ROWS_PER_PAGE):
        self._client = client
        self.rows_per_page = max(10, rows_per_page)
        self.last_mode: str = ""              # 진단용: POST/GET 중 무엇으로 받았는지
        self.channel_counts: dict[str, int] = {}   # 진단용: 갈래별 수집 건수
        self.channel_errors: dict[str, str] = {}
        self.channel_notes: dict[str, str] = {}
        self.board_counts: dict[str, int] = {}
        self.board_errors: dict[str, str] = {}

    # -- HTTP ---------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ko-KR,ko;q=0.9",
                },
            )
        return self._client

    def _form(self, page: int, rows: int,
              menu_idx: str | None = None,
              program_type: str | None = None) -> dict[str, str]:
        return {
            "menu_idx": menu_idx or self.MENU_IDX,
            "program_type": program_type or self.PROGRAM_TYPE,
            "search_type": "lect_nm",
            "viewPage": str(page),
            "rowCount": str(rows),
        }

    def _fetch(self, page: int = 1, rows: int | None = None,
               path: str | None = None,
               menu_idx: str | None = None,
               program_type: str | None = None) -> str:
        """목록 화면을 가져온다. 폼 POST가 원래 방식이고, 실패하면 GET으로 한 번 더 시도."""
        rows = rows or self.rows_per_page
        path = path or self.LIST
        menu_idx = menu_idx or self.MENU_IDX
        url = self.BASE + path
        data = self._form(page, rows, menu_idx, program_type)
        headers = {"Referer": f"{url}?menu_idx={menu_idx}"}

        last_err: Exception | None = None
        for mode in ("post", "get"):
            try:
                if mode == "post":
                    r = self.client.post(url, data=data, headers=headers)
                else:
                    r = self.client.get(url, params=data, headers=headers)
                r.raise_for_status()
            except httpx.HTTPError as e:
                last_err = e
                continue
            if r.text and len(r.text) >= 500:
                self.last_mode = mode.upper()
                return r.text

        if last_err:
            raise SourceError(
                f"경주 사이트 접속 실패: {type(last_err).__name__}: {last_err}"
            ) from last_err
        raise SourceError("경주 사이트가 빈 응답을 보냈습니다 (차단 또는 점검 가능).")

    # -- 폼 자동 수집 ---------------------------------------------------------

    def _open(self, path: str, menu_idx: str) -> str:
        """메뉴 화면을 그냥 열어 본다. 폼 구조를 읽기 위한 첫 요청."""
        try:
            r = self.client.get(self.BASE + path, params={"menu_idx": menu_idx})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceError(f"{path} 접속 실패: {type(e).__name__}: {e}") from e
        if not r.text or len(r.text) < 500:
            raise SourceError(f"{path} 가 빈 응답을 보냈습니다.")
        return r.text

    def _submit(self, path: str, menu_idx: str, form: dict[str, str]) -> str:
        url = self.BASE + path
        try:
            r = self.client.post(
                url, data=form,
                headers={"Referer": f"{url}?menu_idx={menu_idx}"},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceError(f"{path} 조회 실패: {type(e).__name__}: {e}") from e
        self.last_mode = "POST"
        return r.text

    def _harvest(self, page_html: str) -> dict[str, str]:
        """화면에 들어 있는 hidden 값을 그대로 모은다.

        파라미터 이름을 추측하는 대신 사이트가 알려 주는 것을 쓴다.
        메뉴마다 폼이 달라도, 나중에 사이트가 바뀌어도 이 방식이 덜 깨진다.
        """
        out: dict[str, str] = {}
        for tag in self.RE_HIDDEN.findall(page_html):
            attrs = dict(self.RE_ATTR.findall(tag))
            name = attrs.get("name")
            if not name or name in self.SKIP_FIELDS:
                continue
            out[name] = attrs.get("value", "")
        return out

    def _options(self, page_html: str, select_name: str) -> list[tuple[str, str]]:
        """지정한 select의 선택지를 (값, 표시명)으로 뽑는다."""
        m = re.search(
            rf"""<select[^>]*\bname\s*=\s*["']{re.escape(select_name)}["'][^>]*>(.*?)</select>""",
            page_html, re.I | re.S,
        )
        if not m:
            return []
        opts: list[tuple[str, str]] = []
        for val, label in self.RE_OPTION.findall(m.group(1)):
            val = val.strip()
            label = re.sub(r"\s+", " ", strip_tags(label)).strip()
            if val and label and not re.match(r"^(전체|선택|--)", label):
                opts.append((val, label))
        return opts

    # -- 파싱 ---------------------------------------------------------------

    ROW = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")

    RE_CODE = re.compile(r"\(\s*(\d{2}[A-Za-z]\d{2,4})\s*\)")
    RE_TIME = re.compile(r"(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})")
    RE_FEE = re.compile(r"수강료\s*([\d,]+)\s*원")
    RE_FREE = re.compile(r"수강료\s*(무료|0\s*원)")
    RE_SEATS = re.compile(r"(\d+)\s*명\s*/\s*(\d+)\s*명")
    RE_INST = re.compile(r"교육\s*기관\s*([^\n]{2,40}?)(?=교육\s*요일|교육\s*시간|수강료|$)")
    RE_DAYS = re.compile(r"교육\s*요일\s*([월화수목금토일,\s]+)")
    RE_METHOD = re.compile(
        r"접수방법\s*([^\n]{2,40}?)"
        r"(?=\s*(?:신청기간|우선접수|교육기간|접수전|접수중|접수완료|접수마감|$))"
    )
    RE_APPLY = re.compile(
        r"신청기간\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?)\s*~\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?)"
    )
    RE_EDU = re.compile(r"교육기간\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})")
    RE_STATUS = re.compile(r"(접수중|접수전|접수완료|접수마감|교육중|교육완료|교육전)")

    def _parse_row(self, row_html: str, include_test: bool = False,
                   channel: str = "", link: str = "") -> Course | None:
        text = strip_tags(row_html)
        if not text or len(text) < 20:
            return None
        # 표 머리글 행 제외
        if "강좌정보" in text and "신청" in text and "상태" in text and len(text) < 60:
            return None

        m_code = self.RE_CODE.search(text)
        # 강좌명: 코드 괄호 뒤부터 '교육기관' 앞까지
        if m_code:
            after = text[m_code.end():]
            title = re.split(r"교육\s*기관|모집인원|교육\s*요일", after)[0]
        else:
            # 코드가 없는 형태 — 첫 줄을 제목으로 본다
            title = re.split(r"교육\s*기관|모집인원|교육\s*요일", text)[0]
            title = re.sub(r"^\s*\d+\s+", "", title)
        title = re.sub(r"(모집인원|우선모집|신규|재등록)", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" ·-–—[]")
        if not title or len(title) < 2:
            return None

        # 기관이 올려 둔 관리자 테스트 강좌는 주민에게 보여줄 필요가 없다.
        # (경주 사이트에 '테스트강좌', '수강신청 연습' 항목이 실제로 노출되어 있음)
        # 남겨서 보고 싶으면 환경변수 DPS_KEEP_TEST=1
        if not (KEEP_TEST or include_test) and re.search(
            r"테스트\s*강좌|수강신청\s*연습|테스트용", title
        ):
            return None

        fee: int | None = None
        if self.RE_FREE.search(text):
            fee = 0
        else:
            mf = self.RE_FEE.search(text)
            if mf:
                fee = to_int(mf.group(1))

        applied = capacity = None
        ms = self.RE_SEATS.search(text)
        if ms:
            applied, capacity = to_int(ms.group(1)), to_int(ms.group(2))

        st, et = None, None
        mt = self.RE_TIME.search(text)
        if mt:
            st, et = mt.group(1), mt.group(2)

        days: list[str] = []
        md = self.RE_DAYS.search(text)
        if md:
            days = [d for d in WEEKDAYS if d in md.group(1)]

        inst = ""
        mi = self.RE_INST.search(text)
        if mi:
            inst = re.sub(r"\s+", " ", mi.group(1)).strip()
        if not inst:
            inst = "경주시평생학습가족관"

        af = at = ef = et2 = None
        ma = self.RE_APPLY.search(text)
        if ma:
            af, at = ma.group(1), ma.group(2)
        me = self.RE_EDU.search(text)
        if me:
            ef, et2 = me.group(1), me.group(2)

        status = " ".join(dict.fromkeys(self.RE_STATUS.findall(text))) or ""

        method = ""
        mm = self.RE_METHOD.search(text)
        if mm:
            method = re.sub(r"\s+", " ", mm.group(1)).strip()

        cid = m_code.group(1) if m_code else f"gj-{abs(hash(title)) % 10**8}"

        return Course(
            source=self.NAME,
            course_id=cid,
            title=title,
            institution=inst,
            region=self.REGION,
            weekdays=days,
            start_time=st,
            end_time=et,
            fee=fee,
            capacity=capacity,
            applied=applied,
            apply_from=af,
            apply_to=at,
            edu_from=ef,
            edu_to=et2,
            status=status,
            method=method,
            topic=channel,
            link=link or f"{self.BASE}{self.LIST}?menu_idx={self.MENU_IDX}",
            note=(f"[{channel}] " if channel else "")
                 + "상세·신청은 경주시 평생학습포털에서. 문의 " + self.CONTACT,
        )

    def _parse_page(self, page_html: str, include_test: bool = False,
                    channel: str = "", link: str = "") -> list[Course]:
        out: list[Course] = []
        for row in self.ROW.findall(page_html):
            c = self._parse_row(row, include_test=include_test,
                                channel=channel, link=link)
            if c:
                out.append(c)
        return out

    # -- 총 건수 --------------------------------------------------------------

    RE_TOTAL = re.compile(r"(?:총|전체)\s*([\d,]+)\s*(?:건|개)")

    def total_count(self, page_html: str) -> int | None:
        m = self.RE_TOTAL.search(strip_tags(page_html))
        return to_int(m.group(1)) if m else None

    # -- 공개 메서드 ---------------------------------------------------------

    def _paginate(self, label: str, path: str, menu_idx: str,
                  form: dict[str, str], max_pages: int,
                  seen: set[str], inst_hint: str = "") -> list[Course]:
        """주어진 폼으로 viewPage를 넘겨 가며 끝까지 수집한다."""
        got: list[Course] = []
        link = f"{self.BASE}{path}?menu_idx={menu_idx}"

        for p in range(1, max_pages + 1):
            if p > 1:
                time.sleep(POLITE_DELAY)
            f = dict(form)
            f["viewPage"] = str(p)
            f["rowCount"] = str(self.rows_per_page)
            f["menu_idx"] = menu_idx

            page_html = self._submit(path, menu_idx, f)

            # 종료 판정은 '필터 전' 원본 행 수로 한다.
            # 테스트강좌를 걸러낸 뒤 숫자로 비교하면 마지막 페이지가 아닌데도 멈춘다.
            raw = self._parse_page(page_html, include_test=True)
            shown = self._parse_page(page_html, channel=label, link=link)

            fresh = [c for c in shown if c.course_id not in seen]
            for c in fresh:
                seen.add(c.course_id)
                if inst_hint and (not c.institution
                                  or c.institution == "경주시평생학습가족관"):
                    c.institution = inst_hint
            got.extend(fresh)

            if len(raw) < self.rows_per_page:
                break
            if not fresh:          # 같은 내용만 반복되면 중단
                break

        return got

    def _fetch_channel(self, label: str, path: str, menu_idx: str,
                       program_type: str, max_pages: int,
                       seen: set[str]) -> list[Course]:
        """갈래 하나를 수집한다.

        1) 화면을 열어 hidden 폼을 그대로 가져온다
        2) 그 폼으로 조회한다
        3) 결과가 없고 기관 선택(engn_code)이 필요한 화면이면 기관별로 나눠 조회한다
        """
        page_html = self._open(path, menu_idx)
        form = self._harvest(page_html)

        if not form:                       # hidden을 못 읽으면 알려진 기본값으로
            form = self._form(1, self.rows_per_page, menu_idx, program_type or None)
        if program_type:
            form["program_type"] = program_type

        # 사이트가 '없는 프로그램 유형'이라고 알려 주면 실제로 등록된 게 없다는 뜻
        if form.get("program_type", "").strip() == "없는 프로그램 유형":
            self.channel_errors[label] = "현재 등록된 프로그램이 없습니다 (사이트 표시)"
            return []

        return self._paginate(label, path, menu_idx, form, max_pages, seen)

    # -- 기관 명부 -------------------------------------------------------------

    def fetch_institutions(self) -> list[Institution]:
        """관내 평생교육기관 명부를 가져온다.

        강좌 목록은 비어 있지만 기관 명부는 살아 있다.
        '우리 동네 어디서 배울 수 있나'에 답하는 재료가 된다.
        """
        ck = "gj:inst"
        cached = cache_get(ck)
        if cached is not None:
            return cached

        page_html = self._open(self.ENGN_PATH, self.ENGN_MENU)
        opts = self._options(page_html, "engn_code")
        if not opts:
            raise SourceError(
                "경주 기관 명부를 읽지 못했습니다. "
                "화면 구조가 바뀌었을 수 있습니다 (engn_code 선택지 없음)."
            )

        link = f"{self.BASE}{self.ENGN_PATH}?menu_idx={self.ENGN_MENU}"
        insts = [
            Institution(
                source=self.NAME,
                code=code,
                name=name,
                category=classify_institution(name),
                region=self.REGION,
                link=link,
            )
            for code, name in opts
        ]
        cache_put(ck, insts)
        return insts

    # -- 게시판 ---------------------------------------------------------------

    BBS_PATH = "/gjlll/main/bbs/index.do"
    BOARDS: list[tuple[str, str, str]] = [
        # (표시명, menu_idx, master_idx)
        ("공지사항", "144", "1"),
        ("평생학습소식", "145", "13"),
    ]

    # 게시판 마크업은 지자체마다 제각각이라 표 구조를 믿지 않는다.
    # 대신 '글 보기 링크'를 기준으로 잡고 그 주변에서 날짜를 줍는다.
    # 1순위: bbs_idx가 든 링크 (href든 onclick이든)
    RE_POST = re.compile(
        r"""<a\b[^>]*?(?:href|onclick)\s*=\s*["']([^"']*bbs_idx['"]?\s*[=,]\s*['"]?(\d+)[^"']*)["'][^>]*>(.*?)</a>""",
        re.I | re.S,
    )
    # 2순위: 링크 형태를 못 잡을 때 글 번호 위치를 기준으로 주변을 훑는다
    RE_IDX = re.compile(r"""bbs_idx['"]?\s*[=,]\s*['"]?(\d+)""")
    RE_DATE = re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})")

    def _clean_title(self, raw: str) -> str:
        t = re.sub(r"\s+", " ", strip_tags(raw)).strip()
        t = re.sub(r"^(새글|NEW|new|공지|알림|첨부파일|파일)\s*", "", t).strip()
        t = re.sub(r"\s*(새글|첨부파일 있음|이미지)\s*$", "", t).strip()
        return t

    def _mk_notice(self, board: str, post_id: str, title: str,
                   posted: str | None, href: str) -> Notice:
        if href.startswith("http"):
            link = href
        elif href.startswith("/"):
            link = self.BASE + href
        else:
            link = (f"{self.BASE}{self.BBS_PATH.replace('index.do', 'view.do')}"
                    f"?bbs_idx={post_id}")
        return Notice(
            source=self.NAME, board=board, post_id=post_id, title=title,
            posted=posted, link=link,
            kind=classify_notice(title), deadline_hint=deadline_hint(title),
        )

    def _date_near(self, page_html: str, pos: int, span: int = 400) -> str | None:
        dm = self.RE_DATE.search(strip_tags(page_html[pos: pos + span]))
        if not dm:
            return None
        return f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

    def _parse_board(self, page_html: str, board: str) -> list[Notice]:
        out: list[Notice] = []
        seen: set[str] = set()

        # --- 1순위: 링크에서 바로 뽑기 ---------------------------------------
        for m in self.RE_POST.finditer(page_html):
            href, post_id, inner = m.group(1), m.group(2), m.group(3)
            if post_id in seen:
                continue
            title = self._clean_title(inner)
            if len(title) < 4:
                continue
            seen.add(post_id)
            out.append(self._mk_notice(
                board, post_id, title, self._date_near(page_html, m.end()), href,
            ))

        if out:
            return out

        # --- 2순위: 글 번호 주변을 훑기 ---------------------------------------
        # 목록이 자바스크립트 링크나 특이한 마크업으로 되어 있을 때를 대비한다.
        for m in self.RE_IDX.finditer(page_html):
            post_id = m.group(1)
            if post_id in seen:
                continue

            window = page_html[max(0, m.start() - 200): m.end() + 600]
            # 창 안의 앵커 중 가장 긴 글자를 제목으로 본다
            cands = [
                self._clean_title(a)
                for a in re.findall(r"(?is)<a\b[^>]*>(.*?)</a>", window)
            ]
            cands = [c for c in cands if len(c) >= 6]
            if not cands:
                lines = [
                    self._clean_title(x)
                    for x in re.split(r"(?i)</t[dh]>|<br\s*/?>", window)
                ]
                cands = [c for c in lines if len(c) >= 6]
            if not cands:
                continue

            title = max(cands, key=len)
            if re.fullmatch(r"[\d\s.\-/]+", title):
                continue

            seen.add(post_id)
            out.append(self._mk_notice(
                board, post_id, title,
                self._date_near(page_html, m.end(), 600), "",
            ))

        if out:
            return out

        # --- 3순위: 표의 행 단위로 훑기 ---------------------------------------
        # bbs_idx라는 이름을 아예 안 쓰는 게시판(goView('1921') 같은 형태)을 위한 대비.
        # '링크 + 날짜'가 같은 행에 있으면 글 목록으로 본다.
        for tb in re.findall(r"(?is)<table[^>]*>(.*?)</table>", page_html):
            for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", tb):
                if not self.RE_DATE.search(strip_tags(tr)):
                    continue
                anchors = re.findall(r"(?is)<a\b([^>]*)>(.*?)</a>", tr)
                if not anchors:
                    continue

                attrs, inner = max(anchors, key=lambda a: len(strip_tags(a[1])))
                title = self._clean_title(inner)
                if len(title) < 6 or re.fullmatch(r"[\d\s.\-/]+", title):
                    continue

                nm = re.search(r"(\d{2,})", attrs)
                post_id = nm.group(1) if nm else str(abs(hash(title)) % 10**8)
                if post_id in seen:
                    continue

                hm = re.search(r"""href\s*=\s*["']([^"']+)["']""", attrs, re.I)
                href = hm.group(1) if hm else ""
                if href.startswith(("javascript:", "#")):
                    href = ""

                dm = self.RE_DATE.search(strip_tags(tr))
                posted = (
                    f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                    if dm else None
                )

                seen.add(post_id)
                out.append(self._mk_notice(board, post_id, title, posted, href))

        return out

    def fetch_notices(self, per_board: int = 60) -> list[Notice]:
        """공지사항·평생학습소식에 올라온 글을 가져온다.

        정형 강좌 목록에 없는 모집 정보가 여기 실린다.
        관내 기관·대학·유관기관 프로그램은 사실상 이 경로로만 알려진다.
        """
        ck = f"gj:bbs:{per_board}"
        cached = cache_get(ck)
        if cached is not None:
            return cached

        notices: list[Notice] = []
        self.board_counts = {}
        self.board_errors = {}

        for board, menu_idx, master_idx in self.BOARDS:
            try:
                first_html = self._open_bbs(menu_idx, master_idx)
                got = self._parse_board(first_html, board)

                # 더 많이 받아 보려고 폼을 되돌려 보낸다.
                # 단, 결과가 늘어날 때만 채택한다. POST가 엉뚱한 화면을 주면
                # 처음 받은 목록까지 잃게 되기 때문.
                form = self._harvest(first_html)
                form.update({
                    "menu_idx": menu_idx,
                    "master_idx": master_idx,
                    "viewPage": "1",
                    "rowCount": str(per_board),
                })
                try:
                    more = self._parse_board(
                        self._submit(self.BBS_PATH, menu_idx, form), board
                    )
                    if len(more) > len(got):
                        got = more
                except SourceError:
                    pass
            except SourceError as e:
                self.board_errors[board] = str(e)
                continue
            except Exception as e:
                self.board_errors[board] = f"{type(e).__name__}: {e}"
                continue

            self.board_counts[board] = len(got)
            notices.extend(got)
            time.sleep(POLITE_DELAY)

        if not notices:
            detail = "; ".join(f"{k}: {v}" for k, v in self.board_errors.items())
            raise SourceError(
                "경주 게시판에서 글을 가져오지 못했습니다. "
                + (f"[{detail}]" if detail else
                   "접속은 됐지만 글 목록을 못 읽었습니다 — "
                   "python probe_bbs.py 로 화면 구조를 확인하세요.")
            )

        notices.sort(key=lambda n: (n.posted or ""), reverse=True)
        cache_put(ck, notices)
        return notices

    def _open_bbs(self, menu_idx: str, master_idx: str) -> str:
        try:
            r = self.client.get(
                self.BASE + self.BBS_PATH,
                params={"menu_idx": menu_idx, "master_idx": master_idx},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceError(f"게시판 접속 실패: {type(e).__name__}: {e}") from e
        if not r.text or len(r.text) < 500:
            raise SourceError("게시판이 빈 응답을 보냈습니다.")
        return r.text

    def fetch_all(self, max_pages: int = MAX_PAGES) -> list[Course]:
        """세 갈래(가족관 강좌 / 관내 기관 강좌 / 특성화 프로그램)를 모두 수집한다."""
        ck = f"gj:all:{max_pages}:{self.rows_per_page}"
        cached = cache_get(ck)
        if cached is not None:
            return cached

        courses: list[Course] = []
        seen: set[str] = set()
        self.channel_counts = {}
        self.channel_errors = {}
        self.channel_notes = {}

        for label, path, menu_idx, ptype in self.CHANNELS:
            try:
                got = self._fetch_channel(label, path, menu_idx, ptype,
                                          max_pages, seen)
            except SourceError as e:
                self.channel_errors[label] = str(e)
                continue
            except Exception as e:
                self.channel_errors[label] = f"{type(e).__name__}: {e}"
                continue
            self.channel_counts[label] = len(got)
            courses.extend(got)
            time.sleep(POLITE_DELAY)

        if not courses:
            detail = "; ".join(f"{k}: {v}" for k, v in self.channel_errors.items())
            raise SourceError(
                "경주에서 강좌를 하나도 가져오지 못했습니다. "
                "사이트 구조가 바뀌었을 수 있습니다 "
                "(python selftest.py --dump 로 원본을 확인하세요). "
                + (f"[{detail}]" if detail else "")
            )

        cache_put(ck, courses)
        return courses


# ===========================================================================
# 2. 대구평생학습플랫폼 (공공데이터포털 오픈API)
# ===========================================================================

class DaeguSource:
    """
    대구광역시 평생학습포털 강좌조회서비스.
      데이터셋: https://www.data.go.kr/data/15061491/openapi.do

    사이트(dle.study.daegu.kr)는 자바스크립트로 화면을 그려서 크롤링이 어렵지만,
    공식 오픈API가 이미 열려 있으므로 크롤링하지 않고 API를 쓴다.

    두 가지를 환경변수로 받는다.
      DAEGU_API_KEY : 공공데이터포털 인증키(일반 인증키, Decoding)
      DAEGU_API_URL : 활용신청 상세화면의 '요청 주소'(엔드포인트)
                      데이터셋 개편으로 주소가 바뀔 수 있어 코드에 박지 않는다.

    미설정 시 예외를 던지지 않고 '미설정' 상태를 알려 준다.
    파일럿에서 경주만으로도 동작해야 하기 때문.
    """

    NAME = "대구"
    REGION = "대구광역시"
    DATASET = "https://www.data.go.kr/data/15061491/openapi.do"

    def __init__(self, client: httpx.Client | None = None):
        self.key = os.environ.get("DAEGU_API_KEY", "").strip()
        self.url = os.environ.get("DAEGU_API_URL", "").strip()
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.key and self.url)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": UA, "Accept": "application/json, text/xml"},
            )
        return self._client

    # -- 응답 파싱 ----------------------------------------------------------

    KEYMAP = {
        "title": ["lctreNm", "courseNm", "lctrNm", "prgrmNm", "title", "강좌명"],
        "institution": ["operInsttNm", "instNm", "operOrgNm", "orgNm", "운영기관명"],
        "fee": ["lctreCost", "tuitionFee", "cost", "fee", "수강료"],
        "capacity": ["fixnum", "capacity", "정원", "모집인원"],
        "applied": ["applicantCnt", "reqCnt", "신청인원"],
        "apply_from": ["rceptBgnde", "acceptBgnDe", "접수시작일자"],
        "apply_to": ["rceptEndde", "acceptEndDe", "접수종료일자"],
        "edu_from": ["eduBgnde", "eduBgnDe", "교육시작일자"],
        "edu_to": ["eduEndde", "eduEndDe", "교육종료일자"],
        "status": ["progrsSttus", "status", "진행상태", "강좌상태"],
        "target": ["eduTrgt", "target", "교육대상"],
        "topic": ["lctreSj", "category", "교육주제", "분야"],
        "weekdays": ["eduDay", "operDay", "운영요일"],
        "start_time": ["eduBgnTime", "교육시작시각"],
        "end_time": ["eduEndTime", "교육종료시각"],
        "course_id": ["lctreNo", "courseId", "lctrNo", "id", "강좌코드"],
        "link": ["hmpgAdres", "url", "홈페이지주소"],
    }

    @staticmethod
    def _pick(item: dict, names: Iterable[str]):
        for n in names:
            if n in item and item[n] not in (None, "", "null"):
                return item[n]
        # 대소문자 무시 재시도
        low = {k.lower(): v for k, v in item.items()}
        for n in names:
            v = low.get(n.lower())
            if v not in (None, "", "null"):
                return v
        return None

    def _to_course(self, item: dict) -> Course | None:
        title = self._pick(item, self.KEYMAP["title"])
        if not title:
            return None
        raw_fee = self._pick(item, self.KEYMAP["fee"])
        fee = to_int(str(raw_fee)) if raw_fee is not None else None
        if isinstance(raw_fee, str) and "무료" in raw_fee:
            fee = 0

        raw_days = self._pick(item, self.KEYMAP["weekdays"]) or ""
        days = [d for d in WEEKDAYS if d in str(raw_days)]

        cid = self._pick(item, self.KEYMAP["course_id"])
        return Course(
            source=self.NAME,
            course_id=str(cid) if cid else f"dg-{abs(hash(str(title))) % 10**8}",
            title=str(title).strip(),
            institution=str(self._pick(item, self.KEYMAP["institution"]) or "").strip(),
            region=self.REGION,
            weekdays=days,
            start_time=self._fmt_time(self._pick(item, self.KEYMAP["start_time"])),
            end_time=self._fmt_time(self._pick(item, self.KEYMAP["end_time"])),
            fee=fee,
            capacity=to_int(str(self._pick(item, self.KEYMAP["capacity"]) or "")),
            applied=to_int(str(self._pick(item, self.KEYMAP["applied"]) or "")),
            apply_from=self._fmt_date(self._pick(item, self.KEYMAP["apply_from"])),
            apply_to=self._fmt_date(self._pick(item, self.KEYMAP["apply_to"])),
            edu_from=self._fmt_date(self._pick(item, self.KEYMAP["edu_from"])),
            edu_to=self._fmt_date(self._pick(item, self.KEYMAP["edu_to"])),
            status=str(self._pick(item, self.KEYMAP["status"]) or "").strip(),
            target=str(self._pick(item, self.KEYMAP["target"]) or "").strip(),
            topic=str(self._pick(item, self.KEYMAP["topic"]) or "").strip(),
            link=str(self._pick(item, self.KEYMAP["link"]) or "https://study.daegu.kr/"),
            note="대구평생학습플랫폼 공식 오픈API",
        )

    @staticmethod
    def _fmt_date(v) -> str | None:
        if v in (None, ""):
            return None
        s = re.sub(r"[^\d]", "", str(v))
        if len(s) == 8:
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
        m = re.search(r"\d{4}-\d{2}-\d{2}", str(v))
        return m.group(0) if m else str(v)

    @staticmethod
    def _fmt_time(v) -> str | None:
        if v in (None, ""):
            return None
        s = re.sub(r"[^\d]", "", str(v))
        if len(s) == 4:
            return f"{s[0:2]}:{s[2:4]}"
        m = re.search(r"\d{1,2}:\d{2}", str(v))
        return m.group(0) if m else None

    @staticmethod
    def _extract_items(payload: Any) -> list[dict]:
        """공공데이터포털 응답의 중첩 구조에서 item 배열만 찾아낸다."""
        found: list[dict] = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k.lower() in ("item", "items", "row", "rows") and v:
                        if isinstance(v, list):
                            found.extend([x for x in v if isinstance(x, dict)])
                        elif isinstance(v, dict):
                            inner = v.get("item") or v.get("row")
                            if isinstance(inner, list):
                                found.extend([x for x in inner if isinstance(x, dict)])
                            elif isinstance(inner, dict):
                                found.append(inner)
                            else:
                                found.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)

        walk(payload)
        return found

    # -- 공개 메서드 ---------------------------------------------------------

    def fetch_all(self, max_rows: int = 500) -> list[Course]:
        if not self.configured:
            raise SourceError(
                "대구 API가 아직 설정되지 않았습니다. "
                "DAEGU_API_KEY와 DAEGU_API_URL 환경변수를 설정하세요. "
                f"활용신청: {self.DATASET}"
            )

        ck = f"dg:all:{max_rows}"
        cached = cache_get(ck)
        if cached is not None:
            return cached

        params = {
            "serviceKey": self.key,
            "pageNo": "1",
            "numOfRows": str(max_rows),
            "type": "json",
            "dataType": "JSON",
        }
        try:
            r = self.client.get(self.url, params=params)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceError(f"대구 API 호출 실패: {type(e).__name__}: {e}") from e

        body = r.text.strip()
        payload: Any
        if body.startswith("{") or body.startswith("["):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as e:
                raise SourceError(f"대구 API 응답을 JSON으로 읽지 못했습니다: {e}") from e
        else:
            # XML로 내려온 경우 — 태그 단위로 얕게 변환
            if "SERVICE" in body.upper() and "ERROR" in body.upper():
                msg = re.search(r"(?is)<returnAuthMsg>(.*?)</returnAuthMsg>", body)
                raise SourceError(
                    "대구 API가 오류를 반환했습니다: "
                    + (msg.group(1) if msg else body[:200])
                    + " (인증키 또는 요청주소를 확인하세요)"
                )
            payload = self._xml_items(body)

        items = self._extract_items(payload)
        if not items:
            raise SourceError(
                "대구 API 응답에서 강좌 항목을 찾지 못했습니다. "
                "요청주소(DAEGU_API_URL)가 강좌목록 오퍼레이션인지 확인하세요."
            )

        courses = [c for c in (self._to_course(i) for i in items) if c]
        cache_put(ck, courses)
        return courses

    @staticmethod
    def _xml_items(body: str) -> list[dict]:
        out = []
        for block in re.findall(r"(?is)<item>(.*?)</item>", body):
            row = {}
            for k, v in re.findall(r"(?is)<([A-Za-z0-9_]+)>(.*?)</\1>", block):
                row[k] = html.unescape(v).strip()
            if row:
                out.append(row)
        return [{"items": {"item": out}}] if out else []


# ===========================================================================
# 통합 검색
# ===========================================================================

TIME_SLOTS = {
    "오전": (6, 12),
    "오후": (12, 18),
    "저녁": (18, 23),
}


def _in_slot(c: Course, slot: str) -> bool:
    if slot == "주말":
        return any(d in ("토", "일") for d in c.weekdays)
    rng = TIME_SLOTS.get(slot)
    if not rng or not c.start_time:
        return False
    try:
        hh = int(c.start_time.split(":")[0])
    except ValueError:
        return False
    return rng[0] <= hh < rng[1]


def search(
    courses: Iterable[Course],
    keyword: str | None = None,
    weekday: str | None = None,
    time_slot: str | None = None,
    free_only: bool = False,
    open_only: bool = True,
    has_seats: bool = False,
    limit: int = 20,
) -> list[Course]:
    """파싱된 강좌 목록에 조건을 적용한다. 기관 서버에 부담을 주지 않도록 로컬 필터."""
    res = list(courses)

    if keyword:
        kws = [k for k in re.split(r"[\s,]+", keyword.strip()) if k]
        res = [
            c for c in res
            if all(k.lower() in f"{c.title} {c.institution} {c.topic} {c.target}".lower()
                   for k in kws)
        ]

    if weekday:
        want = [d for d in WEEKDAYS if d in weekday]
        if want:
            res = [c for c in res if any(d in c.weekdays for d in want)]

    if time_slot:
        res = [c for c in res if _in_slot(c, time_slot)]

    if free_only:
        res = [c for c in res if c.fee == 0 or c.fee is None]

    if open_only:
        def still_open(c: Course) -> bool:
            n = c.days_until_deadline()
            if n is not None and n < 0:
                return False
            if "접수완료" in c.status or "접수마감" in c.status:
                return False
            return True
        res = [c for c in res if still_open(c)]

    if has_seats:
        res = [
            c for c in res
            if c.capacity is None or c.applied is None or c.applied < c.capacity
        ]

    def sort_key(c: Course):
        n = c.days_until_deadline()
        return (0 if n is not None else 1, n if n is not None else 9999, c.title)

    res.sort(key=sort_key)
    return res[:limit]


def search_notices(
    notices: Iterable[Notice],
    keyword: str | None = None,
    board: str | None = None,
    kinds: tuple[str, ...] = ("모집", "안내"),
    within_days: int | None = 180,
    limit: int = 20,
) -> list[Notice]:
    """게시물을 걸러낸다. 기본값은 최근 6개월 안의 모집·안내 글."""
    res = list(notices)

    if board:
        res = [n for n in res if board in n.board]
    if kinds:
        res = [n for n in res if n.kind in kinds]
    if within_days is not None:
        res = [n for n in res
               if n.age_days is None or n.age_days <= within_days]
    if keyword:
        kws = [k for k in re.split(r"[\s,]+", keyword.strip()) if k]
        res = [n for n in res
               if all(k.lower() in n.title.lower() for k in kws)]

    res.sort(key=lambda n: (n.posted or ""), reverse=True)
    return res[:limit]
