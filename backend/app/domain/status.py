from collections.abc import Iterable

from app.domain.enums import RobotStatus, Severity


def derive_robot_status(is_live: bool, open_severities: Iterable[Severity]) -> RobotStatus:
    """OFFLINE if not live; ERROR if a CRITICAL incident is open; DEGRADED if a WARNING one is;
    otherwise ONLINE. INFO incidents don't affect status."""
    if not is_live:
        return RobotStatus.OFFLINE
    severities = set(open_severities)
    if Severity.CRITICAL in severities:
        return RobotStatus.ERROR
    if Severity.WARNING in severities:
        return RobotStatus.DEGRADED
    return RobotStatus.ONLINE
