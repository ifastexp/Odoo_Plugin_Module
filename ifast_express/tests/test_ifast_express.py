# -*- coding: utf-8 -*-
"""Runs inside Odoo (odoo-bin -i ifast_express --test-tags /ifast_express). The iFAST service is mocked."""
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.ifast_connector import PARAM_SECRET, PARAM_SITE_ID, sign_request


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


@tagged("post_install", "-at_install")
class TestIfastExpress(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_SITE_ID, "od_0123456789abcdef01234567")
        params.set_param(PARAM_SECRET, "test-secret")
        cls.carrier = cls.env.ref("ifast_express.delivery_carrier_ifast")
        cls.partner = cls.env["res.partner"].create({"name": "Sara Ali", "city": "Dubai", "country_id": cls.env.ref("base.ae").id})
        cls.product = cls.env["product.product"].create({"name": "Blue Mug", "type": "consu", "list_price": 35})

    def _order(self):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2})],
        })

    def test_rate_is_signed_and_used(self):
        calls = []

        def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
            calls.append((url, data, headers))
            return FakeResponse({"ok": True, "price": 25.0, "currency": self.env.company.currency_id.name})

        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", side_effect=fake_post):
            result = self.carrier.rate_shipment(self._order())
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 25.0)
        url, body, headers = calls[0]
        self.assertTrue(url.endswith("/odoo/rates"))
        self.assertEqual(json.loads(body)["city"], "Dubai")
        self.assertEqual(headers["X-IFAST-Signature"], sign_request("test-secret", headers["X-IFAST-Timestamp"], "/odoo/rates", body))

    def test_rate_failure_is_shown_not_raised(self):
        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", return_value=FakeResponse({"ok": False, "message": "No price"})):
            result = self.carrier.rate_shipment(self._order())
        self.assertFalse(result["success"])
        self.assertIn("No price", result["error_message"])

    def test_send_shipping_returns_the_tracking_number(self):
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": self.env.ref("stock.stock_location_stock").id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "carrier_id": self.carrier.id,
        })
        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", return_value=FakeResponse({"ok": True, "tracking_number": "F000650"})):
            result = self.carrier.ifast_send_shipping(picking)
        self.assertEqual(result[0]["tracking_number"], "F000650")
        picking.carrier_tracking_ref = "F000650"
        self.assertIn("tracking_number=F000650", self.carrier.ifast_get_tracking_link(picking))

    def test_rate_falls_back_to_other_cities_when_ifast_is_slow(self):
        import requests
        # The fallback is in AED: a UAE shop has AED active with a rate (1.0 here, so the price stays 25).
        aed = self.env.ref("base.AED")
        aed.active = True
        if self.env.company.currency_id != aed:
            self.env["res.currency.rate"].create({"currency_id": aed.id, "rate": 1.0, "company_id": self.env.company.id})
        ok = FakeResponse({"ok": True, "price": 20.0, "fallback_price": 25.0, "currency": self.env.company.currency_id.name})
        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", return_value=ok):
            self.carrier.rate_shipment(self._order())
        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", side_effect=requests.Timeout()):
            result = self.carrier.rate_shipment(self._order())
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 25.0)

    def test_refused_send_lets_validation_through(self):
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": self.env.ref("stock.stock_location_stock").id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "carrier_id": self.carrier.id,
        })
        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", return_value=FakeResponse({"ok": False, "message": "Receiver phone is missing"})):
            picking.send_to_shipper()
        self.assertIn("Receiver phone is missing", picking.ifast_send_error)
        self.assertFalse(picking.carrier_tracking_ref)
        picking.carrier_tracking_ref = "F000651"
        self.assertFalse(picking.ifast_send_error)

    def test_send_shipping_raises_when_refused(self):
        picking = self.env["stock.picking"].create({
            "partner_id": self.partner.id,
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": self.env.ref("stock.stock_location_stock").id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
        })
        with patch("odoo.addons.ifast_express.models.ifast_connector.requests.post", return_value=FakeResponse({"ok": False, "message": "Receiver phone is missing"})):
            with self.assertRaises(UserError):
                self.carrier.ifast_send_shipping(picking)

    def test_not_connected_says_so(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_SITE_ID, False)
        with self.assertRaises(UserError):
            self.env["ifast.connector"].action_open_console()
