"""확정된 ArchSpec 으로 새 프로젝트의 코드 골격(폴더·`__init__.py`·최소 스텁)을 생성한다."""

from __future__ import annotations

from pathlib import Path

from audit_kit.arch.layout import layer_folder
from audit_kit.arch.spec import ArchSpec
from audit_kit.textio import TextFormat, write_source

STUB_MODULE = '''"""{module} — 자동 생성된 최소 골격. 실제 로직으로 채우세요."""


def ping() -> str:
    """생성 직후 임포트·커버리지 확인용 스텁."""
    return "ok"
'''

STUB_TEST = """from {module} import ping


def test_ping() -> None:
    assert ping() == "ok"
"""

ENTRYPOINT_MODULE = '''"""{entrypoint} — 진입점. 계층을 조립해 실행한다."""


def main() -> None:
    pass


if __name__ == "__main__":
    main()
'''


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_source(path, text, TextFormat())
    return path


def generate_project(spec: ArchSpec, root: Path, package_name: str) -> list:
    """계층별 폴더·`__init__.py`·최소 스텁 모듈 + 대응 테스트, 진입점 스텁을 생성한다.

    `spec.layers[].modules` 를 실제로 만든 모듈 경로로 채운다(비어 있던 것을 여기서 채움).
    생성한 파일 경로 목록을 반환한다.
    """
    created: list = []
    src_root = root / "src" / package_name
    created.append(_write(src_root / "__init__.py", ""))
    created.append(_write(root / "tests" / "__init__.py", ""))

    for lay in spec.layers:
        folder = layer_folder(lay.name, spec.types)
        if folder is None:
            continue  # library 의 interface: 패키지 루트 자체가 공개 API
        module = f"{package_name}.{folder}.core"
        if module not in lay.modules:
            lay.modules.append(f"{package_name}.{folder}")
        created.append(_write(src_root / folder / "__init__.py", ""))
        created.append(_write(src_root / folder / "core.py", STUB_MODULE.format(module=module)))
        created.append(_write(root / "tests" / folder / "__init__.py", ""))
        created.append(
            _write(root / "tests" / folder / "test_core.py", STUB_TEST.format(module=module))
        )

    for ep in spec.entrypoints:
        rel = ep[len(package_name) + 1 :] if ep.startswith(package_name + ".") else ep
        created.append(_write(src_root / f"{rel}.py", ENTRYPOINT_MODULE.format(entrypoint=ep)))
    return created
