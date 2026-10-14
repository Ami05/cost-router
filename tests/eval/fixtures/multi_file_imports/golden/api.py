from utils import format_date


def api_response(record):
    return {"created": format_date(record.created_at), "id": record.id}
