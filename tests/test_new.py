"""`audit-kit new` (2단계: 새 프로젝트 설계·생성) — 6개 유형 생성, spec 왕복, concerns 조건부 생성."""

from __future__ import annotations

import tomllib

import pytest
from audit_kit.arch.ci_gen import generate_ci_and_readme
from audit_kit.arch.concerns_scaffold import generate_concerns
from audit_kit.arch.db_api_fitness import (
    generate_api_contract_fitness,
    generate_db_fitness,
)
from audit_kit.arch.pyproject_gen import generate_pyproject
from audit_kit.arch.scaffold import generate_project
from audit_kit.arch.scan import scan
from audit_kit.arch.spec import Layer, load_spec, render_spec, spec_from_scope
from audit_kit.arch.templates import TYPES
from audit_kit.arch.verify import verify_project
from audit_kit.config import AuditConfig
from audit_kit.scope import _CACHE, build_project_graph


def _generate(root, package="app", types=None, concerns=None, entrypoints=None):
    types = types or ["library"]
    entrypoints = entrypoints if entrypoints is not None else [f"{package}.main"]
    spec = spec_from_scope([package], types, concerns, entrypoints)
    generate_project(spec, root, package)
    generate_concerns(spec, root, package)
    generate_db_fitness(spec, root)
    generate_api_contract_fitness(spec, root, package)
    generate_pyproject(
        root, package, "테스트 프로젝트", types, entrypoints, has_db=bool(spec.concerns.get("db"))
    )
    generate_ci_and_readme(root, package, "테스트 프로젝트")
    (root / "architecture.toml").write_text(render_spec(spec, []), encoding="utf-8")
    _CACHE.clear()
    cfg = AuditConfig(packages=[package])
    cfg.root = root
    return spec, cfg


def _scaffold(tmp_path, name, **kw):
    """`root = tmp_path / 이름; root.mkdir(); _generate(root, ...)` 3줄이 테스트마다 반복돼
    pylint duplicate-code(2026-09-27 Stop hook 실측 확인)에 걸려서 하나로 묶었다."""
    root = tmp_path / name
    root.mkdir()
    spec, cfg = _generate(root, **kw)
    return root, spec, cfg


@pytest.mark.parametrize("type_key", sorted(TYPES))
def test_generate_all_types_zero_violations(tmp_path, type_key):
    _, spec, cfg = _scaffold(tmp_path, type_key, types=[type_key])
    graph = build_project_graph(cfg)
    violations = scan(cfg, spec, graph)
    assert violations == [], f"{type_key}: {[v.message for v in violations]}"


def test_spec_round_trip_with_standard_doc_id(tmp_path):
    spec = spec_from_scope(["app"], ["cli"])
    spec.layers[0].standard_doc_id = "DOM-01"
    (tmp_path / "architecture.toml").write_text(render_spec(spec, []), encoding="utf-8")
    loaded = load_spec(tmp_path)
    assert loaded is not None
    assert loaded.layers[0].standard_doc_id == "DOM-01"
    assert all(lay.standard_doc_id == "" for lay in loaded.layers[1:])


def test_layer_default_standard_doc_id_empty():
    assert Layer(name="x", modules=[]).standard_doc_id == ""


def test_pyproject_fields(tmp_path):
    root, _, _ = _scaffold(tmp_path, "proj", package="myapp", types=["fastapi"], concerns=["db"])
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == "myapp"
    assert "fastapi" in project["dependencies"]
    assert "sqlalchemy" in project["dependencies"]
    assert "alembic" in project["dependencies"]
    assert project["scripts"]["myapp"] == "myapp.main:main"
    assert "architecture: " in data["tool"]["pytest"]["markers"][0]


def test_entrypoints_multiple_scripts(tmp_path):
    entrypoints = ["multi.main", "multi.worker"]
    root, _, _ = _scaffold(
        tmp_path, "proj", package="multi", types=["cli"], entrypoints=entrypoints
    )
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts == {"main": "multi.main:main", "worker": "multi.worker:main"}


