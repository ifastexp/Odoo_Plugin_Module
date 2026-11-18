# -*- coding: utf-8 -*-
"""Talks to the iFAST service (connect5): connect, signed calls, console launch.

Every call after connecting is signed exactly like the service expects (src/store/signature.ts):
HMAC-SHA256 over "<unix seconds>\\n<route>\\n<sha256 hex of the body>" with the site secret, sent as
X-IFAST-Site / X-IFAST-Timestamp / X-IFAST-Signature. The secret never leaves this database.
"""
import base64
import hashlib
import inspect
import hmac
import json
import logging
import secrets
import time

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_SERVICE_URL = "https://connect5.ifast.express"
PARAM_SERVICE_URL = "ifast_express.service_url"
PARAM_PUBLIC_URL = "ifast_express.public_url"
PARAM_SITE_ID = "ifast_express.site_id"
PARAM_SECRET = "ifast_express.site_secret"
PARAM_PENDING = "ifast_express.pending_connect"
API_KEY_NAME = "iFAST Express"
TIMEOUT = 30
EVENT_TIMEOUT = 5


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_request(secret, timestamp, route, body):
    """Same signature as the service: HMAC-SHA256(secret, "<ts>\\n<route>\\n<sha256(body)>"), hex."""
    body_hash = hashlib.sha256(body).hexdigest()
    message = "%s\n%s\n%s" % (timestamp, route, body_hash)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def launch_token(site_id, secret, site_url, user_id, now=None, ttl=120):
    """A short-lived HS256 token the console accepts (src/store/tokens.ts verifyLaunchToken)."""
    now = int(now or time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {
        "typ": "launch",
        "sid": site_id,
        "sub": str(user_id),
        "dest": site_url,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(8),
    }
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signature = _b64url(hmac.new(secret.encode("utf-8"), ("%s.%s" % (header, body)).encode("ascii"), hashlib.sha256).digest())
    return "%s.%s.%s" % (header, body, signature)


class IfastConnector(models.AbstractModel):
    _name = "ifast.connector"
    _description = "iFAST Express connection"

    # ---- settings ------------------------------------------------------------------------------------------------

    @api.model
    def _param(self, key, default=False):
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    @api.model
    def _service_url(self):
        return (self._param(PARAM_SERVICE_URL) or DEFAULT_SERVICE_URL).rstrip("/")

    @api.model
    def _public_url(self):
        """The address iFAST reaches this Odoo at (must be public https)."""
        return (self._param(PARAM_PUBLIC_URL) or self._param("web.base.url") or "").rstrip("/")

    @api.model
    def _credentials(self):
        site_id = self._param(PARAM_SITE_ID)
        secret = self._param(PARAM_SECRET)
        return (site_id, secret) if site_id and secret else (False, False)

    @api.model
    def is_connected(self):
        return bool(self._credentials()[0])

    # ---- HTTP ------------------------------------------------------------------------------------------------------

    @api.model
    def _signed_post(self, route, payload, timeout=TIMEOUT):
        site_id, secret = self._credentials()
        if not site_id:
            raise UserError(_("iFAST is not connected yet. Open Inventory > Configuration > Settings > iFAST Express and click Connect."))
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-IFAST-Site": site_id,
            "X-IFAST-Timestamp": str(timestamp),
            "X-IFAST-Signature": sign_request(secret, timestamp, route, body),
        }
        try:
            response = requests.post(self._service_url() + route, data=body, headers=headers, timeout=timeout)
        except requests.RequestException as error:
            _logger.warning("iFAST call %s failed: %s", route, error)
            raise UserError(_("iFAST could not be reached. Please try again in a minute.")) from error
        return self._read(response)

    @api.model
    def _read(self, response):
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code == 401:
            raise UserError(_("iFAST did not accept this Odoo. Reconnect it in Settings > iFAST Express."))
        if response.status_code >= 400:
            message = data.get("error") or data.get("message")
            raise UserError(message if isinstance(message, str) else _("iFAST answered with an error (%s).", response.status_code))
        return data

    # ---- connect / disconnect / open -------------------------------------------------------------------------------

    @api.model
    def _remove_api_keys(self):
        """The iFAST keys of the current user (an older connection), so reconnecting never piles up keys."""
        keys = self.env["res.users.apikeys"].sudo().search([("user_id", "=", self.env.uid), ("name", "=", API_KEY_NAME)])
        keys.unlink()

    @api.model
    def _new_api_key(self):
        """A key for the current user, used by iFAST to read deliveries and write tracking back.
        Odoo 18 takes an expiration date (False: none, allowed for administrators); Odoo 17 does not."""
        keys = self.env["res.users.apikeys"]
        if "expiration_date" in inspect.signature(keys._generate).parameters:
            return keys._generate("rpc", API_KEY_NAME, False)
        return keys._generate("rpc", API_KEY_NAME)

    @api.model
    def action_connect(self):
        """Step 1: a new API key, saved when this request commits. Step 2 (a new request, so the key exists for iFAST
        to check) registers with iFAST."""
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only an administrator can connect iFAST."))
        if not self._public_url().startswith("https://"):
            raise UserError(_("iFAST needs this Odoo's public https address. Set it in Settings > iFAST Express (Public Odoo address)."))
        self._remove_api_keys()
        api_key = self._new_api_key()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_PENDING, api_key)
        return {"type": "ir.actions.server", "id": self.env.ref("ifast_express.action_ifast_connect_finish").id}

    @api.model
    def action_connect_finish(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only an administrator can connect iFAST."))
        params = self.env["ir.config_parameter"].sudo()
        api_key = params.get_param(PARAM_PENDING)
        if not api_key:
            raise UserError(_("Click Connect iFAST again."))
        params.set_param(PARAM_PENDING, False)
        module = self.env["ir.module.module"].sudo().search([("name", "=", "ifast_express")], limit=1)
        payload = {
            "url": self._public_url(),
            "db": self.env.cr.dbname,
            "login": self.env.user.login,
            "api_key": api_key,
            "module_version": module.latest_version or module.installed_version or "",
            "locale": (self.env.user.lang or "en_US")[:5],
        }
        try:
            response = requests.post(self._service_url() + "/odoo/register", json=payload, timeout=60)
        except requests.RequestException as error:
            raise UserError(_("iFAST could not be reached. Please try again in a minute.")) from error
        data = self._read(response)
        if not data.get("site_id") or not data.get("site_secret"):
            raise UserError(_("iFAST did not finish the connection. Please try again."))
        params.set_param(PARAM_SITE_ID, data["site_id"])
        params.set_param(PARAM_SECRET, data["site_secret"])
        return self.action_open_console()

    @api.model
    def action_disconnect(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only an administrator can disconnect iFAST."))
        if self.is_connected():
            try:
                self._signed_post("/odoo/events", {"event": "site.disconnect"}, timeout=EVENT_TIMEOUT)
            except UserError as error:
                _logger.warning("iFAST disconnect could not reach the service: %s", error)
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_SITE_ID, False)
        params.set_param(PARAM_SECRET, False)
        self._remove_api_keys()
        return {"type": "ir.actions.client", "tag": "reload"}

    @api.model
    def action_open_console(self):
        # The console acts for the whole shop (send, cancel, finance): Inventory managers only.
        if not self.env.user.has_group("stock.group_stock_manager"):
            raise UserError(_("Only Inventory administrators can open iFAST."))
        site_id, secret = self._credentials()
        if not site_id:
            raise UserError(_("iFAST is not connected yet. Open Inventory > Configuration > Settings > iFAST Express and click Connect."))
        token = launch_token(site_id, secret, self._public_url(), self.env.uid)
        return {"type": "ir.actions.act_url", "url": "%s/app?launch=%s" % (self._service_url(), token), "target": "new"}

    # ---- events ----------------------------------------------------------------------------------------------------

    @api.model
    def notify_pickings_changed(self, picking_ids):
        """After the transaction commits, tell iFAST which deliveries changed (it reads them back itself).
        Never blocks or fails the user's action: a lost call is caught by iFAST's poll every 2 minutes."""
        site_id, secret = self._credentials()
        if not site_id or not picking_ids:
            return
        data = self.env.cr.postcommit.data
        pending = data.get("ifast_express.picking_ids")
        if pending is None:
            pending = data["ifast_express.picking_ids"] = set()
            service_url = self._service_url()

            def send():
                ids = sorted(pending)
                for start in range(0, len(ids), 200):
                    body = json.dumps({"event": "picking.changed", "ids": ids[start:start + 200]}, separators=(",", ":")).encode("utf-8")
                    timestamp = int(time.time())
                    headers = {
                        "Content-Type": "application/json",
                        "X-IFAST-Site": site_id,
                        "X-IFAST-Timestamp": str(timestamp),
                        "X-IFAST-Signature": sign_request(secret, timestamp, "/odoo/events", body),
                    }
                    try:
                        requests.post(service_url + "/odoo/events", data=body, headers=headers, timeout=EVENT_TIMEOUT)
                    except requests.RequestException as error:
                        _logger.info("iFAST event not delivered (the poll will catch it): %s", error)

            self.env.cr.postcommit.add(send)
        pending.update(picking_ids)
