import os
import logging
import re

from odoo import models, fields, api, tools

_logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.config import Config
except (ImportError, IOError) as err:
    _logger.debug(err)


class IrAttachment(models.Model):
    """
    We set up two additional checklists for the gc.
    The original list is used to mark for deletion local files.
    The external_checklist is the same but for files on s3.
    The upload_checklist keeps track of local files that are still to upload, so they aren't
    deleted by the gc.

    Confront comments in base/models/ir_attachment.py
    """

    _inherit = "ir.attachment"

    is_external = fields.Boolean(string="Risorsa esterna", default=False)

    @api.model
    def _get_aws_config(self):
        region = self.env["ir.config_parameter"].sudo().get_param("aws_region_name")
        return Config(region_name=region)

    @api.model
    def _file_read(self, fname):
        full_path = self._full_path(fname)

        atts = self.search([("store_fname", "=", fname)])
        if not atts:
            _logger.info("_file_read reading %s", fname, exc_info=True)
            return b""
        att = atts[0]

        if att.is_external and not os.path.exists(full_path):
            # FIXME test

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
        fname = re.sub("[.]", "", fname).strip("/\\")
        full_path = os.path.join(self._full_path("external_checklist"), fname)
        if not os.path.exists(full_path):
            dirname = os.path.dirname(full_path)
            if not os.path.isdir(dirname):
                with tools.ignore(OSError):
                    os.makedirs(dirname)
            open(full_path, "ab").close()

    @api.model
    def _file_mark_for_upload(self, fname):
        fname = re.sub("[.]", "", fname).strip("/\\")
        full_path = os.path.join(self._full_path("upload_checklist"), fname)
        if not os.path.exists(full_path):
            dirname = os.path.dirname(full_path)
            if not os.path.isdir(dirname):
                with tools.ignore(OSError):
                    os.makedirs(dirname)
            open(full_path, "ab").close()

    @api.model_create_multi
    def create(self, vals_list):
        """
        Create override to mark external files for upload.
        """
        for vals in vals_list:
            # FIXME set is_external
            pass

        atts = super().create(vals_list)

        for att in atts:
            if att.is_external:
                self._file_mark_for_upload(att.store_fname)

        return atts

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

        # Clean up the checklist.
        removed = 0
        for names in cr.split_for_in_conditions(checklist):
            # Keep files that are linked to a _local_ attachment
            # FIXME keep files that are not yet been uploaded
            cr.execute(
                """
                SELECT store_fname FROM ir_attachment
                WHERE store_fname IN %s and is_external is false
                """,
                [names],
            )
            whitelist = set(row[0] for row in cr.fetchall())

            for fname in names:
                filepath = checklist[fname]
                if fname not in whitelist:
                    try:
                        os.unlink(self._full_path(fname))
                        _logger.debug("_file_gc unlinked %s", self._full_path(fname))
                        removed += 1
                    except (OSError, IOError):
                        _logger.info(
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

        # get the files still to upload
        # not_uploaded = []
        # for dirpath, _, filenames in os.walk(self._full_path("upload_checklist")):
        #     dirname = os.path.basename(dirpath)
        #     for filename in filenames:
        #         not_uploaded.append(f"{dirname}/{filename}")

        # Clean up the checklist.
        removed = 0

        s3 = boto3.client(
            "s3",
            config=Config(region_name=aws_region_name),
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

        # FIXME here
        cr.execute(
            """
            SELECT store_fname FROM ir_attachment
            WHERE store_fname IN %s OR is_external is true
            """,
            [list(checklist.keys())],
        )
        whitelist = set(row[0] for row in cr.fetchall())

        to_delete = list(set(checklist.keys()) - whitelist)

        errors = []

        # boto3 can donwload at most 1000 elements with a single HTTP call
        for i in range(0, len(to_delete), 1000):
            chunk = to_delete[i : i + 1000]
            errs = s3.delete_objects(
                Bucket=aws_bucket_name,
                Delete={
                    "Objects": [{"Key": key} for key in chunk],
                    "Quiet": True,
                },
            )
            errors.extend(errs["Errors"]) # FIXME does this work?

        # FIXME manage errors (sigh)
        # FIXME clean up checklist with stuff that was really deleted



        for names in cr.split_for_in_conditions(checklist):
            # Keep files that are marked to be uploaded
            cr.execute(
                """
                SELECT store_fname FROM ir_attachment
                WHERE store_fname IN %s
                    AND store_fname NOT IN %s
                    AND is_external is true
                """,
                [names],
            )
            whitelist = set(row[0] for row in cr.fetchall())

            # remove garbage files, and clean up checklist
            for fname in names:
                filepath = checklist[fname]
                if fname not in whitelist:
                    try:
                        # FIXME delete file on s3

                        _logger.debug("_file_egc deleted %s", self._full_path(fname))
                        removed += 1
                    except Exception:
                        _logger.info(
                            "_file_egc could not delete %s",
                            self._full_path(fname),
                            exc_info=True,
                        )
                with tools.ignore(OSError):
                    os.unlink(filepath)

        # commit to release the lock
        cr.commit()  # pylint: disable=invalid-commit
        _logger.info(
            "external filestore gc %d checked, %d removed", len(checklist), removed
        )

    def _set_attachment_data(self, asbytes):
        for attach in self:
            fname = attach.store_fname
            if attach.is_external and fname:
                self._file_delete_external(fname)

        return super()._set_attachment_data(asbytes)

    def unlink(self):
        """
        Override to delete externally stored files.
        """
        to_delete = set(
            attach.store_fname
            for attach in self
            if attach.store_fname and attach.is_external
        )
        res = super(IrAttachment, self).unlink()
        for fname in to_delete:
            self._file_delete_external(fname)

        return res

    @api.model
    def _upload_all(self):
        # retrieve the file names from the checklist
        checklist = {}
        for dirpath, _, filenames in os.walk(self._full_path("upload_checklist")):
            dirname = os.path.basename(dirpath)
            for filename in filenames:
                fname = f"{dirname}/{filename}"
                checklist[fname] = os.path.join(dirpath, filename)

        uploaded = 0
        for names in self._cr.split_for_in_conditions(checklist):
            for fname in names:
                filepath = checklist[fname]

                try:
                    # FIXME upload(self._full_path(fname))

                    with tools.ignore(OSError):
                        os.unlink(filepath)

                    _logger.debug("_file_up uploaded %s", self._full_path(fname))
                    uploaded += 1
                except Exception:
                    _logger.warning(
                        "_file_up could not upload %s",
                        self._full_path(fname),
                        exc_info=True,
                    )

        _logger.info("filestore up %d checked, %d uploaded", len(checklist), uploaded)
