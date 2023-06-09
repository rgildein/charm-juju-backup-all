# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import base64
import hashlib
import logging
import os
import pathlib
import socket
import subprocess
import traceback

import yaml
from charmhelpers.contrib.charmsupport.nrpe import NRPE
from charmhelpers.core import hookenv, host
from charmhelpers.core.host import rsync
from jujubackupall.config import Config
from jujubackupall.process import BackupProcessor
from jujubackupall.utils import connect_controller, connect_model
from ops.model import BlockedStatus
from yaml.parser import ParserError

from config import BACKUP_USERNAME, Paths

# configure libjuju to the location of the credentials
if "JUJUDATA_DIR" not in os.environ:
    os.environ["JUJU_DATA"] = str(Paths.JUJUDATA_DIR)


def run_async(func):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(func)


class JujuBackupAllHelper:
    """Juju-backup-all helper object."""

    def __init__(self, model):
        """Initialise the helper."""
        self.model = model
        self.charm_config = model.config
        self.nrpe = NRPE()
        self.config = Config(args=self._charm_config_to_datadict())
        self.charm_dir = pathlib.Path(hookenv.charm_dir())

    @property
    def accounts(self):
        """Return the current accounts config parsed."""
        accounts_yaml = self.charm_config["accounts"]
        return yaml.safe_load(accounts_yaml)["controllers"]

    def create_backup_user(self):
        """Create the jujubackup user."""
        if not Paths.JUJUDATA_DIR.exists():
            Paths.JUJUDATA_DIR.mkdir()

        if not host.user_exists(BACKUP_USERNAME):
            host.adduser(BACKUP_USERNAME, home_dir=Paths.JUJUDATA_DIR)

    def create_backup_dir(self):
        """Create the backup directory."""
        backup_dir = pathlib.Path(self.charm_config["backup-dir"])
        if not backup_dir.exists():
            backup_dir.mkdir()
            self._update_dir_owner(backup_dir)

    def deploy_scripts(self):
        """Deploy the scripts needed by the charm."""
        logging.debug("charm dir: '{}'".format(self.charm_dir))

        # Create the auto_backup.py script from the template with the right permissions
        logging.debug("templating and deploying the auto_backup.py script")
        auto_backup_template = (
            self.charm_dir / "scripts/templates/auto_backup.py"
        ).read_text()

        auto_backup_script = auto_backup_template.replace(
            "REPLACE_CHARMDIR", str(self.charm_dir)
        )

        fd = os.open(
            str(Paths.AUTO_BACKUP_SCRIPT_PATH), os.O_CREAT | os.O_WRONLY, 0o755
        )
        with open(fd, "w") as f:
            f.write(auto_backup_script)

    def configure_nrpe(self):
        """Deploy the nagios check.

        NOTE: trailing slash(/) here is important for rsync.
        which means all content in dir other than dir itself.
        """
        # install all files in scripts/plugins/ into nagios plugins dir
        logging.debug("deploying the nagios check script")
        plugins_dir = self.charm_dir / "scripts/plugins"

        # Note that we need to add 'options' here to remove the flag '--delete' that
        # charmhelpers uses by default (!)
        #  https://github.com/juju/charm-helpers/blob/master/charmhelpers/core/host.py#L502
        # otherwise it will wipe out all the plugins installed by the nrpe charm
        rsync(
            "{}/".format(plugins_dir),
            str(Paths.NAGIOS_PLUGINS_DIR),
            options=["--executability"],
        )

        # set up the check in nagios via relation
        base_cmd = str(Paths.NAGIOS_PLUGINS_DIR / "check_auto_backup_results.py")
        check_cmd = "{} --backup-results-file {}".format(
            base_cmd,
            Paths.AUTO_BACKUP_RESULTS_PATH,
        )
        self.nrpe.add_check(
            shortname="juju_backup_all_results",
            description="check results file generated by auto_backup.py",
            check_cmd=check_cmd,
        )
        self.nrpe.write()

    def init_jujudata_dir(self):
        """Initialise the jujudata directory."""
        Paths.JUJUDATA_SSH_DIR.mkdir(exist_ok=True)
        Paths.JUJUDATA_COOKIES_DIR.mkdir(exist_ok=True)

        if not Paths.SSH_PRIVATE_KEY.exists():
            keyname = "{}@{}".format(BACKUP_USERNAME, socket.gethostname())
            logging.debug("ssh key doesn't exist, creating it...")
            cmd = [
                "ssh-keygen",
                "-t",
                "rsa",
                "-b",
                "2048",
                "-f",
                Paths.SSH_PRIVATE_KEY,
                "-C",
                keyname,
                "-N",
                "",
            ]
            try:
                subprocess.check_output(cmd)
            except subprocess.CalledProcessError as error:
                logging.error(error.output.decode("utf8"))
                raise

        self._update_dir_owner(Paths.JUJUDATA_DIR)

    def perform_backup(self, omit_models=None):
        """Perform backups."""
        # first ensure the ssh key is in all models, then perform the backup
        self.push_ssh_keys()
        backup_processor = BackupProcessor(self.config)
        backup_results = backup_processor.process_backups(omit_models=omit_models)
        logging.info("backup results = '{}'".format(backup_results))
        self._update_dir_owner(self.charm_config["backup-dir"])
        return backup_results

    def push_ssh_keys(self):
        """Use helper to push ssh keys."""
        ssh_helper = SSHKeyHelper(self.config, self.accounts)
        ssh_helper.push_ssh_keys_to_models()
        return "success"

    def update_crontab(self):
        """Update crontab "/etc/cron.d/juju-backup-all" that runs "auto_backup.py"."""
        path = "PATH=/usr/bin:/bin:/snap/bin"
        cron_job = "{}\n{} {} {} --debug".format(
            path,
            self.charm_config["crontab"],
            "root",  # backup script need to write to /var/snap/{exporter_name}/common
            Paths.AUTO_BACKUP_SCRIPT_PATH,
        )

        if self.charm_config["backup-retention-period"]:
            cron_job += " --purge {}".format(
                self.charm_config["backup-retention-period"]
            )

        if self.charm_config["timeout"]:
            cron_job += " --task-timeout {}".format(self.charm_config["timeout"])

        if self.charm_config["exclude-models"]:
            exclude_models = self.charm_config["exclude-models"].split(",")
            omit_model_params = " ".join(
                ["--omit-model {}".format(m) for m in exclude_models]
            )
            cron_job += " " + omit_model_params

        cron_job += " >> {} 2>&1\n".format(Paths.AUTO_BACKUP_LOG_PATH)
        Paths.AUTO_BACKUP_CRONTAB_PATH.write_text(cron_job)

    def update_jujudata_config(self):
        """Update the config files in JUJU_DATA."""
        # first write the yaml files
        (Paths.JUJUDATA_DIR / "controllers.yaml").write_text(
            self.charm_config["controllers"]
        )
        (Paths.JUJUDATA_DIR / "accounts.yaml").write_text(self.charm_config["accounts"])

        # need to create a cookie file for each controller configured otherwise
        # it will attempt to look for the cookies in $HOME and fail see the
        # source for 'cookies_for_controller' in this file for libjuju
        # https://github.com/juju/python-libjuju/blob/master/juju/client/jujudata.py
        controllers_yaml = self.charm_config["controllers"]
        controller_names = yaml.safe_load(controllers_yaml)["controllers"].keys()
        for controller_name in controller_names:
            logging.debug(
                "writing cookie file for controller: '{}'".format(controller_name)
            )
            (Paths.JUJUDATA_COOKIES_DIR / "{}.json".format(controller_name)).write_text(
                "null"
            )

        # save the charm config as yaml for the cronjob
        Paths.CONFIG_YAML.write_text(yaml.safe_dump(self._charm_config_to_datadict()))

        self._update_dir_owner(Paths.JUJUDATA_DIR)

    def validate_config(self):
        """Validate the current juju config options."""
        for yaml_field in ["controllers", "accounts"]:
            logging.debug("checking config '{}'...".format(yaml_field))
            content = self.charm_config[yaml_field]
            try:
                content_dict = yaml.safe_load(content)
                assert type(content_dict) == dict
                assert "controllers" in content_dict
                logging.debug("config for '{}' is valid".format(yaml_field))
            except (ParserError, AssertionError):
                msg = "Invalid yaml for '{}' option".format(yaml_field)
                logging.error(msg)
                logging.error(traceback.format_exc())
                self.model.unit.status = BlockedStatus(msg)
                return False
        return True

    def _charm_config_to_datadict(self):
        """Convert the charm config to a dict similar to juju-backup-all."""
        return {
            "all_controllers": not self.charm_config["controller-names"],
            "backup_controller": not self.charm_config["exclude-controller-backup"],
            "backup_juju_client_config": not self.charm_config[
                "exclude-juju-client-config-backup"
            ],
            "controllers": self.charm_config["controller-names"].split(","),
            "excluded_charms": self.charm_config["exclude-charms"].split(","),
            "log_level": "INFO",
            "output_dir": self.charm_config["backup-dir"],
            "timeout": self.charm_config["timeout"],
        }

    def _update_dir_owner(self, path):
        """Set the right owner for the jujudata directory."""
        host.chownr(
            path,
            owner=BACKUP_USERNAME,
            group=BACKUP_USERNAME,
            chowntopdir=True,
        )


