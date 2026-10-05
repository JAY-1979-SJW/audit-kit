# audit-kit std 검증 스크립트

`audit-kit std` 의 정밀도(오탐)와 재현율(미탐)을 다시 측정하는 스크립트. 결과와 결론은 `docs/검증결과_std.md`.
검증 대상 폴더 목록(`roots.txt`·`hosts_from.txt`)은 PC 마다 다르므로 저장소에는 예시만 있다. 본인 경로는 같은 폴더의 `roots.local.txt`·`hosts_from.local.txt`(.gitignore 대상)에 한 줄에 하나씩 적는다.

| 파일 | 하는 일 | 실행 |
|---|---|---|
| `corpus.py` | 실제 프로젝트 여러 개에 std 를 돌려 조항별 발견을 JSON 으로 저장 (리포트 파일은 만들지 않음) | `python corpus.py out.json` |
| `sample.py` | 그 JSON 에서 조항별 표본을 코드 문맥과 함께 출력 → 사람이 진짜 위반인지 판정 | `python sample.py out.json EFF-03 7` |
| `mutate.py` | 실제 파일 뒤에 조항별 결함(21종)을 심고 놓치는 것을 센다 (재현율) | `python mutate.py <임시폴더> 25` |

같은 사람이 만든 결함으로 재현율을 재므로 낙관적일 수 있다. `tests/test_std_variants.py` 가 다른 모양의 변형을 별도로 확인한다.
