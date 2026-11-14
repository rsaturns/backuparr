"""A request's JSON body, for the routes that take an object."""
from flask import abort, jsonify, request


def json_object():
    """The request's JSON body as a dict, {} when there's none. Anything
    else - a list, a number, a string - ends the request with 400 "expected
    a JSON object" (#226), so a route can call .get() on what it gets."""
    data = request.get_json(force=True, silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        response = jsonify({"error": "expected a JSON object"})
        response.status_code = 400
        abort(response)
    return data
