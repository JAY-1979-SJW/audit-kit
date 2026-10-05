import os
import subprocess

from app.core.db import SessionLocal
from app.models import wall


def unit_price() -> float:
    return 1.5


def save_quantity(name: str, value: float) -> None:
    db = SessionLocal()
    db.add({"name": name, "value": value})


def search(keyword: str) -> list:
    with SessionLocal() as db:
        return db.execute(f"SELECT * FROM items WHERE name = '{keyword}'")


def search2(keyword: str) -> list:
    query = "SELECT * FROM items WHERE name = '" + keyword + "'"
    with SessionLocal() as db:
        return db.execute(query)


def run_cmd(cmd: str) -> None:
    subprocess.call(cmd, shell=True)


def unused_helper():
    return os.getcwd()


def total(walls: list) -> float:
    x: int = "wrong"
    return sum(w.cost() for w in walls if isinstance(w, wall.Wall))


def grade(a: int, b: int, c: int, d: int) -> str:
    r = ""
    if a > 1:
        r += "a"
    elif a > 2:
        r += "b"
    if b > 1 and c > 1:
        r += "c"
    elif b < 0 or c < 0:
        r += "d"
    for i in range(d):
        if i % 2 and a:
            r += "e"
        elif i % 3 or b:
            r += "f"
        while c > 10 and d:
            c -= 1
            if c == 5:
                break
    if d and a or b and c:
        r += "g"
    return r
