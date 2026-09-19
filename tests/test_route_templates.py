import tempfile
import unittest
import uuid
from pathlib import Path

from controlplane.route_templates import (
    RouteTemplateError,
    render_caddy_route,
    tenant_socket,
)


class RouteTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "runtime"
        self.tenant_id = str(uuid.uuid4())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_renders_validated_hosts_to_uuid_derived_socket(self):
        route = render_caddy_route(
            self.root,
            tenant_id=self.tenant_id,
            hosts=["store.example.test", "custom.example.test"],
        )
        self.assertTrue(route.startswith("custom.example.test store.example.test {\n"))
        self.assertIn("reverse_proxy unix//", route)
        self.assertIn(tenant_socket(self.root, self.tenant_id).as_posix(), route)
        self.assertNotIn("@tenant_", route)
        self.assertNotIn("..", route)

    def test_rejects_untrusted_hosts_and_tenant_ids(self):
        with self.assertRaises(RouteTemplateError):
            render_caddy_route(
                self.root,
                tenant_id=self.tenant_id,
                hosts=["store.example.test { respond 200 }"],
            )
        with self.assertRaises(RouteTemplateError):
            tenant_socket(self.root, "../../etc")


if __name__ == "__main__":
    unittest.main()
