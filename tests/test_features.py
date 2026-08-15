from pathlib import Path
import hashlib
import tempfile
import threading
import unittest

import numpy as np

from camera_agent.features import DesiredFeatureState, FeatureError, FeatureId, FeatureRuntime
from camera_agent.person_guard import PersonGuard, PersonGuardConfig
from camera_agent.ptz import PTZArbiter, PTZController, PTZMove
from camera_agent.vision import PersonDetection, PersonDetector


def detection(
    *,
    confidence: float = 0.8,
    x: float = 0.5,
    y: float = 0.5,
    area: int = 100,
    face_x: float | None = None,
    face_y: float | None = None,
    cut_top: bool = False,
    cut_bottom: bool = False,
    cut_left: bool = False,
    cut_right: bool = False,
) -> PersonDetection:
    return PersonDetection(
        box=(0, 0, 10, 10),
        confidence=confidence,
        area=area,
        center_x=x,
        center_y=y,
        face_target_x=x if face_x is None else face_x,
        face_target_y=y if face_y is None else face_y,
        cut_top=cut_top,
        cut_bottom=cut_bottom,
        cut_left=cut_left,
        cut_right=cut_right,
    )


class FeatureRuntimeTests(unittest.TestCase):
    def test_feature_is_exclusive_and_generation_is_monotonic(self):
        runtime = FeatureRuntime(
            allowed={FeatureId.FACEBOOK_MONITOR, FeatureId.PERSON_GUARD},
            initial_feature=FeatureId.FACEBOOK_MONITOR,
        )

        current, changed = runtime.apply(
            DesiredFeatureState(True, FeatureId.PERSON_GUARD, 2)
        )
        self.assertTrue(changed)
        self.assertEqual(current.feature, FeatureId.PERSON_GUARD)
        self.assertFalse(runtime.apply(DesiredFeatureState(True, FeatureId.FACEBOOK_MONITOR, 1))[1])
        self.assertEqual(runtime.snapshot().feature, FeatureId.PERSON_GUARD)
        with self.assertRaisesRegex(FeatureError, "conflicting"):
            runtime.apply(DesiredFeatureState(True, FeatureId.FACEBOOK_MONITOR, 2))

    def test_none_is_safe_and_cannot_be_installed(self):
        with self.assertRaisesRegex(FeatureError, "none"):
            FeatureRuntime(allowed={FeatureId.NONE})
        runtime = FeatureRuntime(allowed={FeatureId.FACEBOOK_MONITOR})
        state, changed = runtime.apply(DesiredFeatureState(True, FeatureId.NONE, 1))
        self.assertTrue(changed)
        self.assertEqual(state.active_feature, FeatureId.NONE)


