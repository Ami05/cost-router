from datetime import datetime

from utils import format_date


def render_dashboard(user):
    return f"Welcome {user.name}, last login: {format_date(user.last_login)}"


def render_profile(user):
    return f"Profile: {user.name}, joined: {format_date(user.created_at)}"
