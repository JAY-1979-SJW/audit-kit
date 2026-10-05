"""ArchSpec 의 계층 이름 → 실제 물리 폴더 이름 매핑.

계층 '이름'은 항상 5개 고정(templates.LAYER_ORDER)이지만, 물리 폴더 이름은 프로젝트
유형에 따라 다르다(예: interface 계층은 fastapi 면 `api`, cad 면 `plugin`).
`init_project.LAYER_CANDIDATES` 의 마지막 단계(core/db/database/...)와 맞추기 위해
infra 계층의 기본 폴더 이름은 `core` 로 둔다.
"""

from __future__ import annotations

# 유형별 interface 계층의 물리 폴더 이름. 목록에 없으면(library) 별도 폴더를 두지 않고
# 패키지 루트(`__init__.py`)를 공개 API 로 취급한다.
INTERFACE_FOLDER: dict[str, str] = {
    "fastapi": "api",
    "cad": "plugin",
    "mcp": "tools",
    "cli": "cli",
    "desktop": "ui",
}

# interface 를 제외한 나머지 계층은 유형과 무관하게 같은 폴더 이름을 쓴다.
LAYER_FOLDER: dict[str, str] = {
    "service": "services",
    "repository": "repositories",
    "domain": "domain",
    "infra": "core",
}


def interface_folder(types: list) -> str | None:
    """여러 유형 중 처음으로 매칭되는 interface 폴더 이름. 전부 library(또는 미지정)면 None."""
    for t in types:
        folder = INTERFACE_FOLDER.get(t)
        if folder:
            return folder
    return None


def layer_folder(layer_name: str, types: list) -> str | None:
    """계층 이름 + 유형 목록 → 물리 폴더 이름. interface 이고 library 뿐이면 None(폴더 없음)."""
    if layer_name == "interface":
        return interface_folder(types)
    return LAYER_FOLDER.get(layer_name, layer_name)
