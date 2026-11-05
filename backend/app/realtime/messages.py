"""Builders for realtime messages. Shapes:

{"type": "robot",    "data": RobotRead}      channels: fleet, robot:<id>
{"type": "incident", "data": IncidentRead}   channels: fleet, robot:<id>
{"type": "frame",    "data": Frame}          channel:  robot:<id>
"""

from typing import Any

from app.db.models import Event, Incident, Robot, TelemetrySample, TopicSample
from app.realtime.hub import FLEET, Message, robot_channel
from app.schemas.incident import IncidentRead
from app.schemas.robot import RobotRead
from app.schemas.telemetry import EventRead, Frame, SampleRead, TopicSampleRead

Channels = tuple[str, ...]


def robot_message(robot: Robot) -> tuple[Channels, Message]:
    data = RobotRead.model_validate(robot).model_dump(mode="json")
    return (FLEET, robot_channel(robot.id)), {"type": "robot", "data": data}


def incident_message(incident: Incident) -> tuple[Channels, Message]:
    data = IncidentRead.model_validate(incident).model_dump(mode="json")
    return (FLEET, robot_channel(incident.robot_id)), {"type": "incident", "data": data}


def frame_for(row: TelemetrySample | TopicSample | Event) -> Frame:
    if isinstance(row, TelemetrySample):
        return Frame(kind="sample", ts=row.ts, data=SampleRead.model_validate(row))
    if isinstance(row, TopicSample):
        return Frame(kind="topic", ts=row.ts, data=TopicSampleRead.model_validate(row))
    return Frame(kind="event", ts=row.ts, data=EventRead.model_validate(row))


def frame_message(row: TelemetrySample | TopicSample | Event) -> tuple[Channels, Message]:
    data: dict[str, Any] = frame_for(row).model_dump(mode="json")
    return (robot_channel(row.robot_id),), {"type": "frame", "data": data}
