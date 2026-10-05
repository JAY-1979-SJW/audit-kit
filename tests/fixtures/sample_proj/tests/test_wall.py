from app.models.wall import Wall


def test_cost():
    assert Wall(2, 3).cost() == 9.0


def test_fails():
    assert Wall(1, 1).cost() == 2.0
