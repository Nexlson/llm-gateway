from app.core.errors import error_envelope, GatewayError, AuthError, BadRequestError


def test_envelope_shape():
    env = error_envelope("boom", "invalid_request_error", param="model", code="x")
    assert env == {
        "error": {"message": "boom", "type": "invalid_request_error",
                  "param": "model", "code": "x"}
    }


def test_auth_error_defaults():
    e = AuthError()
    assert e.status_code == 401 and e.error_type == "authentication_error"


def test_bad_request_carries_param():
    e = BadRequestError("no model", param="model")
    assert e.status_code == 400 and e.param == "model"
    assert isinstance(e, GatewayError)
