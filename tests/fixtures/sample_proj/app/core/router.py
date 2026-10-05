"""FastAPI APIRouter 흉내."""


class Router:
    def get(self, path: str):
        def deco(fn):
            return fn

        return deco

    post = get


router = Router()
