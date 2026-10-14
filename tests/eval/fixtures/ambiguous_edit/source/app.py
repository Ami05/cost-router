def process_request(request):
    try:
        data = request.get_json()
        if data is None:
            return {"error": "no data"}, 400
        try:
            result = transform(data)
        except Exception as e:
            return {"error": str(e)}, 500
        try:
            save(result)
        except Exception as e:
            return {"error": "save failed: " + str(e)}, 500
        return {"status": "ok"}, 200
    except Exception as e:
        return {"error": "unexpected: " + str(e)}, 500


def transform(data):
    return {k.upper(): v for k, v in data.items()}


def save(data):
    pass