class PersonGuardTests(unittest.TestCase):
    def test_debounce_and_absence_rearm(self):
        now = [0.0]
        guard = PersonGuard(
            PersonGuardConfig(confirmation_frames=2, absence_rearm_seconds=3),
            clock=lambda: now[0],
        )

        self.assertFalse(guard.observe(detection()).present)
        present = guard.observe(detection())
        self.assertTrue(present.present)
        self.assertTrue(present.alert_event)
        now[0] = 2.9
        self.assertTrue(guard.observe(None).present)
        now[0] = 5.9
        self.assertTrue(guard.observe(None).event_cleared)
        self.assertFalse(guard.observe(detection()).alert_event)
        self.assertTrue(guard.observe(detection()).alert_event)

    def test_tracking_prefers_larger_axis_and_respects_interval(self):
        now = [0.0]
        guard = PersonGuard(
            PersonGuardConfig(confirmation_frames=1, tracking_move_interval_seconds=0.5),
            clock=lambda: now[0],
        )
        target = detection(x=0.9, y=0.7)
        guard.observe(target)
        self.assertEqual(guard.next_tracking_move(target, auto_enabled=True), PTZMove.RIGHT)
        self.assertIsNone(guard.next_tracking_move(target, auto_enabled=True))
        now[0] = 0.5
        vertical = detection(x=0.5, y=0.8, face_y=0.7)
        self.assertEqual(guard.next_tracking_move(vertical, auto_enabled=True), PTZMove.DOWN)
        self.assertIsNone(guard.next_tracking_move(vertical, auto_enabled=False))

    def test_tracking_prioritizes_top_cutoff_for_face_capture(self):
        now = [0.0]
        guard = PersonGuard(
            PersonGuardConfig(confirmation_frames=1, tracking_move_interval_seconds=0.5),
            clock=lambda: now[0],
        )
        cut_top_target = detection(x=0.5, y=0.5, cut_top=True)
        guard.observe(cut_top_target)
        self.assertEqual(guard.next_tracking_move(cut_top_target, auto_enabled=True), PTZMove.UP)

    def test_search_move_when_person_absent(self):
        now = [0.0]
        guard = PersonGuard(
            PersonGuardConfig(confirmation_frames=1, search_move_interval_seconds=2.0),
            clock=lambda: now[0],
        )

        # When auto is False, no search move
        self.assertFalse(guard.should_search_move(auto_enabled=False))

        # When auto is True and no person present, search move fires
        self.assertTrue(guard.should_search_move(auto_enabled=True))

        # Too soon for next search move
        now[0] = 1.0
        self.assertFalse(guard.should_search_move(auto_enabled=True))

        # After interval elapsed
        now[0] = 2.1
        self.assertTrue(guard.should_search_move(auto_enabled=True))

        # When person becomes present, search move stops
        guard.observe(detection())
        self.assertFalse(guard.should_search_move(auto_enabled=True))


class _Scalar:
    def __init__(self, value): self.value = value
    def __float__(self): return float(self.value)


class _XY:
    def __init__(self, value): self.value = value
    def cpu(self): return self
    def tolist(self): return self.value


class _Box:
    def __init__(self, class_id, confidence, coords):
        self.cls = [_Scalar(class_id)]
        self.conf = [_Scalar(confidence)]
        self.xyxy = [_XY(coords)]


class _FakePersonModel:
    names = {0: "person", 1: "cat"}
    def __init__(self, boxes): self.boxes = boxes
    def predict(self, **_kwargs):
        return [type("Result", (), {"boxes": self.boxes, "names": self.names})()]


class PersonDetectorTests(unittest.TestCase):
    def test_partial_person_box_is_allowed_and_largest_person_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "yolo11n.pt"
            path.write_bytes(b"verified-local-yolo")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            model = _FakePersonModel(
                [
                    _Box(0, 0.91, [0, 0, 8, 8]),
                    _Box(0, 0.46, [80, 0, 100, 40]),
                    _Box(1, 0.99, [0, 0, 100, 100]),
                ]
            )
            detector = PersonDetector(
                path, expected_sha256=digest, shared_model=model,
                prediction_lock=threading.Lock(), confidence=0.45,
            )
            result = detector.detect(np.zeros((100, 100, 3), dtype=np.uint8))

        self.assertIsNotNone(result)
        self.assertEqual(result.box, (80, 0, 100, 40))
        self.assertEqual(result.area, 800)


class _RecordingPTZ(PTZController):
    @property
    def available(self): return True
    def __init__(self): self.calls = []
    def start(self, direction, *, duration_seconds=None): self.calls.append(("start", direction, duration_seconds))
    def stop(self): self.calls.append(("stop",))


class PTZArbiterTests(unittest.TestCase):
    def test_new_owner_stops_old_motion_and_stale_owner_cannot_stop_it(self):
        controller = _RecordingPTZ()
        arbiter = PTZArbiter(controller)
        arbiter.start("facebook_patrol", PTZMove.LEFT)
        arbiter.start("person_guard", PTZMove.RIGHT, duration_seconds=0.25)

        self.assertFalse(arbiter.stop("facebook_patrol"))
        self.assertTrue(arbiter.stop("person_guard"))
        self.assertEqual(
            controller.calls,
            [("start", PTZMove.LEFT, None), ("stop",), ("start", PTZMove.RIGHT, 0.25), ("stop",)],
        )


if __name__ == "__main__":
    unittest.main()
