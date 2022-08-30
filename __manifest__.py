{
    "name": "S3 Attachments",
    "summary": """
        Modulo di gestione storage documentale.
    """,
    "description": """
      This module allows to use an external S3 filestore for Odoo Attachments.
      It also uses local caching to reduce the number of API calls.
    """,
    "license": "Other proprietary",
    "author": "Gruppo Scudo Srl, David Barozzini",
    "website": "http://www.grupposcudo.it",
    "version": "14.0.0.4.2",
    "depends": ["base"],
    "external_dependencies": {"python": ["boto3"]},
    "data": [
        "security/ir.model.access.csv",
        "views/ir_attachment_s3_settings_view.xml",
        # "automation/automatic_actions.xml",
    ],
}