class SSHKeyHelper:
    """Deal with SSH key operations."""

    def __init__(self, config, accounts):
        """Initialise the helper."""
        self.config = config
        self.accounts = accounts

    def push_ssh_keys_to_models(self):
        """Add jujubackup ssh keys to all relevant models."""
        pubkey = Paths.SSH_PUBLIC_KEY.read_text().strip()

        backup_processor = BackupProcessor(self.config)

        fingerprint = self._gen_libjuju_ssh_key_fingerprint()
        # go over each controller we are configured to touch, and push the
        # jujubackup key to each model if not present already
        for controller_name in backup_processor.controller_names:
            try:
                with connect_controller(controller_name) as controller:
                    logging.debug("processing controller: {}".format(controller_name))
                    model_names = run_async(controller.list_models())
                    for model_name in model_names:
                        try:
                            logging.debug(
                                "connecting to model: '{}'".format(model_name)
                            )
                            with connect_model(controller, model_name) as model:
                                logging.debug("processing model: {}".format(model_name))
                                # check if the fingerprint is present, if not add it
                                username = self.accounts[controller_name]["user"]
                                if (
                                    fingerprint
                                    not in self._get_model_ssh_key_fingeprints(model)
                                ):
                                    logging.debug(
                                        "ssh key missing for user '{}',"
                                        "adding it".format(username)
                                    )
                                    run_async(model.add_ssh_keys(username, pubkey))
                                else:
                                    logging.debug(
                                        "key for user '{}' already present,"
                                        "skipping".format(username)
                                    )
                        except Exception:
                            logging.error(traceback.format_exc())
            except Exception:
                logging.error(traceback.format_exc())

    def _gen_libjuju_ssh_key_fingerprint(self, raw_pubkey=None):
        """Generate a pubkey fingerprint in the same format as libjuju Model.get_ssh_keys.  # noqa

        The output will look something like the following:

        '41:62:c9:7b:ef:50:98:c8:ff:5c:e3:e0:33:40:0e:93 (jujubackup@juju-deb4b2-tmp-4)'
        """
        # implementation based on https://stackoverflow.com/a/6682934

        try:
            if raw_pubkey is None:
                raw_pubkey = Paths.SSH_PUBLIC_KEY.read_text().strip()
            key_type, key_body, key_comment = raw_pubkey.split()
        except ValueError:
            logging.error("Invalid ssh pubkey: {}".format(raw_pubkey))
            raise

        logging.debug(
            "processing key_comment='{}', key_body='{}'".format(key_comment, key_body)
        )
        key = base64.b64decode(key_body)
        key_fp_plain = hashlib.md5(key).hexdigest()
        key_fp = ":".join(a + b for a, b in zip(key_fp_plain[::2], key_fp_plain[1::2]))
        return "{} ({})".format(key_fp, key_comment)

    def _get_model_ssh_key_fingeprints(self, model):
        """Extract libjuju ssh keys from a model."""
        libjuju_keyinfo = run_async(model.get_ssh_keys())
        logging.debug("get_ssh_keys received: '{}'".format(libjuju_keyinfo))
        fingerprints = libjuju_keyinfo.get("results")[0]["result"]
        # handle the case where there are no keys
        return fingerprints if fingerprints else []
