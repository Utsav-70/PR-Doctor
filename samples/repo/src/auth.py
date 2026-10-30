"""Unrelated module, to prove searches stay scoped."""


def login(user, password) -> bool:
    return bool(user and password)
