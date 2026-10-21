import unittest

from processwatch.challenge_gate import ChallengeKind, classify_challenge, decide_challenge_action


class ChallengeGateTests(unittest.TestCase):
    def test_classifies_supported_markers_without_retaining_raw_text(self):
        cases = [
            ("g-recaptcha-response", ChallengeKind.RECAPTCHA),
            ("hcaptcha checkbox", ChallengeKind.HCAPTCHA),
            ("cf-turnstile", ChallengeKind.TURNSTILE),
            ("Cloudflare challenge", ChallengeKind.CLOUDFLARE),
            ("Complete o CAPTCHA", ChallengeKind.CAPTCHA),
        ]
        for signal, expected in cases:
            with self.subTest(signal=signal):
                observation = classify_challenge(signal)
                self.assertEqual(observation.kind, expected)
                self.assertEqual(observation.evidence, (expected.value,))
                self.assertNotIn(signal, str(observation))

    def test_empty_or_normal_page_is_not_a_challenge(self):
        self.assertFalse(classify_challenge().detected)
        self.assertFalse(classify_challenge("process movements loaded").detected)

    def test_unknown_verification_marker_fails_closed(self):
        observation = classify_challenge("verification required")
        self.assertTrue(observation.detected)
        self.assertEqual(observation.kind, ChallengeKind.UNKNOWN)

    def test_challenge_stops_without_provider_or_retry_and_releases_worker(self):
        observation = classify_challenge("hcaptcha widget")
        decision = decide_challenge_action(observation)
        self.assertEqual(decision.status, "awaiting_access")
        self.assertIsNone(decision.provider_id)
        self.assertFalse(decision.retryable)
        self.assertTrue(decision.worker_released)

    def test_normal_observation_continues_without_provider(self):
        decision = decide_challenge_action(classify_challenge("ordinary case page"))
        self.assertEqual(decision.status, "continue")
        self.assertTrue(decision.worker_released)
        self.assertFalse(decision.retryable)


if __name__ == "__main__":
    unittest.main()

