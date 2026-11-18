"""Request-local ownership context shared by the data access modules."""

from contextvars import ContextVar


current_user_id = ContextVar("current_user_id", default=None)


def set_current_user_id(user_id):
    return current_user_id.set(user_id)


def reset_current_user_id(token):
    current_user_id.reset(token)


def get_current_user_id():
    return current_user_id.get()