def test_db_concern_creates_fitness_and_alembic(tmp_path):
    root, _, _ = _scaffold(tmp_path, "withdb", package="app", types=["fastapi"], concerns=["db"])
    assert (root / "alembic.ini").is_file()
    assert (root / "migrations" / "env.py").is_file()
    assert (root / "tests" / "architecture" / "test_fitness.py").is_file()
    assert (root / "src" / "app" / "repositories" / "user_repository.py").is_file()


def test_no_db_concern_skips_fitness_and_alembic(tmp_path):
    root, _, _ = _scaffold(
        tmp_path, "nodb", package="app", types=["cli"], concerns=["config", "logging"]
    )
    assert not (root / "alembic.ini").exists()
    assert not (root / "tests" / "architecture").exists()


# ---------------------------------------------------------------- ROADMAP 4단계: DB/API 검증 도구
def test_db_concern_creates_squawk_tbls_migra_scaffolding(tmp_path):
    """db 관심사가 있으면 concerns_scaffold.py 의 기존 alembic 골격에 더해, Squawk(정적 SQL
    린트)/tbls(문서·드리프트)/migra(모델-마이그레이션 드리프트) 설정·적합성 테스트도 생긴다."""
    root, _, _ = _scaffold(tmp_path, "withdb", package="app", types=["fastapi"], concerns=["db"])
    assert (root / ".squawk.toml").is_file()
    assert (root / ".tbls.yml").is_file()
    assert (root / "tests" / "architecture" / "test_migration_lint.py").is_file()
    assert (root / "tests" / "architecture" / "test_db_docs.py").is_file()
    assert (root / "tests" / "architecture" / "test_schema_drift.py").is_file()


def test_no_db_concern_skips_squawk_tbls_migra_scaffolding(tmp_path):
    root, _, _ = _scaffold(
        tmp_path, "nodb", package="app", types=["cli"], concerns=["config", "logging"]
    )
    assert not (root / ".squawk.toml").exists()
    assert not (root / ".tbls.yml").exists()


def test_fastapi_type_creates_api_contract_test(tmp_path):
    """fastapi 는 OpenAPI 가 자동 생성되는 유일한 유형이라(templates.py TYPES) Schemathesis
    계약 테스트가 생긴다 — db 관심사와 무관하게(concerns=[] 로 db 도 제외) 유형만으로 결정."""
    root, _, _ = _scaffold(tmp_path, "api", package="app", types=["fastapi"], concerns=[])
    contract = root / "tests" / "architecture" / "test_api_contract.py"
    assert contract.is_file()
    assert "app.api.core" in contract.read_text(encoding="utf-8")
    assert not (root / ".squawk.toml").exists()  # db 관심사 없음 — 섞이지 않음


def test_non_fastapi_type_skips_api_contract_test(tmp_path):
    """fastapi 가 아닌 유형은 db 관심사가 있어도 Schemathesis(OpenAPI 계약 테스트)는 안 만든다."""
    root, _, _ = _scaffold(tmp_path, "clidb", package="app", types=["cli"], concerns=["db"])
    assert not (root / "tests" / "architecture" / "test_api_contract.py").exists()
    assert (root / ".squawk.toml").is_file()  # db 관심사는 여전히 적용됨


def test_pyproject_dev_deps_include_db_api_tools(tmp_path):
    root, _, _ = _scaffold(tmp_path, "proj", package="myapp", types=["fastapi"], concerns=["db"])
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data["project"]["optional-dependencies"]["dev"]
    assert "squawk-cli" in dev and "migradiff" in dev and "schemathesis" in dev


def test_pyproject_dev_deps_skip_db_api_tools_when_not_applicable(tmp_path):
    root, _, _ = _scaffold(tmp_path, "proj", package="myapp", types=["cli"], concerns=["config"])
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data["project"]["optional-dependencies"]["dev"]
    assert "squawk-cli" not in dev and "migradiff" not in dev and "schemathesis" not in dev


