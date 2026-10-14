import logging

logger = logging.getLogger(__name__)


def handle_create(data):
    logger.info("Creating resource with data: %s", data)
    validated = validate(data)
    result = db.insert(validated)
    logger.info("Created resource id=%s", result.id)
    return {"status": "created", "id": result.id}


def handle_update(data):
    validated = validate(data)
    result = db.update(validated)
    return {"status": "updated", "id": result.id}


def handle_delete(data):
    validated = validate(data)
    result = db.delete(validated)
    return {"status": "deleted", "id": result.id}


def validate(data):
    if not data:
        raise ValueError("empty data")
    return data


class db:
    @staticmethod
    def insert(data):
        return type("R", (), {"id": 1})()

    @staticmethod
    def update(data):
        return type("R", (), {"id": 2})()

    @staticmethod
    def delete(data):
        return type("R", (), {"id": 3})()
