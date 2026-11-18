# -*- coding: utf-8 -*-
{
    "name": "iFAST Express",
    "version": "17.0.1.0.0",
    "category": "Inventory/Delivery",
    "summary": "Ship with iFAST Express (UAE): delivery prices at checkout, send deliveries, AWB labels and tracking",
    "description": """
iFAST Express
=============
Connect your Odoo to iFAST Express, the UAE same-day and next-day courier.

* iFAST Express shipping method: delivery price on quotations and website checkout (the shipper's Other cities price if iFAST is slow)
* Deliveries are sent to iFAST when you validate them (or in bulk from the list); a refused delivery is still validated and marked "Not sent to iFAST"
* AWB labels printed from Odoo and attached to the delivery
* Tracking number and tracking link on every delivery, status notes in the chatter
* Cash on delivery collected when the sale is not paid
* The iFAST console (orders, dashboard, finance, stock) one click away
""",
    "author": "iFAST Express",
    "maintainer": "iFAST Express",
    "website": "https://ifast.express",
    "support": "support@ifast.express",
    "license": "LGPL-3",
    "depends": ["stock_delivery"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "data/ifast_data.xml",
        "data/ifast_actions.xml",
        "views/res_config_settings_views.xml",
        "views/stock_picking_views.xml",
        "views/ifast_menus.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
