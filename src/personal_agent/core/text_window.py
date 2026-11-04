"""UTF-8 byte windows with lossless continuation offsets."""

import codecs


def text_window(body: bytes, *, final: bool = False) -> tuple[str, int]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(body, final=final)
    used = len(body) - len(decoder.getstate()[0])
    if body and not used:
        raise ValueError("Window cannot contain a complete UTF-8 character")
    return text, used
