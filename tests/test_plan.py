import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class PlanGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # 达到阈值的缺陷（quantity >= threshold）
        self.item = self.service.create_item(
            {"title": "seepage anomaly", "description": "seepage over threshold",
             "severity": "major", "quantity": 12, "threshold": 6,
             "external_ref": "PLAN-1"}, "creator", "inspector")
        # 普通缺陷（未达阈值）
        self.normal = self.service.create_item(
            {"title": "minor crack", "description": "below threshold",
             "severity": "minor", "quantity": 1, "threshold": 10,
             "external_ref": "PLAN-2"}, "creator", "inspector")
        for it in (self.item, self.normal):
            current = it
            for target in ("inspected", "defect_confirmed"):
                current = self.service.transition(
                    current["id"], target, current["version"], "reviewer",
                    TRANSITION_ROLES[target][0])
        self.item = self.service.get_item(self.item["id"], "viewer")
        self.normal = self.service.get_item(self.normal["id"], "viewer")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _save_plan(self, steps="降低库水位并回填反滤料", scope="下游500米范围"):
        return self.service.save_plan(
            self.item["id"], {"steps": steps, "evacuation_scope": scope},
            "manager", "emergency_manager")

    def test_gate_requires_plan_and_dual_confirmation(self):
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.item["id"], "repair", self.item["version"],
                                    "eng1", "dam_engineer")
        self.assertIn("尚未保存应急预案", str(ctx.exception))

        summary = self._save_plan()
        self.assertEqual(summary["version"], 1)
        self.assertEqual(sorted(summary["pending_roles"]), ["dam_engineer", "inspector"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.item["id"], "repair", self.item["version"],
                                    "eng1", "dam_engineer")
        self.assertIn("巡检员未确认", str(ctx.exception))
        self.assertIn("坝工未确认", str(ctx.exception))

        summary = self.service.confirm_plan(self.item["id"], "insp1", "inspector")
        self.assertEqual(summary["pending_roles"], ["dam_engineer"])
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(self.item["id"], "insp1", "inspector")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.item["id"], "repair", self.item["version"],
                                    "eng1", "dam_engineer")
        self.assertIn("坝工未确认", str(ctx.exception))

        summary = self.service.confirm_plan(self.item["id"], "eng1", "dam_engineer")
        self.assertTrue(summary["complete"])
        updated = self.service.transition(self.item["id"], "repair",
                                          self.item["version"], "eng1", "dam_engineer")
        self.assertEqual(updated["status"], "repair")

        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        self.assertIn("plan_save", actions)
        self.assertEqual(actions.count("plan_confirm"), 2)
        self.assertIn("release", actions)
        release = [e for e in self.service.audit("viewer", self.item["id"])
                   if e["action"] == "release"][0]
        self.assertEqual(release["detail"]["plan_version"], 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_revision_invalidates_old_confirmations(self):
        self._save_plan()
        self.service.confirm_plan(self.item["id"], "insp1", "inspector")
        self.service.confirm_plan(self.item["id"], "eng1", "dam_engineer")
        summary = self._save_plan(steps="更新后的处置步骤")
        self.assertEqual(summary["version"], 2)
        self.assertEqual(sorted(summary["pending_roles"]), ["dam_engineer", "inspector"])
        self.assertFalse(summary["complete"])
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"], "repair", self.item["version"],
                                    "eng1", "dam_engineer")
        self.service.confirm_plan(self.item["id"], "insp1", "inspector")
        self.service.confirm_plan(self.item["id"], "eng1", "dam_engineer")
        updated = self.service.transition(self.item["id"], "repair",
                                          self.item["version"], "eng1", "dam_engineer")
        self.assertEqual(updated["status"], "repair")
        releases = [e for e in self.service.audit("viewer", self.item["id"])
                    if e["action"] == "release"]
        self.assertEqual(releases[0]["detail"]["plan_version"], 2)

    def test_normal_defect_follows_original_flow(self):
        updated = self.service.transition(
            self.normal["id"], "repair", self.normal["version"], "eng1", "dam_engineer")
        self.assertEqual(updated["status"], "repair")
        actions = [e["action"] for e in self.service.audit("viewer", self.normal["id"])]
        self.assertNotIn("release", actions)

    def test_plan_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.save_plan(self.item["id"], {"steps": "s", "evacuation_scope": "e"},
                                   "insp1", "inspector")
        self._save_plan()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_plan(self.item["id"], "manager", "emergency_manager")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_plan(self.item["id"], "guest", "viewer")

    def test_item_view_shows_plan_progress_and_blockers(self):
        view = self.service.get_item(self.item["id"], "viewer")
        self.assertIsNone(view["plan"]["version"])
        self.assertIn("尚未保存应急预案", view["repair_blockers"])
        self._save_plan()
        self.service.confirm_plan(self.item["id"], "insp1", "inspector")
        view = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(view["plan"]["version"], 1)
        self.assertEqual(view["plan"]["pending_roles"], ["dam_engineer"])
        self.assertEqual(view["plan"]["confirmations"][0]["actor"], "insp1")
        self.assertEqual(view["repair_blockers"], ["坝工未确认当前预案版本"])


if __name__ == "__main__":
    unittest.main()
