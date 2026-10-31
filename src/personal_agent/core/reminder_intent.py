"""Conservative current-message reminder intent gate; no historical permissions."""

import re

UNTRUSTED = re.compile(r'[“”"「」『』]|```')
META = re.compile(r"如果|假如|假设|例如|举例|他说|她说|你说|原话|引用|讨论|代码")
CREATE = re.compile(
    r"^(?P<before>[^。！？\n]{0,100}?)提醒我(?P<after>[^\n]{0,1200})[。！!？?]?$", re.S
)


def reminder_action(text: str) -> str | None:
    text = text.strip()
    if len(text) > 1600 or UNTRUSTED.search(text) or text.startswith("/"):
        return None
    if re.fullmatch(
        r"(?:请|帮我|请帮我)?(?:看看|查看|列出|查询)(?:一下)?(?:我的|当前|所有)?提醒[。？?]?", text
    ):
        return "list"
    if re.fullmatch(r"(?:我|现在)?(?:有|设了)(?:哪些|什么)提醒[。？?]?", text):
        return "list"
    if re.fullmatch(
        r"(?:请|帮我|请帮我)?取消(?:刚才|刚刚|上一个|最近)(?:设置的|设的|那个|的)?提醒[。！!]?",
        text,
    ):
        return "cancel_latest"
    if re.fullmatch(r"(?:不用|不要|别)(?:再)?提醒我(?:了|啦)?[。！!]?", text):
        return "cancel_ambiguous"
    if re.fullmatch(r"(?:请|帮我|请帮我)?取消提醒[。！!]?", text):
        return "cancel_ambiguous"
    if re.fullmatch(r"(?:算了|不设置了|不设了|取消设置)[。！!]?", text):
        return "abandon"
    if re.search(r"(?:不用|不要|别).{0,8}提醒我", text):
        return None
    match = CREATE.fullmatch(text)
    if match and not META.search(match["before"]):
        if re.match(r"(?:这个词|是什么意思|怎么说|的含义|是什么功能)", match["after"]):
            return None
        return "create"
    return None
