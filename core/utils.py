
import datetime


def time_desc(h=None):
    """返回中文时段：深夜/清晨/上午/中午/下午/晚上"""
    h = (h or datetime.datetime.now().hour) % 24
    return (
        "深夜"
        if h < 6
        else "清晨"
        if h < 9
        else "上午"
        if h < 12
        else "中午"
        if h < 14
        else "下午"
        if h < 18
        else "晚上"
        if h < 22
        else "深夜"
    )




