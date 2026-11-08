from unittest import TestCase

from pocket_agent.agents.verifier import ExecutionVerifier


class ContractVerifierTests(TestCase):
    def test_failed_or_stale_observation_cannot_pass(self):
        task = {"steps": [{"status": "completed"}],
                "contract": {"postconditions": [{"path": "/enabled", "value": True}]}}
        good = {"tool_name": "android_get_ui_tree", "ok": True, "result": {"enabled": True}}
        bad = {"tool_name": "android_get_ui_tree", "ok": False, "result": {"enabled": True}}
        verifier = ExecutionVerifier()
        self.assertFalse(verifier.verify(task, [bad]).verified)
        self.assertFalse(verifier.verify(task, [good, {"tool_name": "android_tap"}]).verified)
        self.assertFalse(verifier.verify(task, [{**good, "result": {"enabled": False}}]).verified)
        self.assertTrue(verifier.verify(task, [{"tool_name": "android_tap"}, good]).verified)

    def test_observation_without_contract_is_unverified(self):
        self.assertFalse(ExecutionVerifier().verify({"steps": []}, [{"tool_name": "android_get_ui_tree", "ok": True}]).verified)
