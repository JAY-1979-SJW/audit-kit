"""프로젝트 유형별 표준 설계 — 계층의 책임, 하면 안 되는 일, 공통 관심사 위치.

계층 이름은 모든 유형이 같은 다섯 개(interface/service/repository/domain/infra)를 쓰고,
유형마다 설명과 규칙만 다르다. 여러 유형이 섞인 프로젝트(예: FastAPI + CAD + MCP)는 합쳐서 쓴다.
"""

from __future__ import annotations

from typing import Any

LAYER_ORDER = ["interface", "service", "repository", "domain", "infra"]

# 공통 관심사: 이 작업을 하는 코드는 지정된 모듈 한 곳(+진입점)에만 있어야 한다.
# calls: (모듈, 함수) — 모듈이 None 이면 함수 이름만으로 판정. subscripts: os.environ[...] 같은 첨자 접근.
# 키마다 값 모양(str/list/dict)이 달라 Any 로 둔다 — 쓰는 쪽에서 필요한 키를 duck-typing 으로 꺼낸다.
CONCERNS: dict[str, dict[str, Any]] = {
    "config": {
        "title": "설정 읽기 (환경 변수·.env)",
        "calls": [
            ("os", "getenv"),
            ("os.environ", "get"),
            (None, "load_dotenv"),
            (None, "dotenv_values"),
        ],
        "subscripts": ["os.environ"],
        "why": "설정을 여러 곳에서 직접 읽으면 기본값·형 변환이 제각각이 되고 테스트에서 바꾸기 어렵다",
    },
    "logging": {
        "title": "로깅 설정",
        "calls": [
            ("logging", "basicConfig"),
            ("logging.config", "dictConfig"),
            ("logging.config", "fileConfig"),
        ],
        "subscripts": [],
        "why": "로깅 설정은 진입점에서 한 번만 (Python 로깅 HOWTO). 모듈은 getLogger(__name__) 만 쓴다",
    },
    "db": {
        "title": "DB 연결·세션 생성",
        "calls": [
            (None, "create_engine"),
            (None, "create_async_engine"),
            (None, "sessionmaker"),
            (None, "async_sessionmaker"),
            ("sqlite3", "connect"),
            ("psycopg2", "connect"),
            ("pymysql", "connect"),
            ("pyodbc", "connect"),
        ],
        "subscripts": [],
        "why": "연결·세션 수명주기를 한 곳에서 관리해야 커밋·닫기 누락과 중복 연결을 막는다",
    },
    "com": {
        "title": "COM 자동화 연결 (AutoCAD·Excel 등)",
        "calls": [
            ("win32com.client", "Dispatch"),
            ("win32com.client", "DispatchEx"),
            ("win32com.client", "GetActiveObject"),
            ("win32com.client", "GetObject"),
            (None, "GetActiveObject"),
            ("comtypes.client", "CreateObject"),
            ("comtypes.client", "GetActiveObject"),
        ],
        "subscripts": [],
        "why": "COM 연결·재시도·해제를 어댑터 한 곳에 두어야 계산 로직이 AutoCAD 없이 테스트된다",
    },
    "http": {
        "title": "외부 HTTP 호출",
        "calls": [
            ("requests", None),
            ("httpx", None),
            ("urllib.request", "urlopen"),
            ("aiohttp", "ClientSession"),
        ],
        "subscripts": [],
        "why": "타임아웃·재시도·인증을 클라이언트 한 곳에서 관리한다",
    },
}