def test_library_has_no_interface_folder(tmp_path):
    root, spec, _ = _scaffold(tmp_path, "lib", package="mylib", types=["library"], entrypoints=[])
    assert not (root / "src" / "mylib" / "api").exists()
    interface = next(lay for lay in spec.layers if lay.name == "interface")
    assert interface.modules == []


def test_new_command_refuses_existing_project_dir_without_force(tmp_path, capsys):
    """cruft/cookiecutter 전환 후: `--out`은 부모 폴더, 실제 프로젝트는 `<out>/<package>`에
    생긴다. 그 하위 폴더가 이미 있으면(--force 없이는) 덮어쓰지 않고 거부해야 한다."""
    from argparse import Namespace

    from audit_kit.newproj import cmd_new

    out = tmp_path
    (out / "app").mkdir()
    (out / "app" / "이미있음.txt").write_text("x", encoding="utf-8")
    args = Namespace(
        out=str(out),
        package="app",
        types="library",
        concerns=None,
        entrypoint="main",
        description="",
        force=False,
        no_verify=True,
        full=False,
    )
    assert cmd_new(args) == 1
    assert "app" in capsys.readouterr().err


@pytest.mark.parametrize("type_key", sorted(TYPES))
def test_verify_project_end_to_end(tmp_path, type_key):
    """6개 유형 전부 arch check→ruff→mypy→pytest 가 실제로 통과하는지 확인
    (`--full`의 `pip install -e .` 는 느려서 여기 포함하지 않음 — library/cli 는 실제
    수동 실행으로 별도 확인됨, 나머지는 이 자동 테스트가 유일한 실행 증거)."""
    kw = {"entrypoints": []} if type_key == "library" else {}
    root, spec, _ = _scaffold(tmp_path, f"verify_{type_key}", package="app", types=[type_key], **kw)
    result = verify_project(root, spec, "app", full=False)
    names = [s.name for s in result.steps]
    assert names == ["arch check", "ruff", "mypy", "pytest"]
    assert result.ok, "\n".join(f"{s.name}: {s.detail}" for s in result.steps if not s.ok)


def test_generate_ci_and_readme_creates_files(tmp_path):
    root = tmp_path / "ciproj"
    root.mkdir()
    written = generate_ci_and_readme(root, "app", "설명")
    ci_path = root / ".github" / "workflows" / "ci.yml"
    readme_path = root / "README.md"
    assert ci_path in written
    assert readme_path in written
    assert ci_path.is_file()
    assert readme_path.is_file()


def test_ci_workflow_is_valid_yaml_with_expected_jobs(tmp_path):
    import yaml

    root = tmp_path / "ciproj2"
    root.mkdir()
    generate_ci_and_readme(root, "app", "")
    data = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert set(data["jobs"]) == {"test", "claude-review"}
    step_names = " ".join(s.get("run", "") for s in data["jobs"]["test"]["steps"])
    assert "ruff check" in step_names
    assert "mypy" in step_names
    assert "pytest" in step_names
    assert "bandit -r src/" in step_names
    assert "semgrep scan --config auto" in step_names
    semgrep_step = next(s for s in data["jobs"]["test"]["steps"] if "semgrep" in s.get("run", ""))
    assert semgrep_step.get("continue-on-error") is True


def test_readme_mentions_secret_requirement(tmp_path):
    root = tmp_path / "ciproj3"
    root.mkdir()
    generate_ci_and_readme(root, "app", "")
    text = (root / "README.md").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text


def test_generate_ci_and_readme_does_not_overwrite_existing(tmp_path):
    root = tmp_path / "ciproj4"
    root.mkdir()
    (root / "README.md").write_text("custom", encoding="utf-8")
    generate_ci_and_readme(root, "app", "")
    assert (root / "README.md").read_text(encoding="utf-8") == "custom"


@pytest.mark.parametrize("type_key", sorted(TYPES))
def test_new_command_generates_valid_ci_for_all_types(tmp_path, type_key):
    import yaml

    root = tmp_path / f"ci_{type_key}"
    _generate(root, package="app", types=[type_key])
    data = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert "test" in data["jobs"]
