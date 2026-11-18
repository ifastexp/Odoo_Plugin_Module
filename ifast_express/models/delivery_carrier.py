# -*- coding: utf-8 -*-
"""The native "iFAST Express" shipping method (delivery_type "ifast")."""
from odoo import _, fields, models
from odoo.exceptions import UserError

TRACKING_URL = "https://ifast.express/en/track-shipment?tracking_number=%s"
# The shipper's own "Other cities" price (AED), kept from iFAST's last good answer. Shown when iFAST cannot answer in
# time, never one of the cheaper or dearer categories (Same City, Western Abu Dhabi, Western Al Ain).
PARAM_FALLBACK_PRICE = "ifast_express.fallback_price"


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("ifast", "iFAST Express")],
        ondelete={"ifast": lambda records: records.write({"delivery_type": "fixed", "fixed_price": 0})},
    )

    def ifast_rate_shipment(self, order):
        """Price for the order's delivery address, from the shipper's iFAST prices (AED, converted to the order currency)."""
        self.ensure_one()
        partner = order.partner_shipping_id or order.partner_id
        payload = {
            "city": partner.city or "",
            "state": partner.state_id.name or "",
            "country_code": partner.country_id.code or "",
        }
        connector = self.env["ifast.connector"]
        params = self.env["ir.config_parameter"].sudo()
        try:
            # Short: this runs on every quotation and website checkout.
            data = connector._signed_post("/odoo/rates", payload, timeout=8)
        except UserError as error:
            fallback = self._ifast_fallback_price() if connector.is_connected() else None
            if fallback is None:
                return {"success": False, "price": 0.0, "error_message": str(error), "warning_message": False}
            # iFAST is slow or down: the shipper's "Other cities" price, so the checkout still works.
            data = {"ok": True, "price": fallback, "currency": "AED"}
        if not data.get("ok"):
            return {"success": False, "price": 0.0, "error_message": data.get("message") or _("No iFAST price for this address."), "warning_message": False}
        fallback_now = data.get("fallback_price")
        if isinstance(fallback_now, (int, float)) and fallback_now >= 0:
            stored = "%.2f" % fallback_now
            # Written only when it changes: this runs on every checkout.
            if params.get_param(PARAM_FALLBACK_PRICE) != stored:
                params.set_param(PARAM_FALLBACK_PRICE, stored)
        price = float(data.get("price") or 0.0)
        currency = self.env["res.currency"].search([("name", "=", data.get("currency") or "AED")], limit=1)
        if order.currency_id and currency != order.currency_id:
            if not currency or not currency.rate_ids:
                # Without an active AED with rates Odoo would convert at 1: better no price than a wrong one.
                return {"success": False, "price": 0.0, "error_message": _("Activate the AED currency (with a rate) to use iFAST prices."), "warning_message": False}
            price = currency._convert(price, order.currency_id, order.company_id, order.date_order or fields.Date.today())
        return {"success": True, "price": price, "error_message": False, "warning_message": False}

    def _ifast_fallback_price(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_FALLBACK_PRICE)
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if price >= 0 else None

    def ifast_send_shipping(self, pickings):
        """Called by Odoo when a delivery with this method is validated: the delivery is created in iFAST first."""
        results = []
        for picking in pickings:
            # Odoo waits for this inside the validation; iFAST answers the same tracking number if asked twice (retries).
            data = self.env["ifast.connector"]._signed_post("/odoo/ship", {"picking_id": picking.id}, timeout=45)
            if not data.get("ok"):
                raise UserError(_("iFAST could not take %(name)s: %(message)s", name=picking.name, message=data.get("message") or _("unknown error")))
            # The price the customer was charged stays on the sale; iFAST invoices the shipper separately.
            results.append({"exact_price": 0.0, "tracking_number": data.get("tracking_number")})
        return results

    def ifast_get_tracking_link(self, picking):
        return TRACKING_URL % picking.carrier_tracking_ref if picking.carrier_tracking_ref else False

    def ifast_cancel_shipment(self, pickings):
        """Odoo posts "Shipment ... cancelled" and clears the tracking number itself after this returns."""
        for picking in pickings:
            data = self.env["ifast.connector"]._signed_post("/odoo/cancel", {"picking_id": picking.id}, timeout=30)
            if not data.get("ok"):
                raise UserError(_("iFAST could not cancel %(name)s: %(message)s", name=picking.name, message=data.get("message") or _("unknown error")))
