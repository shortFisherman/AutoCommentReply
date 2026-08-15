"""WBI signing used by Bilibili's current web comment endpoint."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlparse

MIXIN_KEY_ENC_TAB = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)
_FILTERED_CHARACTERS = str.maketrans("", "", "!'()*")


def _file_stem(url: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    stem, separator, _suffix = filename.partition(".")
    if not separator or not stem:
        raise ValueError("WBI image URL does not contain a usable filename")
    return stem


def derive_mixin_key(img_url: str, sub_url: str) -> str:
    """Derive the 32-character mixin key from the nav response."""

    raw_key = _file_stem(img_url) + _file_stem(sub_url)
    if len(raw_key) <= max(MIXIN_KEY_ENC_TAB):
        raise ValueError("WBI key material is shorter than expected")
    return "".join(raw_key[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi_params(
    params: Mapping[str, Any],
    *,
    mixin_key: str,
    timestamp: int,
) -> dict[str, str]:
    """Return canonical parameters with ``wts`` and ``w_rid`` added."""

    canonical = {key: str(value).translate(_FILTERED_CHARACTERS) for key, value in params.items()}
    canonical["wts"] = str(timestamp)
    query = urlencode(sorted(canonical.items()))
    canonical["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return canonical
