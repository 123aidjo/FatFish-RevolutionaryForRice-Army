from datetime import datetime, timedelta, timezone

# 每日限额按用户所在的北京时间切日。
CHINA_TIME = timezone(timedelta(hours=8))


def start_of_day(now: float) -> float:
    return datetime.fromtimestamp(now, CHINA_TIME).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
