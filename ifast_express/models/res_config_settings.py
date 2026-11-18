# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .ifast_connector import DEFAULT_SERVICE_URL, PARAM_PUBLIC_URL, PARAM_SERVICE_URL


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ifast_public_url = fields.Char(
        string="Public Odoo address",
        config_parameter=PARAM_PUBLIC_URL,
        help="The https address iFAST uses to reach this Odoo. Leave empty to use the web base URL.",
    )
    ifast_service_url = fields.Char(
        string="iFAST service",
        config_parameter=PARAM_SERVICE_URL,
        default=DEFAULT_SERVICE_URL,
    )
    ifast_connected = fields.Boolean(string="iFAST connected", compute="_compute_ifast_connected")

    @api.depends("company_id")
    def _compute_ifast_connected(self):
        connected = self.env["ifast.connector"].is_connected()
        for settings in self:
            settings.ifast_connected = connected

    def action_ifast_connect(self):
        self.execute()
        return self.env["ifast.connector"].action_connect()

    def action_ifast_disconnect(self):
        return self.env["ifast.connector"].action_disconnect()

    def action_ifast_open(self):
        return self.env["ifast.connector"].action_open_console()
