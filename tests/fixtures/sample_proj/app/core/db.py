"""가짜 DB 계층 (외부 의존성 없이 테스트하기 위한 스텁)."""


class SessionLocal:
    def execute(self, sql: str, params: dict | None = None) -> list:
        return []

    def add(self, obj: object) -> None:
        pass

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "SessionLocal":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


global_db = SessionLocal()
