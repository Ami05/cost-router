import re


def register_user(name, email, password):
    if not name or len(name) < 2:
        raise ValueError("invalid name")

    if not email or "@" not in email:
        raise ValueError("invalid email")
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        raise ValueError("invalid email format")

    if len(password) < 8:
        raise ValueError("password too short")

    return {"name": name, "email": email}