TYPES: dict[str, dict[str, Any]] = {
    "fastapi": {
        "title": "FastAPI 웹 서버 (+DB)",
        "detect": ["fastapi", "starlette"],
        "layers": {
            "interface": (
                "HTTP 라우터: 요청 검증 → 서비스 호출 → 응답 변환",
                ["DB 세션 직접 생성", "비즈니스 계산", "외부 API 직접 호출"],
            ),
            "service": (
                "유스케이스: 트랜잭션 경계, 여러 저장소·도메인 호출 순서",
                ["HTTPException 등 웹 프레임워크 사용", "요청 객체 의존"],
            ),
            "repository": ("DB 조회·저장 쿼리", ["비즈니스 규칙", "커밋 여부 결정(서비스가 결정)"]),
            "domain": ("ORM/스키마 모델, 순수 계산", ["DB 세션", "HTTP"]),
            "infra": ("설정, DB 연결·세션 팩토리, 로깅, 외부 API 클라이언트", ["비즈니스 규칙"]),
        },
        "concerns": {"config": "infra", "logging": "infra", "db": "infra", "http": "infra"},
        "external": {
            "fastapi": ["interface"],
            "starlette": ["interface"],
            "sqlalchemy": ["repository", "domain", "infra", "service"],
        },
    },
    "cad": {
        "title": "CAD·COM 자동화 (AutoCAD/Excel COM + ezdxf 계산)",
        "detect": ["win32com", "pythoncom", "pyautocad", "comtypes", "ezdxf"],
        "layers": {
            "interface": ("사용자 명령·플러그인 진입점·UI", ["도면 계산", "COM 직접 연결"]),
            "service": (
                "작업 흐름: 도면 열기 → 추출 → 계산 → 결과 저장 순서",
                ["COM 객체 직접 조작"],
            ),
            "repository": ("결과·설정 파일 저장/조회 (Excel·JSON·DB)", ["도면 계산"]),
            "domain": (
                "도면 해석·물량 계산 — ezdxf 데이터만 다루는 순수 로직",
                ["COM(win32com)", "파일 저장", "UI"],
            ),
            "infra": ("COM 어댑터(연결·재시도·해제), 설정, 로깅, 경로", ["물량 계산"]),
        },
        "concerns": {"config": "infra", "logging": "infra", "com": "infra"},
        "external": {
            "win32com": ["infra"],
            "pythoncom": ["infra"],
            "pyautocad": ["infra"],
            "comtypes": ["infra"],
        },
    },
    "mcp": {
        "title": "MCP 도구 서버 (Claude 연동)",
        "detect": ["mcp", "fastmcp"],
        "layers": {
            "interface": (
                "MCP 도구 정의: 인자 검증 → 서비스 호출 → 결과 직렬화",
                ["계산 로직", "파일·DB 직접 접근"],
            ),
            "service": ("도구가 부르는 작업 흐름", ["MCP 객체 의존"]),
            "repository": ("데이터 파일·캐시 읽기/쓰기", []),
            "domain": ("순수 계산·해석", ["MCP", "파일 I/O"]),
            "infra": ("설정, 로깅, 경로, 외부 연동 어댑터", []),
        },
        "concerns": {"config": "infra", "logging": "infra"},
        "external": {"mcp": ["interface", "entrypoints"], "fastmcp": ["interface", "entrypoints"]},
    },
    "cli": {
        "title": "CLI·배치 작업",
        "detect": ["click", "typer", "schedule", "apscheduler"],
        "layers": {
            "interface": ("명령줄 인자 처리·출력 형식", ["업무 로직"]),
            "service": ("배치 작업 흐름", ["인자 파싱"]),
            "repository": ("입출력 파일·DB", []),
            "domain": ("순수 계산", ["파일·네트워크 I/O"]),
            "infra": ("설정, 로깅, 외부 클라이언트", []),
        },
        "concerns": {"config": "infra", "logging": "infra", "http": "infra", "db": "infra"},
        "external": {"click": ["interface"], "typer": ["interface"]},
    },
    "desktop": {
        "title": "데스크톱 GUI (tkinter / PyQt / PySide)",
        "detect": ["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "customtkinter"],
        "layers": {
            "interface": ("화면·위젯·이벤트 처리 (얇게)", ["업무 계산", "파일·DB 직접 접근"]),
            "service": ("화면 동작 뒤의 작업 흐름", ["GUI 위젯 의존"]),
            "repository": ("파일·DB 저장/조회", []),
            "domain": ("순수 계산", ["GUI", "I/O"]),
            "infra": ("설정, 로깅, 외부 연동", []),
        },
        "concerns": {"config": "infra", "logging": "infra"},
        "external": {
            "tkinter": ["interface"],
            "PyQt5": ["interface"],
            "PyQt6": ["interface"],
            "PySide6": ["interface"],
            "customtkinter": ["interface"],
        },
    },
    "library": {
        "title": "라이브러리 (공용 패키지)",
        "detect": [],
        "layers": {
            "interface": ("공개 API (__init__ 에서 재수출하는 함수·클래스)", ["내부 구현 세부"]),
            "service": ("여러 기능을 조합하는 상위 함수", []),
            "repository": ("파일·외부 저장소 접근", []),
            "domain": ("핵심 로직", ["전역 설정 변경", "로깅 설정"]),
            "infra": ("공통 도우미", []),
        },
        "concerns": {"logging": "entrypoints-only"},
        "external": {},
    },
}


def detect_types(external_usage: dict) -> list:
    """외부 라이브러리 사용 현황 {패키지: 사용 수} → 해당하는 유형 목록 (많이 쓰는 순)."""
    scores = {}
    for key, t in TYPES.items():
        n = sum(external_usage.get(p, 0) for p in t["detect"])
        if n:
            scores[key] = n
    return sorted(scores, key=lambda k: -scores[k]) or ["library"]


def merged(types: list) -> dict:
    """여러 유형을 합친 계층 설명·금지·관심사·외부 라이브러리 규칙."""
    layers: dict[str, dict[str, list]] = {
        name: {"responsibility": [], "must_not": []} for name in LAYER_ORDER
    }
    concerns: dict[str, Any] = {}
    external: dict[str, list] = {}
    for key in types:
        t = TYPES.get(key)
        if not t:
            continue
        for name, (resp, must_not) in t["layers"].items():
            if resp not in layers[name]["responsibility"]:
                layers[name]["responsibility"].append(resp)
            for m in must_not:
                if m not in layers[name]["must_not"]:
                    layers[name]["must_not"].append(m)
        for c, where in t["concerns"].items():
            concerns.setdefault(c, where)
        for pkg, allowed in t["external"].items():
            external.setdefault(pkg, list(allowed))
    return {"layers": layers, "concerns": concerns, "external": external}
