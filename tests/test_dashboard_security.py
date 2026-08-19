import os
import unittest
from unittest.mock import patch

from brain.dashboard.api.security import (
    configured_dashboard_token,
    dashboard_request_authorized,
    is_loopback_client,
    is_cross_site_mutation,
    is_private_client,
)


class DashboardSecurityTests(unittest.TestCase):
    def test_cross_site_browser_mutations_are_rejected(self):
        self.assertTrue(is_cross_site_mutation("POST", "cross-site"))
        self.assertFalse(is_cross_site_mutation("GET", "cross-site"))
        self.assertFalse(is_cross_site_mutation("POST", "same-origin"))
        self.assertFalse(is_cross_site_mutation("POST", None))

    def test_loopback_detection_supports_ipv4_ipv6_and_mapped_addresses(self):
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::1"))
        self.assertTrue(is_loopback_client("::ffff:127.0.0.1"))
        self.assertTrue(is_loopback_client("localhost"))
        self.assertFalse(is_loopback_client("192.0.2.10"))
        self.assertFalse(is_loopback_client(None))

    def test_remote_dashboard_requests_fail_closed_without_a_token(self):
        self.assertFalse(
            dashboard_request_authorized(
                client_host="192.0.2.10",
                configured_token="",
                presented_token=None,
            )
        )

    def test_remote_dashboard_requests_require_the_exact_token(self):
        self.assertTrue(
            dashboard_request_authorized(
                client_host="192.0.2.10",
                configured_token="correct",
                presented_token="correct",
            )
        )
        self.assertFalse(
            dashboard_request_authorized(
                client_host="192.0.2.10",
                configured_token="correct",
                presented_token="wrong",
            )
        )

    def test_private_lan_requests_do_not_need_a_token(self):
        self.assertTrue(is_private_client("192.168.50.194"))
        self.assertTrue(
            dashboard_request_authorized(
                client_host="192.168.50.194",
                configured_token="",
                presented_token=None,
            )
        )

    def test_tailscale_cgnat_requests_do_not_need_a_token(self):
        self.assertTrue(is_private_client("100.106.68.80"))
        self.assertTrue(
            dashboard_request_authorized(
                client_host="100.106.68.80",
                configured_token="",
                presented_token=None,
            )
        )

    def test_reserved_documentation_range_is_not_trusted_lan(self):
        self.assertFalse(is_private_client("192.0.2.10"))

    def test_loopback_dashboard_requests_do_not_need_the_remote_token(self):
        self.assertTrue(
            dashboard_request_authorized(
                client_host="127.0.0.1",
                configured_token="configured",
                presented_token=None,
            )
        )

    def test_environment_token_overrides_tracked_config(self):
        with patch.dict(
            os.environ,
            {"CRAZY_AUDIOBOOK_DASHBOARD_TOKEN": "from-environment"},
            clear=False,
        ):
            self.assertEqual(
                configured_dashboard_token({"api_token": "from-yaml"}),
                "from-environment",
            )


if __name__ == "__main__":
    unittest.main()
