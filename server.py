"""
동네 평생학습 MCP 서버 (파일럿)

대상 기관 2곳
  · 경주시 평생학습가족관   — 강좌 목록 페이지 실시간 조회
  · 대구평생학습플랫폼      — 공공데이터포털 공식 오픈API (인증키 설정 시)

툴 3개
  1. find_courses      조건으로 강좌 찾기
  2. get_course_detail 강좌 하나 자세히
  3. check_sources     지금 어느 기관 데이터가 살아 있는지 진단

실행:  python server.py
Claude 데스크톱 연결 방법은 README.md 참고.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Literal

# Claude 데스크톱이 다른 폴더에서 실행해도 sources.py를 찾을 수 있게 한다.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from sources import (
    Course,
    DaeguSource,
    GyeongjuSource,
    Institution,
    Notice,
    SourceError,
    cache_clear,
    search,
    search_notices,
)

from mcp.server.transport_security import TransportSecuritySettings

# MCP SDK는 DNS 리바인딩 공격을 막으려고 Host 헤더를 검사한다.
# 기본값이 localhost 계열만 허용이라, 인터넷에 올리면 '421 Invalid Host header'가 뜬다.
# 와일드카드('*')는 지원하지 않고 정확한 도메인만 받는다.
#
#   ALLOWED_HOSTS 를 지정하면  → 그 도메인만 허용 (권장)
#   지정하지 않으면            → 검사를 끈다
#
# 이 보호 장치는 원래 '내 PC에서 도는 서버를 악성 웹사이트가 부르는 것'을 막기 위한 것이라,
# HTTPS로 공개된 조회 전용 서버에서는 실익이 크지 않다. 다만 도메인이 정해져 있다면
# ALLOWED_HOSTS 를 넣어 두는 편이 낫다.
_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

if _hosts:
    _security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_hosts,
        allowed_origins=["*"],
    )
else:
    _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP(
    "dongne-pyeongsaeng",
    # 원격(HTTP)으로 띄울 때를 대비한 설정.
    # 세션을 서버에 쌓아 두지 않아야 무료 호스팅·프록시 환경에서 안정적이다.
    stateless_http=True,
    transport_security=_security,
)

_gj = GyeongjuSource()
_dg = DaeguSource()

SOURCES = {"경주": _gj, "대구": _dg}


# ---------------------------------------------------------------------------
# 수집 (실패한 기관은 결과에서 빼지 않고 따로 보고한다)
# ---------------------------------------------------------------------------

def _collect(which: list[str]) -> tuple[list[Course], dict[str, str]]:
    courses: list[Course] = []
    problems: dict[str, str] = {}
    for name in which:
        src = SOURCES.get(name)
        if src is None:
            problems[name] = "알 수 없는 기관"
            continue
        try:
            courses.extend(src.fetch_all())
        except SourceError as e:
            problems[name] = str(e)
        except Exception as e:  # 예상 못 한 오류도 조용히 삼키지 않는다
            problems[name] = f"{type(e).__name__}: {e}"
    return courses, problems


def _fmt(c: Course, idx: int) -> str:
    bits = [f"{idx}. [{c.source}] {c.title}", f"   id: {c.course_id}"]
    if c.institution:
        bits.append(f"   기관: {c.institution}")
    when = []
    if c.weekdays:
        when.append("".join(c.weekdays) + "요일")
    if c.start_time:
        when.append(f"{c.start_time}~{c.end_time or ''}".rstrip("~"))
    if when:
        bits.append("   일정: " + " ".join(when))
    if c.edu_from:
        bits.append(f"   교육기간: {c.edu_from} ~ {c.edu_to or ''}")
    bits.append(f"   수강료: {c.fee_label}   정원: {c.seats_label}")
    if c.apply_to:
        bits.append(f"   접수: {c.apply_from or '?'} ~ {c.apply_to}  ({c.deadline_label})")
    else:
        bits.append(f"   접수: {c.deadline_label}")
    if c.status:
        bits.append(f"   상태: {c.status}")
    if c.method:
        bits.append(f"   접수방법: {c.method}")
    if c.link:
        bits.append(f"   원문: {c.link}")
    return "\n".join(bits)


FOOTER = (
    "\n※ 기관 홈페이지를 실시간 조회한 결과입니다. "
    "정원·마감은 조회 직후에도 바뀔 수 있으니 신청 전 원문 링크나 전화로 최종 확인하세요."
)


# ---------------------------------------------------------------------------
# 툴 1 — 강좌 찾기
# ---------------------------------------------------------------------------

@mcp.tool()
def find_courses(
    region: Literal["전체", "경주", "대구"] = "전체",
    keyword: str | None = None,
    weekday: str | None = None,
    time_slot: Literal["", "오전", "오후", "저녁", "주말"] = "",
    free_only: bool = False,
    open_only: bool = True,
    has_seats: bool = False,
    limit: int = 15,
) -> str:
    """지금 신청할 수 있는 평생학습 강좌를 찾습니다. 경주시 평생학습가족관과 대구평생학습플랫폼을
    실시간으로 조회합니다.

    주민이 "우리 동네 강좌", "무료 수업 뭐 있어?", "저녁에 들을 수 있는 것",
    "자리 남은 강좌" 처럼 물을 때 쓰세요. 강좌 하나를 자세히 볼 때는 이 툴이 아니라
    get_course_detail을, 기관 접속 상태를 확인할 때는 check_sources를 쓰세요.

    Args:
        region: 조회할 지역. "전체"면 두 기관 모두.
        keyword: 강좌명·기관명·주제에 포함될 검색어. 여러 단어는 모두 포함되는 것만.
        weekday: 원하는 요일. "월", "화수", "토일" 처럼 자유롭게.
        time_slot: 시간대. 오전 06-12시, 오후 12-18시, 저녁 18-23시, 주말은 토·일 개설.
        free_only: 무료 강좌만. 수강료 미확인 건도 함께 보여줍니다.
        open_only: 접수 마감이 지나지 않은 것만 (기본값 참).
        has_seats: 정원이 아직 남은 것만.
        limit: 최대 결과 수.
    """
    which = ["경주", "대구"] if region == "전체" else [region]
    courses, problems = _collect(which)

    hits = search(
        courses,
        keyword=keyword,
        weekday=weekday,
        time_slot=time_slot or None,
        free_only=free_only,
        open_only=open_only,
        has_seats=has_seats,
        limit=limit,
    )

    lines: list[str] = []
    ok = [n for n in which if n not in problems]

    if not ok:
        lines.append("⚠ 조회한 기관 어느 곳에서도 데이터를 가져오지 못했습니다.")
        lines.append("   (강좌가 없는 것이 아니라, 데이터를 못 받은 상태입니다)")
        for k, v in problems.items():
            lines.append(f"   · {k}: {v}")
        return "\n".join(lines)

    header = f"조회 기관: {', '.join(ok)}  |  조회 시각: {datetime.now():%Y-%m-%d %H:%M}"
    lines.append(header)

    if problems:
        lines.append("")
        lines.append("⚠ 아래 기관은 이번 조회에서 제외됐습니다 (강좌 없음이 아닙니다):")
        for k, v in problems.items():
            lines.append(f"   · {k}: {v}")

    lines.append("")
    if not hits:
        lines.append(
            f"조건에 맞는 강좌가 {', '.join(ok)}에서 검색되지 않았습니다. "
            "조건을 넓혀 보세요 (free_only 해제, open_only 해제, 요일·시간대 제거)."
        )
        lines.append(f"   ※ 조회된 전체 강좌 수: {len(courses)}건")
        return "\n".join(lines)

    lines.append(f"조건에 맞는 강좌 {len(hits)}건 (전체 {len(courses)}건 중, 마감 임박순)")
    lines.append("")
    for i, c in enumerate(hits, 1):
        lines.append(_fmt(c, i))
        lines.append("")
    lines.append(FOOTER.strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 툴 2 — 강좌 상세
# ---------------------------------------------------------------------------

@mcp.tool()
def get_course_detail(course_id: str) -> str:
    """앞서 찾은 강좌 하나의 자세한 정보를 봅니다. course_id는 find_courses 결과의 id 값입니다.

    "그거 마감 언제야?", "얼마야?", "어떻게 신청해?", "자리 남았어?" 처럼 특정 강좌를
    파고들 때 쓰세요. 목록을 처음 찾을 때는 이 툴이 아니라 find_courses를 쓰세요.

    Args:
        course_id: find_courses 결과에 표시된 id (예: 26e101).
    """
    courses, problems = _collect(["경주", "대구"])
    target = next((c for c in courses if c.course_id == course_id), None)

    if target is None:
        msg = [f"id '{course_id}' 에 해당하는 강좌를 찾지 못했습니다."]
        if problems:
            msg.append("일부 기관 데이터를 못 받은 상태입니다:")
            for k, v in problems.items():
                msg.append(f"   · {k}: {v}")
        msg.append("find_courses로 목록을 다시 조회해 id를 확인하세요.")
        return "\n".join(msg)

    c = target
    L = [
        f"[{c.source}] {c.title}",
        "─" * 40,
        f"강좌 id      : {c.course_id}",
        f"운영기관     : {c.institution or '미확인'}",
        f"지역         : {c.region}",
        f"교육 요일    : {''.join(c.weekdays) + '요일' if c.weekdays else '미확인'}",
        f"교육 시간    : {f'{c.start_time}~{c.end_time}' if c.start_time else '미확인'}",
        f"교육 기간    : {c.edu_from or '미확인'} ~ {c.edu_to or ''}",
        f"수강료       : {c.fee_label}",
        f"모집 현황    : {c.seats_label}",
        f"접수 기간    : {c.apply_from or '미확인'} ~ {c.apply_to or '미확인'}",
        f"마감까지     : {c.deadline_label}",
        f"진행 상태    : {c.status or '미확인'}",
        f"접수 방법    : {c.method or '미확인'}",
    ]
    if c.target:
        L.append(f"교육 대상    : {c.target}")
    if c.topic:
        L.append(f"분야         : {c.topic}")
    L.append(f"원문 링크    : {c.link}")
    if c.note:
        L.append(f"안내         : {c.note}")
    L.append("")
    L.append(f"조회 시각    : {datetime.now():%Y-%m-%d %H:%M}")
    L.append(FOOTER.strip())
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 툴 3 — 평생교육기관 명부
# ---------------------------------------------------------------------------

@mcp.tool()
def find_institutions(
    keyword: str | None = None,
    category: str | None = None,
    limit: int = 100,
) -> str:
    """우리 동네에서 평생교육을 하는 기관을 찾습니다. 강좌가 아니라 '어디서 배울 수 있나'에
    답하는 툴입니다.

    "우리 동네에 배울 수 있는 데 어디 있어?", "가까운 도서관", "주민자치센터 목록",
    "평생학습사랑방이 뭐가 있어?" 처럼 장소·기관을 물을 때 쓰세요.
    개별 강좌를 찾을 때는 이 툴이 아니라 find_courses를 쓰세요.

    Args:
        keyword: 기관명에 포함될 검색어 (예: 도서관, 안강, 공방).
        category: 유형으로 좁히기. 평생학습사랑방·학습포석정 / 주민자치센터 / 도서관 /
                  대학 평생교육원 / 복지·가족 시설 / 청소년 시설 / 문화 시설 /
                  지자체 평생학습관 / 민간 학원 / 민간 평생교육원 / 기타
        limit: 최대 결과 수.
    """
    try:
        insts = _gj.fetch_institutions()
    except SourceError as e:
        return f"⚠ 기관 명부를 가져오지 못했습니다 (기관이 없는 것이 아닙니다).\n   {e}"
    except Exception as e:
        return f"⚠ 예외 {type(e).__name__}: {e}"

    hits = insts
    if keyword:
        k = keyword.strip().lower()
        hits = [i for i in hits if k in i.name.lower()]
    if category:
        c = category.strip()
        hits = [i for i in hits if c in i.category]

    if not hits:
        return (
            f"조건에 맞는 기관이 없습니다. (전체 {len(insts)}곳 등록)\n"
            "유형: " + ", ".join(sorted({i.category for i in insts}))
        )

    by_cat: dict[str, list] = {}
    for i in hits[:limit]:
        by_cat.setdefault(i.category, []).append(i)

    L = [
        f"경주시 평생교육기관 {len(hits)}곳"
        + (f" (전체 {len(insts)}곳 중)" if len(hits) != len(insts) else ""),
        f"조회 시각: {datetime.now():%Y-%m-%d %H:%M}",
        "",
    ]
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        items = by_cat[cat]
        L.append(f"■ {cat} ({len(items)}곳)")
        for i in items:
            L.append(f"   · {i.name}")
        L.append("")

    L.append(
        "※ 이 명부는 경주시 평생학습포털에 등록된 기관 목록입니다. "
        "기관별 개설 강좌는 포털에 연계돼 있지 않아, 강좌는 각 기관에 직접 문의하셔야 합니다."
    )
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 툴 4 — 공지사항·소식으로 알리는 모집 정보
# ---------------------------------------------------------------------------

@mcp.tool()
def find_notices(
    keyword: str | None = None,
    board: str | None = None,
    include_all: bool = False,
    within_days: int = 180,
    limit: int = 20,
) -> str:
    """공지사항·평생학습소식 게시판에 올라온 모집 공고를 찾습니다.

    정형 강좌 목록에 없는 강좌가 여기 실립니다. 대학 평생교육원, 관내 기관, 유관기관
    프로그램은 사실상 이 경로로만 알려집니다. find_courses에서 원하는 게 안 나왔을 때,
    또는 "요즘 뭐 모집해?", "새로 올라온 소식 있어?" 처럼 물을 때 함께 쓰세요.

    강좌 목록에서 정원·수강료까지 딱 떨어지는 정보를 원하면 find_courses를,
    기관 목록을 원하면 find_institutions를 쓰세요.

    Args:
        keyword: 제목에 포함될 검색어 (예: 디지털, 문해, 어르신).
        board: "공지사항" 또는 "평생학습소식"으로 좁히기.
        include_all: 참이면 강사 채용·시스템 점검 같은 행정 공고까지 모두 보여 줍니다.
        within_days: 최근 며칠 안의 글만 (기본 180일).
        limit: 최대 결과 수.
    """
    try:
        notices = _gj.fetch_notices()
    except SourceError as e:
        return f"⚠ 게시판을 가져오지 못했습니다 (글이 없는 것이 아닙니다).\n   {e}"
    except Exception as e:
        return f"⚠ 예외 {type(e).__name__}: {e}"

    kinds = () if include_all else ("모집", "안내")
    hits = search_notices(
        notices, keyword=keyword, board=board,
        kinds=kinds, within_days=within_days, limit=limit,
    )

    L = [
        f"경주시 평생학습포털 게시판 — 전체 {len(notices)}건 수집",
        "  " + " / ".join(f"{k} {v}건" for k, v in _gj.board_counts.items()),
        f"조회 시각: {datetime.now():%Y-%m-%d %H:%M}",
        "",
    ]
    for k, v in _gj.board_errors.items():
        L.append(f"⚠ {k}: {v}")

    if not hits:
        L.append(
            f"조건에 맞는 글이 없습니다. "
            f"(최근 {within_days}일, "
            + ("전체 유형" if include_all else "모집·안내 글만")
            + " 기준)"
        )
        L.append("검색어를 빼거나 within_days를 늘려 보세요.")
        return "\n".join(L)

    L.append(f"조건에 맞는 글 {len(hits)}건 (최신순)")
    L.append("")
    for i, n in enumerate(hits, 1):
        head = f"{i}. [{n.kind}] {n.title}"
        if n.deadline_hint:
            head += f"  (마감 {n.deadline_hint})"
        L.append(head)
        age = f"{n.age_days}일 전" if n.age_days is not None else "날짜 미확인"
        L.append(f"   {n.board} · {n.posted or '?'} ({age})")
        L.append(f"   {n.link}")
        L.append("")

    L.append(
        "※ 게시물은 강좌 목록과 달리 정원·수강료가 정리돼 있지 않습니다. "
        "자세한 내용은 링크를 열어 확인하세요."
    )
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 툴 5 — 소스 진단
# ---------------------------------------------------------------------------

@mcp.tool()
def check_sources(refresh: bool = False) -> str:
    """지금 어느 기관에서 데이터를 가져올 수 있는지 진단합니다. 결과가 이상하거나 비어 있을 때,
    또는 설정이 제대로 됐는지 확인할 때 쓰세요.

    각 기관의 접속 성공 여부, 수집된 강좌 수, 대구 API 설정 상태를 알려 줍니다.

    Args:
        refresh: 참이면 캐시를 비우고 새로 조회합니다.
    """
    if refresh:
        cache_clear()

    L = ["데이터 소스 진단", "─" * 40]

    # 경주
    try:
        gj = _gj.fetch_all()
        L.append(f"✅ 경주시 평생학습가족관 : 정상 · {len(gj)}건")
        L.append(f"   수집 방식   : 목록 화면 {_gj.last_mode or 'POST'} 실시간 조회")
        L.append(f"   페이지 설정 : viewPage / rowCount={_gj.rows_per_page}")
        for label, cnt in _gj.channel_counts.items():
            L.append(f"     · {label} — {cnt}건")
        for label, err in _gj.channel_errors.items():
            L.append(f"     ⚠ {label} — {err[:70]}")
        try:
            insts = _gj.fetch_institutions()
            cats = {}
            for i in insts:
                cats[i.category] = cats.get(i.category, 0) + 1
            top = ", ".join(f"{k} {v}" for k, v in
                            sorted(cats.items(), key=lambda x: -x[1])[:4])
            L.append(f"     · 기관 명부 — {len(insts)}곳 ({top})")
            L.append("       ※ 기관별 개설 강좌는 포털에 연계돼 있지 않음")
        except Exception as e:
            L.append(f"     ⚠ 기관 명부 — {type(e).__name__}: {e}")
        try:
            nts = _gj.fetch_notices()
            kinds: dict[str, int] = {}
            for n in nts:
                kinds[n.kind] = kinds.get(n.kind, 0) + 1
            brief = ", ".join(f"{k} {v}" for k, v in
                              sorted(kinds.items(), key=lambda x: -x[1]))
            L.append(f"     · 게시판 — {len(nts)}건 ({brief})")
            for b, cnt in _gj.board_counts.items():
                L.append(f"       {b} {cnt}건")
        except Exception as e:
            L.append(f"     ⚠ 게시판 — {type(e).__name__}: {e}")
        if gj:
            L.append(f"   예시        : {gj[0].title} ({gj[0].deadline_label})")
    except SourceError as e:
        L.append(f"❌ 경주시 평생학습가족관 : 실패")
        L.append(f"   원인        : {e}")
    except Exception as e:
        L.append(f"❌ 경주시 평생학습가족관 : 예외 {type(e).__name__}: {e}")

    L.append("")

    # 대구
    if not _dg.configured:
        L.append("⚙️ 대구평생학습플랫폼 : 미설정")
        L.append("   필요 작업   : 공공데이터포털에서 활용신청 후 아래 두 값을 환경변수로 설정")
        L.append("     DAEGU_API_KEY = 일반 인증키(Decoding)")
        L.append("     DAEGU_API_URL = 활용신청 상세화면의 '요청 주소'")
        L.append(f"   데이터셋    : {_dg.DATASET}")
        L.append("   ※ 미설정이어도 경주 조회는 정상 동작합니다.")
    else:
        try:
            dg = _dg.fetch_all()
            L.append(f"✅ 대구평생학습플랫폼 : 정상 · {len(dg)}건")
            if dg:
                L.append(f"   예시        : {dg[0].title} ({dg[0].deadline_label})")
        except SourceError as e:
            L.append("❌ 대구평생학습플랫폼 : 실패")
            L.append(f"   원인        : {e}")
        except Exception as e:
            L.append(f"❌ 대구평생학습플랫폼 : 예외 {type(e).__name__}: {e}")

    L.append("")
    L.append(f"진단 시각 : {datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append("파일럿 범위 : 2개 기관. 확장 시 sources.py에 클래스를 추가하고 SOURCES에 등록.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 상태 확인용 페이지
# 배포가 잘 됐는지 브라우저 주소창에 그냥 주소만 넣어 확인할 수 있게 한다.
# ---------------------------------------------------------------------------

@mcp.custom_route("/", methods=["GET"])
async def health(request):  # noqa: ANN001
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(
        "동네 평생학습 MCP 서버가 실행 중입니다.\n"
        "\n"
        "Claude 앱 > 설정 > 커넥터 > 사용자 지정 커넥터 추가 에서\n"
        "아래 주소를 입력하세요 (끝에 /mcp 를 꼭 붙이세요):\n"
        f"    {request.url.scheme}://{request.url.netloc}/mcp\n"
        "\n"
        "제공 도구: find_courses, get_course_detail, find_institutions,\n"
        "           find_notices, check_sources\n"
        "데이터: 경주시 평생학습포털(실시간 조회), 대구평생학습플랫폼(공공데이터포털 API)\n",
        media_type="text/plain; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# 실행 방식 두 가지
#   stdio  : 내 PC에서 Claude 데스크톱에 붙일 때 (기본값)
#   http   : 인터넷에 올려 Claude 앱 '커스텀 커넥터'로 붙일 때
#            MCP_TRANSPORT=http 환경변수로 켠다. 접속 주소는 https://호스트/mcp
# ---------------------------------------------------------------------------

def run_http() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        import uvicorn
        app = mcp.streamable_http_app()
    except (AttributeError, ImportError):
        # 구버전 SDK 대비
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
        return
    print(f"MCP 서버 시작 — http://{host}:{port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    try:
        if os.environ.get("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http"):
            run_http()
        else:
            mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)
