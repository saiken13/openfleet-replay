from enum import StrEnum


class RobotStatus(StrEnum):
    """Derived by OpenFleet from liveness and open incidents; never set by the robot."""

    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class TopicStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class NodeState(StrEnum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    RUN_ENDED = "run_ended"
    TOPIC_STATUS_CHANGED = "topic_status_changed"
    NODE_STATE_CHANGED = "node_state_changed"
    LOG = "log"
    ROBOT_STATUS_CHANGED = "robot_status_changed"
    SOFTWARE_VERSION_CHANGED = "software_version_changed"
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_ESCALATED = "incident_escalated"
    INCIDENT_RESOLVED = "incident_resolved"


class EventSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Severity(StrEnum):
    """Incident severity. Ordered: INFO < WARNING < CRITICAL."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"
