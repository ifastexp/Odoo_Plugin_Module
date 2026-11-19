# -*- coding: utf-8 -*-
"""Delivery orders: tell iFAST when they change, and the Send / Print AWB / Open buttons (form and list)."""
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# A change to one of these is worth telling iFAST about. The state is computed from the moves (never in write vals),
# so Validate, Cancel and Check Availability notify through their own methods below.
WATCHED_FIELDS = {"partner_id", "carrier_id", "scheduled_date"}
# Stay well under Odoo's limit_time_real (120 s by default) for one click.
SEND_BUDGET_SECONDS = 80


class StockPicking(models.Model):
    _inherit = "stock.picking"

    # Set when iFAST refused the delivery at validation; cleared when a tracking number arrives.
    ifast_send_error = fields.Char("Not sent to iFAST", copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        pickings = super().create(vals_list)
        pickings._ifast_notify()
        return pickings

    def write(self, vals):
        if vals.get("carrier_tracking_ref") and "ifast_send_error" not in vals:
            vals = dict(vals, ifast_send_error=False)
        result = super().write(vals)
        if WATCHED_FIELDS.intersection(vals):
            self._ifast_notify()
        return result

    def _action_done(self):
        result = super()._action_done()
        self._ifast_notify()
        return result

    def action_cancel(self):
        result = super().action_cancel()
        self._ifast_notify()
        return result

    def action_assign(self):
        result = super().action_assign()
        self._ifast_notify()
        return result

    def send_to_shipper(self):
        """Validation goes through even when iFAST refuses the delivery (owner's rule, like Shopify and WooCommerce):
        the delivery shows "Not sent to iFAST" with the reason and is sent again from the button or the console.
        Our send_shipping only calls iFAST and writes nothing, so catching its refusal leaves no half-written state."""
        self.ensure_one()
        if self.carrier_id.delivery_type != "ifast":
            return super().send_to_shipper()
        try:
            return super().send_to_shipper()
        except UserError as error:
            reason = str(error).strip().rstrip(".")
            # The banner shows the field followed by "Fix it, ...", so the field keeps its own full stop.
            self.ifast_send_error = reason + "."
            self.message_post(body=_("Not sent to iFAST: %s. Fix it, then click Send to iFAST or send it from the iFAST console.", reason))
            return None

    def _ifast_notify(self):
        outgoing = self.filtered(lambda picking: picking.picking_type_code == "outgoing")
        if outgoing:
            self.env["ifast.connector"].sudo().notify_pickings_changed(outgoing.ids)

    def _ifast_outgoing(self):
        pickings = self.filtered(lambda picking: picking.picking_type_code == "outgoing")
        if not pickings:
            raise UserError(_("Select delivery orders (outgoing)."))
        return pickings

    def action_ifast_send(self):
        """Send the selected deliveries to iFAST now (without validating them). Each delivery is reported on its own;
        what does not fit in one click's time is left for the next click."""
        connector = self.env["ifast.connector"]
        started = time.monotonic()
        sent, failed, later = [], [], []
        for picking in self._ifast_outgoing():
            if time.monotonic() - started > SEND_BUDGET_SECONDS:
                later.append(picking.name)
                continue
            if picking.state == "cancel":
                failed.append("%s: %s" % (picking.name, _("cancelled")))
                continue
            if picking.carrier_id and picking.carrier_id.delivery_type != "ifast":
                failed.append("%s: %s" % (picking.name, _("uses another shipping method (%s)", picking.carrier_id.name)))
                continue
            try:
                data = connector._signed_post("/odoo/ship", {"picking_id": picking.id}, timeout=45)
            except UserError as error:
                failed.append("%s: %s" % (picking.name, error))
                continue
            if data.get("ok"):
                tracking = data.get("tracking_number")
                # iFAST does not write to the delivery during /odoo/ship (this transaction may hold its row), so the
                # tracking number is saved here; that also clears "Not sent to iFAST".
                if tracking and picking.carrier_tracking_ref != tracking:
                    picking.carrier_tracking_ref = tracking
                    link = data.get("tracking_url") or (picking.carrier_id.get_tracking_link(picking) if picking.carrier_id else False)
                    picking.message_post(body=_("Sent to iFAST: %(tracking)s %(link)s", tracking=tracking, link=link or ""))
                sent.append("%s: %s" % (picking.name, tracking))
            else:
                failed.append("%s: %s" % (picking.name, data.get("message") or _("not sent")))
        if later:
            failed.append(_("Not tried yet (click again): %s", ", ".join(later)))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("iFAST: %(sent)s sent, %(failed)s not sent", sent=len(sent), failed=len(failed) + len(later) - (1 if later else 0)),
                "message": " · ".join(sent + failed),
                "type": "warning" if failed else "success",
                "sticky": bool(failed),
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_ifast_print_awb(self):
        """One PDF with the iFAST AWB labels of the selected deliveries."""
        pickings = self._ifast_outgoing()
        data = self.env["ifast.connector"]._signed_post("/odoo/awb", {"picking_ids": pickings.ids[:100]}, timeout=90)
        if not data.get("ok") or not data.get("pdf"):
            raise UserError(data.get("message") or _("Nothing to print: send the deliveries to iFAST first."))
        attachment = self.env["ir.attachment"].create({
            "name": data.get("file_name") or "iFAST-AWB.pdf",
            "datas": data["pdf"],
            "mimetype": "application/pdf",
            "res_model": "stock.picking",
            # Always on a delivery, so the file is never an orphan only its creator can open.
            "res_id": pickings[0].id,
        })
        return {"type": "ir.actions.act_url", "url": "/web/content/%s?download=true" % attachment.id, "target": "self"}

    def action_ifast_open(self):
        return self.env["ifast.connector"].action_open_console()
