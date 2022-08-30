from odoo import models, fields
from odoo.exceptions import ValidationError


class IrAttachmentS3Settings(models.TransientModel):
    _name = "ir_attachment_s3_settings"
    _inherit = "res.config.settings"
    _description = "Impostazioni per S3 Attachments"

    aws_access_key_id = fields.Char(
        string="AWS Access Key ID", config_parameter="aws_access_key_id"
    )
    aws_secret_access_key = fields.Char(
        string="AWS Secret Access Key", config_parameter="aws_secret_access_key"
    )
    aws_region_name = fields.Char(
        string="AWS Region Name", config_parameter="aws_region_name"
    )
    aws_bucket_name = fields.Char(
        string="AWS Bucket Name", config_parameter="aws_bucket_name"
    )
    aws_upload_condition = fields.Char(
        string="AWS Condition",
        config_parameter="aws_upload_condition",
        help="A comma-separated list of models with no whitespace.",
    )
    aws_max_upload = fields.Integer(
        string="Max uploads per session",
        config_paramente="aws_max_upload",
        default=1000,
    )
    aws_stage_condition = fields.Char(
        string="ODOO_STAGE value",
        config_parameter="aws_stage_condition",
        help=(
            "Value of the ODOO_STAGE environmental variable for which the "
            "upload is active."
        ),
        default="production",
    )

    def execute(self):
        """
        Before execution, check if the condition is ok.
        """

        if (
            self.aws_access_key_id is False
            or self.aws_secret_access_key is False
            or self.aws_region_name is False
            or self.aws_bucket_name is False
            # or self.aws_upload_condition is False
        ):
            raise ValidationError("Missing data.")

        if self.aws_upload_condition is not False:
            model_names = self.aws_upload_condition.split(",")
            model_ids = self.env["ir.model"].search([("model", "in", model_names)])

            if len(model_names) != len(model_ids):
                raise ValidationError("Malformed condition: some models do not exist.")

        return super().execute()
