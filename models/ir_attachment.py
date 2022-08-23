import os
import logging
import re

from odoo import models, fields, api, tools

_logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except (ImportError, IOError) as err:
    _logger.debug(err)


class IrAttachment(models.Model):
    """
    We set up an additional checklists for the gc.
    The original list is used to mark for deletion local files.
    The external_checklist is the same but for files on s3.

    Confront comments in base/models/ir_attachment.py
    """

    _inherit = "ir.attachment"

    is_external = fields.Boolean(string="Risorsa esterna", default=False)
    is_uploaded = fields.Boolean(string="Caricato su S3", readonly=True, default=False)

    @api.model
    def _is_s3_active(self):
        odoo_stage = os.environ.get("ODOO_STAGE", "production")
        aws_stage_condition = (
            self.env["ir.config_parameter"].sudo().get_param("aws_stage_condition")
        )
        return odoo_stage == aws_stage_condition

    @api.model
    def _file_read(self, fname):
        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return super()._file_read(fname)

        full_path = self._full_path(fname)

        atts = self.env["ir.attachment"].search(
            [
                ("store_fname", "=", fname),
                "|",
                ("res_field", "=", False),
                ("res_field", "!=", False),
            ]
        )
        if not atts:
            _logger.info("_file_read reading %s", fname, exc_info=True)
            return b""
        att = atts[0]

        if att.is_external and not os.path.exists(full_path):

            def get_param(param):
                return self.env["ir.config_parameter"].sudo().get_param(param)

            aws_access_key_id = get_param("aws_access_key_id")
            aws_secret_access_key = get_param("aws_secret_access_key")
            aws_region_name = get_param("aws_region_name")
            aws_bucket_name = get_param("aws_bucket_name")

            if (
                aws_access_key_id is not False
                and aws_secret_access_key is not False
                and aws_region_name is not False
                and aws_bucket_name is not False
            ):
                s3 = boto3.client(
                    "s3",
                    config=Config(region_name=aws_region_name),
                    aws_access_key_id=aws_access_key_id,
                    aws_secret_access_key=aws_secret_access_key,
                )

                s3.download_file(
                    aws_bucket_name,
                    fname,
                    full_path,
                )

                self._file_delete(fname)
                _logger.debug("File %s read from S3", fname)
            else:
                _logger.error("Missing AWS S3 configuration")

        return super()._file_read(fname)

    @api.model
    def _file_delete_external(self, fname):
        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return

        fname = re.sub("[.]", "", fname).strip("/\\")
        full_path = os.path.join(self._full_path("external_checklist"), fname)
        if not os.path.exists(full_path):
            dirname = os.path.dirname(full_path)
            if not os.path.isdir(dirname):
                with tools.ignore(OSError):
                    os.makedirs(dirname)
            open(full_path, "ab").close()  # pylint: disable=consider-using-with

    @api.model_create_multi
    def create(self, vals_list):
        """
        Create override to mark external files for upload.
        """
        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return super().create(vals_list)

        condition = (
            self.env["ir.config_parameter"].sudo().get_param("aws_upload_condition")
        )

        if condition is False:
            return super().create(vals_list)

        model_names = [
            m.model
            for m in self.env["ir.model"].search(
                [("model", "in", condition.split(","))]
            )
        ]

        for vals in vals_list:
            if vals["res_model"] in model_names:
                vals["is_external"] = True

        return super().create(vals_list)

    @api.autovacuum
    def _gc_file_store(self):
        """
        Perform the garbage collection of the filestore.
        Override to change the whitelist.
        """
        if self._storage() != "file":
            return

        cr = self._cr
        cr.commit()  # pylint: disable=invalid-commit

        cr.execute("SET LOCAL lock_timeout TO '10s'")
        cr.execute("LOCK ir_attachment IN SHARE MODE")

        # retrieve the file names from the checklist
        checklist = {}
        for dirpath, _, filenames in os.walk(self._full_path("checklist")):
            dirname = os.path.basename(dirpath)
            for filename in filenames:
                fname = f"{dirname}/{filename}"
                checklist[fname] = os.path.join(dirpath, filename)

        removed = 0
        for names in cr.split_for_in_conditions(checklist):
            # Keep files that are linked to a _local_ attachment
            # or files that are linked to an external attachment and
            # haven't been uploaded yet
            cr.execute(
                """
                SELECT store_fname FROM ir_attachment
                WHERE
                    store_fname IN %s and (
                        is_external is false or
                        is_external is null or (
                            is_external is true
                            and is_uploaded is false
                        )
                    )
                """,
                [names],
            )
            whitelist = set(row[0] for row in cr.fetchall())

            for fname in names:
                filepath = checklist[fname]
                if fname not in whitelist and os.path.exists(self._full_path(fname)):
                    try:
                        os.unlink(self._full_path(fname))
                        _logger.debug("_file_gc unlinked %s", self._full_path(fname))
                        removed += 1
                    except (OSError, IOError):
                        _logger.error(
                            "_file_gc could not unlink %s",
                            self._full_path(fname),
                            exc_info=True,
                        )
                with tools.ignore(OSError):
                    os.unlink(filepath)

        cr.commit()  # pylint: disable=invalid-commit
        _logger.info("filestore gc %d checked, %d removed", len(checklist), removed)

    @api.autovacuum
    def _gc_s3_store(self):
        """
        Perform garbage collection on the s3 external store.
        """
        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return

        if self._storage() != "file":
            return

        def get_param(param):
            return self.env["ir.config_parameter"].sudo().get_param(param)

        aws_access_key_id = get_param("aws_access_key_id")
        aws_secret_access_key = get_param("aws_secret_access_key")
        aws_region_name = get_param("aws_region_name")
        aws_bucket_name = get_param("aws_bucket_name")

        if (
            aws_access_key_id is False
            or aws_secret_access_key is False
            or aws_region_name is False
            or aws_bucket_name is False
        ):
            return

        cr = self._cr
        cr.commit()  # pylint: disable=invalid-commit

        cr.execute("SET LOCAL lock_timeout TO '10s'")
        cr.execute("LOCK ir_attachment IN SHARE MODE")

        # retrieve the file names from the checklist
        checklist = {}
        for dirpath, _, filenames in os.walk(self._full_path("external_checklist")):
            dirname = os.path.basename(dirpath)
            for filename in filenames:
                fname = f"{dirname}/{filename}"
                checklist[fname] = os.path.join(dirpath, filename)

        if not checklist:
            cr.commit()  # pylint: disable=invalid-commit
            _logger.info("external filestore gc 0 checked, 0 removed")
            return

        # keep files that are linked to an _external_ attachment
        cr.execute(
            """
            SELECT store_fname FROM ir_attachment
            WHERE store_fname IN %s AND is_external is true
            """,
            [tuple(checklist.keys())],
        )
        whitelist = set(row[0] for row in cr.fetchall())

        to_delete = list(set(checklist.keys()) - whitelist)

        if not to_delete:
            cr.commit()  # pylint: disable=invalid-commit
            _logger.info("external filestore gc 0 checked, 0 removed")
            return

        s3 = boto3.client(
            "s3",
            config=Config(region_name=aws_region_name),
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

        # boto3 can delete at most 1000 elements with a single HTTP call
        removed = 0
        for i in range(0, len(to_delete), 1000):
            chunk = to_delete[i : i + 1000]
            res = s3.delete_objects(
                Bucket=aws_bucket_name,
                Delete={"Objects": [{"Key": key} for key in chunk]},
            )
            if "Errors" in res:
                for error in res["Errors"]:
                    _logger.warning(
                        "_file_egc could not delete %s: %s",
                        error["Key"],
                        error["Message"],
                    )
                    removed -= 1

        # clean up checklist
        for names in cr.split_for_in_conditions(checklist):
            for fname in names:
                with tools.ignore(OSError):
                    os.unlink(checklist[fname])
                removed += 1

        # commit to release the lock
        cr.commit()  # pylint: disable=invalid-commit
        _logger.info(
            "external filestore gc %d checked, %d removed", len(checklist), removed
        )

    def _set_attachment_data(self, asbytes):
        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return super()._set_attachment_data(asbytes)

        for attach in self:
            fname = attach.store_fname
            if attach.is_external and attach.is_uploaded and fname:
                self._file_delete_external(fname)
                attach.is_uploaded = False

        return super()._set_attachment_data(asbytes)

    def unlink(self):
        """
        Override to delete externally stored files.
        """
        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return super().unlink()

        to_delete = set(
            attach.store_fname
            for attach in self
            if attach.store_fname and attach.is_external
        )
        res = super().unlink()
        for fname in to_delete:
            self._file_delete_external(fname)

        return res

    # FIXME this needs to be automatic
    @api.model
    def upload_all(self):
        """
        Upload on S3 all attachments marked as external but not yet uploaded.
        Then mark the attachments as uploaded and the local files as "to delete".
        """

        if not self._is_s3_active():
            _logger.info("Not in production: only local files available.")
            return

        def get_param(param):
            return self.env["ir.config_parameter"].sudo().get_param(param)

        aws_access_key_id = get_param("aws_access_key_id")
        aws_secret_access_key = get_param("aws_secret_access_key")
        aws_region_name = get_param("aws_region_name")
        aws_bucket_name = get_param("aws_bucket_name")

        if (
            aws_access_key_id is False
            or aws_secret_access_key is False
            or aws_region_name is False
            or aws_bucket_name is False
        ):
            _logger.info("AWS credentials missing")
            return

        s3 = boto3.client(
            "s3",
            config=Config(region_name=aws_region_name),
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

        checklist = self.env["ir.attachment"].search(
            [("is_external", "=", True), ("is_uploaded", "=", False)]
        )

        uploaded = 0
        try:
            for atts in self._cr.split_for_in_conditions(checklist):
                for att in atts:
                    fname = att.store_fname

                    try:
                        _res = s3.upload_file(
                            Bucket=aws_bucket_name,
                            Filename=self._full_path(fname),
                            Key=fname,
                        )

                        att.is_uploaded = True
                        self._file_delete(fname)
                        _logger.debug("_file_up uploaded %s", self._full_path(fname))
                        uploaded += 1
                    except ClientError as e:
                        _logger.warning(
                            "_file_up could not upload %s: %s",
                            self._full_path(fname),
                            e.response,
                        )
        except Exception as e:
            _logger.error(e)

        _logger.info("filestore up %d checked, %d uploaded", len(checklist), uploaded)
