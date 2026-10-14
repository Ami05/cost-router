from datetime import datetime


def format_date(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def render_dashboard(user):
    return f"Welcome {user.name}, last login: {format_date(user.last_login)}"


def render_profile(user):
    return f"Profile: {user.name}, joined: {format_date(user.created_at)}"
