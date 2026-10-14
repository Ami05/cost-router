import logging

logger = logging.getLogger(__name__)


class RequestError(Exception):
    def __init__(self, message, status_code=500):
        super().__init__(message)
        self.status_code = status_code


def process_request(request):
    try:
        data = request.get_json()
        if data is None:
            raise RequestError("no data", 400)
        result = transform(data)
        save(result)
        return {"status": "ok"}, 200
    except RequestError as e:
        logger.warning("Request error: %s", e)
        return {"error": str(e)}, e.status_code
    except Exception as e:
        logger.exception("Unexpected error processing request")
        return {"error": "unexpected: " + str(e)}, 500


def transform(data):
    return {k.upper(): v for k, v in data.items()}


def save(data):
    pass
