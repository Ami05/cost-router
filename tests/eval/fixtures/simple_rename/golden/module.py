MAX_RETRIES = 3


def calc_total(items):
    output = 0
    for item in items:
        output += item.price * item.quantity
    return output


def process_data(raw_input):
    output = parse(raw_input)
    output = validate(output)
    return output


def send_request(url, timeout_ms=5000):
    return http_get(url, timeout=timeout_ms)


class Router:
    def __init__(self):
        self.data = {}

    def add_route(self, path, handler):
        self.data[path] = handler

    def resolve(self, path):
        return self.data.get(path)


def parse(raw):
    return raw.strip().split(",")


def validate(data):
    return [d for d in data if d]


def http_get(url, timeout=5000):
    return {"status": 200, "url": url, "timeout": timeout}
