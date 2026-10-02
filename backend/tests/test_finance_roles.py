import unittest

from app.core.permissions import DEFAULT_ROLES, Permission


class FinanceRolesTests(unittest.TestCase):
    def test_separate_roles_and_no_knowledge_manager(self):
        roles = {role.code: role for role in DEFAULT_ROLES}
        self.assertNotIn("knowledge_manager", roles)
        self.assertEqual(roles["finance"].name, "财务")
        self.assertEqual(roles["cashier"].name, "出纳")
        self.assertIn(Permission.PROFIT_MANAGE, roles["finance"].permissions)
        self.assertIn(Permission.FINANCE_MANAGE, roles["finance"].permissions)
        self.assertIn(Permission.ORDER_PAYMENT, roles["cashier"].permissions)
        self.assertNotIn(Permission.PROFIT_VIEW, roles["cashier"].permissions)
        self.assertNotIn(Permission.PROFIT_MANAGE, roles["cashier"].permissions)
        self.assertNotIn(Permission.FINANCE_MANAGE, roles["cashier"].permissions)
