from app.domain.enums import TopicStatus


def classify_topic(
    expected_hz: float | None,
    actual_hz: float,
    last_msg_age_s: float | None,
    *,
    degraded_ratio: float,
    failed_ratio: float,
    stale_after_s: float,
) -> TopicStatus:
    """Pure topic health classification.

    - FAILED: no messages (0 Hz), last message older than `stale_after_s`, or rate below
      `failed_ratio` x expected.
    - DEGRADED: rate below `degraded_ratio` x expected.
    - HEALTHY otherwise. A topic with no expected rate is HEALTHY while it publishes.
    """
    if last_msg_age_s is not None and last_msg_age_s > stale_after_s:
        return TopicStatus.FAILED
    if actual_hz <= 0:
        return TopicStatus.FAILED
    if not expected_hz:
        return TopicStatus.HEALTHY
    ratio = actual_hz / expected_hz
    if ratio < failed_ratio:
        return TopicStatus.FAILED
    if ratio < degraded_ratio:
        return TopicStatus.DEGRADED
    return TopicStatus.HEALTHY
