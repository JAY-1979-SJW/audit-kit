"""프로그램 구조(계층·임포트 규칙) 설계 → 스캔 → 수정.

architecture.toml 이 설계의 단일 기준이다.
  spec.py  설계 파일 모델·로드·초안 추론
  scan.py  설계 대비 위반 검사
  fix.py   위반 자동 수정 (TYPE_CHECKING 이동, 심볼 하위 계층 이동, 지연 임포트)
  doc.py   ARCHITECTURE.md 생성
"""
