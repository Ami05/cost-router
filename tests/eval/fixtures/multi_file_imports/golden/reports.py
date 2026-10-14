from utils import format_date


def generate_report(data):
    return f"Report generated at {format_date(data.timestamp)}"
