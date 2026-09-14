import unittest
from pathlib import Path

import handlers


EXPECTED_HANDLER_MODULES = {
    "user",
    "admin_orders",
    "catalog",
    "categories",
    "payments",
    "admin_books",
    "admin_broadcast",
    "admin_promo",
    "admin_commands",
    "admin_texts",
    "admin_support",
}


class HandlerPackageExportsTests(unittest.TestCase):
    def test_all_production_handler_modules_are_exported(self):
        self.assertEqual(EXPECTED_HANDLER_MODULES, set(handlers.__all__))
        for name in handlers.__all__:
            self.assertTrue(hasattr(getattr(handlers, name), "router"), name)

    def test_support_router_stays_before_catch_all_user_router(self):
        main_source = Path(__file__).resolve().parents[1] / "main.py"
        source = main_source.read_text(encoding="utf-8")
        self.assertIn("dp.include_router(admin_support.router)", source)
        self.assertLess(
            source.index("dp.include_router(admin_support.router)"),
            source.index("dp.include_router(user.router)"),
        )


if __name__ == "__main__":
    unittest.main()
