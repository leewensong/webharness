"""Presence long-sequence animation protocol regressions."""

import unittest

from app import main


class PresenceAnimationTests(unittest.TestCase):
    def _body(self, animation):
        return main.PresenceUpdate(
            level=3,
            p=[1.2, 1.6, -0.4],
            yaw=1.57,
            animation=animation,
        )

    def test_animation_keyframes_are_normalized_and_carried_in_binary_frame(self):
        body = self._body({
            "action": "replace",
            "id": "wave-001",
            "keyframes": [
                {"t": 0, "bones": {"rightUpperArm": [0, 0, 0, 2]}},
                {"t": 0.5, "bones": {"rightUpperArm": [0, 0.2, 0, 0.98]},
                 "face": {"mouthSmileLeft": 0.8}},
            ],
        })
        pose = main._presence_validate(body)
        self.assertEqual(pose["animation"]["action"], "replace")
        self.assertEqual(pose["animation"]["keyframes"][0]["bones"]["rightUpperArm"], [0.0, 0.0, 0.0, 1.0])
        self.assertAlmostEqual(pose["animation"]["keyframes"][1]["face"]["mouthSmileLeft"], 0.8)

        mask = main._presence_dirty(None, pose)
        self.assertTrue(mask & main.P_DIRTY_ANIMATION)
        frame = main._presence_frame_bytes([(7, 0, mask, pose)])
        self.assertEqual(frame[:3], bytes([main.PRESENCE_BIN_MAGIC, main.PRESENCE_BIN_VERSION, 1]))
        self.assertIn(b'"action":"replace"', frame)
        self.assertIn(b'"keyframes"', frame)

    def test_animation_commands_are_not_coalesced_with_ordinary_presence(self):
        previous = {"p": [0, 1.6, 0], "yaw": 0, "pitch": 0, "hands": [], "state": None,
                    "bones": {}, "face": {}}
        current = dict(previous)
        current["animation"] = {"action": "stop"}
        self.assertTrue(main._presence_dirty(previous, current) & main.P_DIRTY_ANIMATION)

    def test_animation_requires_level_three(self):
        with self.assertRaises(main.HTTPException):
            main._presence_level(main.PresenceUpdate(
                p=[0, 1.6, 0],
                animation={"action": "stop"},
            ))

    def test_animation_requires_strictly_increasing_times(self):
        body = self._body({
            "action": "replace",
            "keyframes": [
                {"t": 0.2, "bones": {"head": [0, 0, 0, 1]}},
                {"t": 0.2, "bones": {"head": [0, 0.1, 0, 0.99]}},
            ],
        })
        with self.assertRaises(main.HTTPException):
            main._presence_validate(body)

    def test_hand_handedness_survives_validation_and_binary_packing(self):
        body = main.PresenceUpdate(
            level=2,
            p=[0, 1.6, 0],
            hands=[
                {"handedness": "left", "p": [-0.3, 1.2, -0.4], "q": [0, 0, 0, 1]},
                {"handedness": "right", "p": [0.3, 1.2, -0.4], "q": [0, 0, 0, 1]},
            ],
        )
        pose = main._presence_validate(body)
        self.assertEqual([h["handedness"] for h in pose["hands"]], ["left", "right"])
        frame = main._presence_frame_bytes([(7, 0, main.P_DIRTY_HANDS, pose)])
        # Header 3B + entry 6B + count 1B, then left side code 1 and right side code 2.
        self.assertEqual(frame[10], 1)
        self.assertEqual(frame[25], 2)

    def test_palm_orientation_tag_survives_existing_state_transport(self):
        body = main.PresenceUpdate(
            level=2, p=[0, 1.6, 0], state={"handOrientation": "palm-v1"},
            hands=[{"handedness": "right", "p": [.3, 1.2, -.4], "q": [0, 0, 0, 1]}],
        )
        pose = main._presence_validate(body)
        self.assertEqual(pose["state"], {"handOrientation": "palm-v1"})
        wire = main._presence_frame_bytes([(7, 0, main.P_DIRTY_HANDS | main.P_DIRTY_STATE, pose)])
        self.assertIn(b'"handOrientation":"palm-v1"', wire)

    def test_hand_sample_repeats_frame_tag_after_presence_coalescing(self):
        previous = main._presence_validate(main.PresenceUpdate(
            p=[0, 1.6, 0], state={"handOrientation": "palm-v1", "handSample": 1},
            hands=[{"handedness": "left", "p": [-.3, 1.2, -.4], "q": [0, 0, 0, 1]}],
        ))
        current = {**previous, "state": {"handOrientation": "palm-v1", "handSample": 2}}
        self.assertTrue(main._presence_dirty(previous, current) & main.P_DIRTY_STATE)


if __name__ == "__main__":
    unittest.main()
