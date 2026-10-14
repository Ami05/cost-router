import json


def read_config(path):
    with open(path) as f:
        return json.load(f)


def slugify(text):
    return text.lower().replace(" ", "-").replace("_", "-")


def truncate(text, max_len=100):
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
