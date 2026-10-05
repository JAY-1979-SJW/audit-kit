# 기여 가이드

지금은 단일 관리자(JAY-1979-SJW) 저장소지만, 팀이 커질 것을 대비해 미리 정한 절차다.
audit-kit은 다른 프로젝트들이 기준서 검사(`audit-kit std`)에 의존하므로, 기존 검사
동작을 바꾸는 변경은 특히 신중하게 리뷰한다.

## 이슈 등록

버그/기능 요청은 GitHub Issues로 등록한다. 어느 조항(rules.toml ID)이나 명령에
영향을 주는지 구체적으로 적는다.

## PR 절차

1. 브랜치를 나눠 작업한다(`main`에 직접 커밋하지 않는다)
2. 커밋 메시지는 Conventional Commits 형식을 따른다
3. `CODEOWNERS`에 지정된 담당자의 리뷰를 받는다
4. `tests/` 전체(pytest)와 `audit-kit std --fail-on critical` 자기 검사를 통과해야 한다

## 코딩 표준

`32. Claude 개발표준`의 기준서(`rules.toml`)를 따른다. `audit-kit std`로 검사할 수 있다.
