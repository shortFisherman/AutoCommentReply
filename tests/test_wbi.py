from auto_comment_reply.wbi import derive_mixin_key, sign_wbi_params


def test_derive_mixin_key_matches_known_vector() -> None:
    img_url = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
    sub_url = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"

    assert derive_mixin_key(img_url, sub_url) == "ea1db124af3c7062474693fa704f4ff8"


def test_sign_wbi_params_is_canonical_and_filters_forbidden_characters() -> None:
    signed = sign_wbi_params(
        {"foo": "114", "bar": "514", "baz": "1919810"},
        mixin_key="ea1db124af3c7062474693fa704f4ff8",
        timestamp=1_702_204_169,
    )

    assert signed["wts"] == "1702204169"
    assert signed["w_rid"] == "6149fdadf571698ca7e6a567265cd0ee"

    filtered = sign_wbi_params({"message": "a!'()*b"}, mixin_key="0" * 32, timestamp=1)
    assert filtered["message"] == "ab"
