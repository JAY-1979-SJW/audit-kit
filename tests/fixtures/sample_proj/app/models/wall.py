from app.services.quantity import unit_price  # 순환: models -> services -> models


class Wall:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height

    def cost(self) -> float:
        return self.width * self.height * unit_price()
