from app.routing.features import RequestFeatures, estimate_tokens, extract_features


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1          # 4 chars ≈ 1 token
    assert estimate_tokens("abcde") == 2         # ceil(5/4)


def test_extract_no_tools_short():
    f = extract_features(
        {"model": "x", "messages": [{"role": "user", "content": "hello"}]},
        headers={},
    )
    assert isinstance(f, RequestFeatures)
    assert f.has_tools is False
    assert f.input_tokens == estimate_tokens("hello")
    assert f.system_prompt == ""


def test_extract_detects_tools_and_functions():
    assert extract_features({"tools": [{"type": "function"}]}, {}).has_tools is True
    assert extract_features({"functions": [{"name": "f"}]}, {}).has_tools is True
    assert extract_features({"tools": []}, {}).has_tools is False


def test_extract_system_prompt_and_headers_lowercased():
    f = extract_features(
        {"messages": [
            {"role": "system", "content": "Translate to French"},
            {"role": "user", "content": "hi"},
        ]},
        headers={"X-Pool": "cheap"},
    )
    assert f.system_prompt == "Translate to French"
    assert f.headers == {"x-pool": "cheap"}


def test_extract_handles_list_content_parts():
    f = extract_features(
        {"messages": [{"role": "user",
                       "content": [{"type": "text", "text": "abcd"},
                                   {"type": "image_url", "image_url": {"url": "x"}}]}]},
        headers={},
    )
    assert f.input_tokens == estimate_tokens("abcd")


def test_last_user_text_is_final_user_message():
    body = {"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second and last"},
    ]}
    f = extract_features(body, {})
    assert f.last_user_text == "second and last"


def test_last_user_text_empty_when_no_user_message():
    f = extract_features({"messages": [{"role": "system", "content": "sys"}]}, {})
    assert f.last_user_text == ""


def test_last_user_text_defaults_on_direct_construction():
    f = RequestFeatures(input_tokens=0, has_tools=False, system_prompt="", headers={})
    assert f.last_user_text == ""
