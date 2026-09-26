import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied
from src.repository import Repository
from src.service import Service

PLAN = {"steps": "开启泄洪闸并加密监测", "evacuation_scope": "下游500米内村庄"}

def make_item(service, **over):
    payload = {"title": "渗流异常", "description": "渗流量突增超控制值",
               "severity": "major", "quantity": 12, "threshold": 6}
    payload.update(over)
    return service.create_item(payload, "creator", "inspector")

class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # 达到阈值的缺陷，推进到 defect_confirmed，等待转入 repair
        item = make_item(self.service)
        cur = self.service.transition(item["id"], "inspected", item["version"], "insp1", "inspector")
        self.cur = self.service.transition(cur["id"], "defect_confirmed", cur["version"], "eng1", "dam_engineer")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def to_repair(self):
        return self.service.transition(self.cur["id"], "repair", self.cur["version"], "eng1", "dam_engineer")

    def confirm_both(self):
        self.service.confirm_plan(self.cur["id"], "insp1", "inspector")
        return self.service.confirm_plan(self.cur["id"], "eng1", "dam_engineer")

    def test_threshold_item_blocked_until_plan_fully_confirmed(self):
        with self.assertRaises(ConflictError) as ctx:
            self.to_repair()
        self.assertIn("尚未制定应急预案", str(ctx.exception))
        status = self.service.save_plan(self.cur["id"], PLAN, "mgr", "emergency_manager")
        self.assertEqual(status["plan"]["version"], 1)
        self.assertEqual(set(status["plan"]["pending_roles"]), {"inspector", "dam_engineer"})
        with self.assertRaises(ConflictError):
            self.to_repair()
        status = self.service.confirm_plan(self.cur["id"], "insp1", "inspector")
        self.assertEqual(status["plan"]["pending_roles"], ["dam_engineer"])
        with self.assertRaises(ConflictError) as ctx:
            self.to_repair()
        self.assertIn("坝工", str(ctx.exception))
        status = self.service.confirm_plan(self.cur["id"], "eng1", "dam_engineer")
        self.assertTrue(status["plan"]["fully_confirmed"])
        self.assertEqual(status["plan"]["blockers"], [])
        repaired = self.to_repair()
        self.assertEqual(repaired["status"], "repair")

    def test_revision_invalidates_old_confirmations(self):
        self.service.save_plan(self.cur["id"], PLAN, "mgr", "emergency_manager")
        self.confirm_both()
        status = self.service.save_plan(
            self.cur["id"], {"steps": "加高子堰", "evacuation_scope": "下游1公里"}, "mgr", "emergency_manager")
        self.assertEqual(status["plan"]["version"], 2)
        self.assertFalse(status["plan"]["fully_confirmed"])
        self.assertEqual(set(status["plan"]["pending_roles"]), {"inspector", "dam_engineer"})
        self.assertEqual(status["plan"]["confirmations"], [])
        with self.assertRaises(ConflictError) as ctx:
            self.to_repair()
        self.assertIn("v2", str(ctx.exception))
        self.confirm_both()
        self.assertEqual(self.to_repair()["status"], "repair")

    def test_normal_item_follows_original_flow(self):
        item = make_item(self.service, severity="minor", quantity=1, threshold=10)
        cur = self.service.transition(item["id"], "inspected", item["version"], "insp1", "inspector")
        cur = self.service.transition(cur["id"], "defect_confirmed", cur["version"], "eng1", "dam_engineer")
        self.assertFalse(cur["escalation_required"])
        self.assertEqual(cur["plan"]["blockers"], [])
        repaired = self.service.transition(cur["id"], "repair", cur["version"], "eng1", "dam_engineer")
        self.assertEqual(repaired["status"], "repair")

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.save_plan(self.cur["id"], PLAN, "x", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.save_plan(self.cur["id"], PLAN, "x", "viewer")
        with self.assertRaises(NotFoundError):
            self.service.confirm_plan(self.cur["id"], "insp1", "inspector")
        self.service.save_plan(self.cur["id"], PLAN, "mgr", "emergency_manager")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_plan(self.cur["id"], "mgr", "emergency_manager")
        with self.assertRaises(Exception):
            self.service.save_plan(self.cur["id"], {"steps": "", "evacuation_scope": "x"}, "mgr", "emergency_manager")

    def test_duplicate_confirm_is_idempotent(self):
        self.service.save_plan(self.cur["id"], PLAN, "mgr", "emergency_manager")
        first = self.service.confirm_plan(self.cur["id"], "insp1", "inspector")
        again = self.service.confirm_plan(self.cur["id"], "insp1", "inspector")
        self.assertEqual(len(again["plan"]["confirmations"]), 1)
        self.assertEqual(again["plan"]["pending_roles"], ["dam_engineer"])
        confirms = [e for e in self.service.audit("viewer", self.cur["id"]) if e["action"] == "plan_confirm"]
        self.assertEqual(len(confirms), 1)
        self.assertEqual(first["plan"]["version"], again["plan"]["version"])

    def test_audit_trail_for_save_confirm_and_release(self):
        self.service.save_plan(self.cur["id"], PLAN, "mgr", "emergency_manager")
        self.confirm_both()
        self.to_repair()
        events = self.service.audit("viewer", self.cur["id"])
        actions = [e["action"] for e in events]
        self.assertIn("plan_save", actions)
        self.assertEqual(actions.count("plan_confirm"), 2)
        release = [e for e in events if e["action"] == "transition" and e["detail"].get("to") == "repair"]
        self.assertEqual(release[0]["detail"]["plan_version"], 1)
        self.assertTrue(self.repo.verify_audit_chain())

if __name__ == "__main__":
    unittest.main()
