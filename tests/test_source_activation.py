"""No retired activation switch can invoke a second planner or alter state."""
import unittest
from app.source_admin import activate_sources


class RetiredActivationTests(unittest.TestCase):
    def test_activation_refuses_before_accessing_store_runtime_or_plan(self):
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, 'retired'):
                activate_sources(None, None, plan_path='/fictional/not-read', apply=apply)
