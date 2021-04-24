#!/usr/bin/env python
__author__ = "OBI"
__copyright__ = "Copyright 2021, DPV e.V."
__license__ = "MIT"

import argparse
import logging
import os
import re
import time
from pathlib import Path

import rotate_backups
from typing import List

from docker import DockerUtils, DockerContainer
from logger import logger, init_mail
from params import PARAMS
from util import init_params, timestamp_string, get_folder_size_in_bytes, get_folder_size_human, str2bool, \
    check_dir_exist, check_file_exists, check_dir_empty, fix_latest_link, get_df_output


class BackupManager(object):
    def __init__(self, params):
        self._data_dir = Path(params['DATA_DIR'])
        self._user_data_dir = Path(params['USER_DATA_DIR'])
        self._backup_dir = Path(params['BACKUP_DIR'])
        self._latest_dir = self._backup_dir.joinpath('latest')

        self._db_user = params['MYSQL_USER']
        self._db_password = params['MYSQL_PASSWORD']
        self._database = params['MYSQL_DATABASE']
        self._db_server = params['MYSQL_HOST']

    def __activate_maintenance(self):
        DockerUtils.run_cmd(cmd="sudo -u www-data php ./occ maintenance:mode --on", container=DockerContainer.NEXTCLOUD)

    def __deactivate_maintenance(self):
        DockerUtils.run_cmd(cmd="sudo -u www-data php ./occ maintenance:mode --off",
                            container=DockerContainer.NEXTCLOUD)

    def __shutdown_nexcloud(self):
        DockerUtils.run_cmd(cmd="docker stop nextcloud_app", container=DockerContainer.LOCAL)

    def __start_nexcloud(self):
        DockerUtils.run_cmd(cmd="docker start nextcloud_app", container=DockerContainer.LOCAL)
        time.sleep(10)

    def __verify_prior_backup(self):
        # Check if data dir is present and non-empty
        if not check_dir_exist(self._data_dir, container=DockerContainer.BACKUP):
            logger.error("Data dir not present.")
            raise

        if check_dir_empty(self._data_dir, container=DockerContainer.BACKUP):
            logger.error("Data dir empty.")
            raise

        if not check_dir_exist(self._user_data_dir, container=DockerContainer.BACKUP):
            logger.error("User data dir not present.")
            raise

        if check_dir_empty(self._user_data_dir, container=DockerContainer.BACKUP):
            logger.error("User data dir empty.")
            raise

        # Check if backup dir is present and non-empty
        if not check_dir_exist(self._backup_dir, container=DockerContainer.BACKUP):
            logger.error("Backup dir not present.")
            raise

        if check_dir_empty(self._backup_dir, container=DockerContainer.BACKUP):
            logger.error("Backup dir empty. Check mount")
            raise

    def __backup_database(self, backup_dir: str):
        cmd_mkdir = "mkdir -p {}".format(backup_dir)
        DockerUtils.run_cmd(cmd=cmd_mkdir, container=DockerContainer.BACKUP)

        cmd = "mysqldump --single-transaction --column-statistics=0 -h {server} -u {user} -p{pwd} {db} > {path}/backup.sql".format(
            server=self._db_server,
            user=self._db_user,
            pwd=self._db_password,
            db=self._database,
            path=Path(backup_dir))
        DockerUtils.run_cmd(cmd=cmd, container=DockerContainer.BACKUP)

    def __backup_data(self, backup_dir: str, incremental=False):
        if incremental and not check_dir_exist(self._latest_dir, container=DockerContainer.BACKUP):
            logger.error("Can no perform incremental backup. Missing folder {}".format(self._latest_dir))
            incremental = False

        # Nextcloud data
        if not incremental:
            cmd = "fpsync -n 32 -o \"-a\" {source}/ {target}/data".format(source=self._data_dir, target=Path(backup_dir))
        else:
            cmd = "fpsync -n 32 -o \"-a --link-dest {latest}/data\" {source}/ {target}/data".format(source=self._data_dir,
                                                                                                  latest=self._latest_dir,
                                                                                                  target=Path(
                                                                                                      backup_dir))
        DockerUtils.run_cmd(cmd=cmd, container=DockerContainer.BACKUP)

        # User data
        if not incremental:
            cmd = "fpsync -n 32 -o \"-a\" {source}/ {target}/user_data".format(source=self._user_data_dir,
                                                                             target=Path(backup_dir))
        else:
            cmd = "fpsync -n 32 -o \"-a --link-dest {latest}/user_data\" {source}/ {target}/user_data".format(
                source=self._user_data_dir,
                latest=self._latest_dir,
                target=Path(backup_dir))
        DockerUtils.run_cmd(cmd=cmd, container=DockerContainer.BACKUP)

        cmd_link = "rm -rf {latest} && ln -s {backup_dir} {latest}".format(backup_dir=backup_dir,
                                                                           latest=self._latest_dir)
        DockerUtils.run_cmd(cmd=cmd_link, container=DockerContainer.BACKUP)

    def __verify_post_backup(self, backup_dir: str):
        # Check that database backup is there
        logger.info("Verifying backup sanity from {}".format(backup_dir))
        db_backup_fn = "{path}/backup.sql".format(path=Path(backup_dir))
        if not check_file_exists(db_backup_fn, container=DockerContainer.BACKUP):
            logger.error("Database backup file {} not present.".format(db_backup_fn))
            raise

        data_backup_dir = "{path}/data".format(path=Path(backup_dir))
        if not check_dir_exist(data_backup_dir, container=DockerContainer.BACKUP):
            logger.error("Data backup directory {} non-existent.".format(data_backup_dir))
            raise

        if check_dir_empty(data_backup_dir, container=DockerContainer.BACKUP):
            logger.error("Data backup directory {} empty.".format(data_backup_dir))
            raise

        user_data_backup_dir = "{path}/user_data".format(path=Path(backup_dir))
        if not check_dir_exist(user_data_backup_dir, container=DockerContainer.BACKUP):
            logger.error("User data backup directory {} non-existent.".format(data_backup_dir))
            raise

        if check_dir_empty(user_data_backup_dir, container=DockerContainer.BACKUP):
            logger.error("User data backup directory {} empty.".format(data_backup_dir))
            raise

        backup_size = get_folder_size_in_bytes(data_backup_dir, container=DockerContainer.BACKUP)
        if backup_size == 0:
            logger.error("Backup size: {}.".format(backup_size))
            raise

        backup_size_str = get_folder_size_human(data_backup_dir, container=DockerContainer.BACKUP)
        logger.info("Backup successful verified in {}".format(backup_size_str))

    def __verify_prior_restore(self, backup_dir: str):
        self.__verify_post_backup(backup_dir=backup_dir)

    def __restore_database(self, backup_dir: str):
        # Drop
        cmd_drop = "mysql -h {server} -u {user} -p{pwd} -e \"DROP DATABASE {db}\"".format(server=self._db_server,
                                                                                          user=self._db_user,
                                                                                          pwd=self._db_password,
                                                                                          db=self._database)
        DockerUtils.run_cmd(cmd=cmd_drop, container=DockerContainer.BACKUP)

        # Create
        cmd_create = "mysql -h {server} -u {user} -p{pwd} -e \"CREATE DATABASE {db}\"".format(server=self._db_server,
                                                                                              user=self._db_user,
                                                                                              pwd=self._db_password,
                                                                                              db=self._database)
        DockerUtils.run_cmd(cmd=cmd_create, container=DockerContainer.BACKUP)

        # Restore
        cmd_restore = "mysql -h {server} -u {user} -p{pwd} {db} < {path}/backup.sql".format(server=self._db_server,
                                                                                            user=self._db_user,
                                                                                            pwd=self._db_password,
                                                                                            db=self._database,
                                                                                            path=Path(backup_dir))
        DockerUtils.run_cmd(cmd=cmd_restore, container=DockerContainer.BACKUP)

    def __restore_data(self, backup_dir: str):
        # Nextcloud data
        cmd = "rsync -az --delete --exclude './data' {}/data/ {}".format(Path(backup_dir), self._data_dir)
        DockerUtils.run_cmd(cmd=cmd, container=DockerContainer.BACKUP)

        # User data
        cmd = "rsync -az --delete {}/user_data/ {}".format(Path(backup_dir), self._user_data_dir)
        DockerUtils.run_cmd(cmd=cmd, container=DockerContainer.BACKUP)

    def __refresh_fingerprints(self):
        DockerUtils.run_cmd(cmd="sudo -u www-data php ./occ maintenance:data-fingerprint",
                            container=DockerContainer.NEXTCLOUD)

    def __refresh_files(self):
        DockerUtils.run_cmd(cmd="sudo -u www-data php ./occ  files:scan --all",
                            container=DockerContainer.NEXTCLOUD)

    def __verify_post_restore(self):
        self.__verify_prior_backup()

    def backup(self, incremental=False) -> bool:
        ts_string = timestamp_string()
        backup_dir = os.path.join(self._backup_dir, ts_string)
        logger.info("Starting backup to folder {}".format(backup_dir))
        try:
            self.__activate_maintenance()
            self.__verify_prior_backup()
            self.__backup_database(backup_dir=backup_dir)
            self.__backup_data(backup_dir=backup_dir, incremental=incremental)
            self.__verify_post_backup(backup_dir=backup_dir)
        finally:
            self.__deactivate_maintenance()
            logger.info("Finished backup to folder {}".format(backup_dir))
            return True

    def restore_backup(self, backup_dir: str) -> bool:
        logger.info("Start restoring backup from folder {}".format(backup_dir))
        try:
            self.__shutdown_nexcloud()
            self.__verify_prior_restore(backup_dir=backup_dir)
            self.__restore_database(backup_dir=backup_dir)
            self.__restore_data(backup_dir=backup_dir)
            self.__verify_post_restore()
        finally:
            self.__start_nexcloud()
            try:
                self.__deactivate_maintenance()
            except:
                logger.info("Could not deactivate maintenance mode.")

            self.__refresh_fingerprints()
            self.__refresh_files()
            logger.info("Finished restoring backup from folder {}".format(backup_dir))
            return True

    def list_backups(self) -> List[str]:
        backup_dirs = []
        regexp = re.compile('[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}')
        for f in os.scandir(self._backup_dir):
            if f.is_dir() and bool(regexp.search(f.path)):
                backup_dirs.append(f.path)

        backup_dirs.sort()
        return backup_dirs

    def clean_backups(self):
        rotation_fn = os.path.dirname(os.path.realpath(__file__)) + '/rotation.ini'
        res = rotate_backups.load_config_file(rotation_fn)
        for location, rotation_scheme, options in res:
            options['prefer_recent'] = True
            rotate_backups.RotateBackups(rotation_scheme=rotation_scheme, **options).rotate_backups(location)
        fix_latest_link(self._backup_dir, self._latest_dir, container=DockerContainer.BACKUP)

    def create_summary(self):
        nextcloud_data_size = get_folder_size_human(self._data_dir, container=DockerContainer.BACKUP)
        user_data_size = get_folder_size_human(self._user_data_dir, container=DockerContainer.BACKUP)
        backup_size = get_folder_size_human(self._backup_dir, container=DockerContainer.BACKUP)
        dfh = get_df_output(self._data_dir, container=DockerContainer.BACKUP)
        logger.info("Current nextcloud data size: {data_size}".format(data_size=nextcloud_data_size))
        logger.info("Current user data size: {data_size}".format(data_size=user_data_size))
        logger.info("Current backup size: {backup_size}".format(backup_size=backup_size))
        logger.info("{dfh}".format(dfh=dfh))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument('-r', '--restore', required=False,
                        help="Restore backup.", action="store_true")
    parser.add_argument('-b', '--backup', required=False,
                        help="Create backup.", action="store_true")
    parser.add_argument('-c', '--clean', required=False,
                        help="Rotate backups.", action="store_true")
    parser.add_argument('-i', '--incremental', required=False,
                        help="Incremental backup.", action="store_true")
    parser.add_argument('-v', '--verbose', required=False,
                        help="Increase verbosity backup.", action="store_true")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        logger.setLevel(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
        logger.setLevel(level=logging.INFO)

    incremental_backup = False
    if args.incremental:
        incremental_backup = True

    init_params(PARAMS)

    if str2bool(PARAMS['SEND_MAIL']):
        init_mail(fromaddr=PARAMS['SMTP_FROM'], password=PARAMS['SMTP_PWD'], toaddrs=PARAMS['SMTP_TO'],
                  subject="DPV Cloud Backup", mailhost=PARAMS['SMTP_HOST'],
                  mailport=PARAMS['SMTP_PORT'])

    backup_manager = BackupManager(params=PARAMS)

    if args.backup:
        backup_manager.backup(incremental=incremental_backup)

    if args.restore:
        backups = backup_manager.list_backups()
        for c, val in enumerate(backups):
            logger.info("   #{}:    {}".format(c, val))

        backup_id = int(input("Enter backup to restore:"))
        assert backup_id < len(backups)
        backup_manager.restore_backup(backups[backup_id])

    if args.clean:
        backup_manager.clean_backups()

    backup_manager.create_summary()

    logging.shutdown()
    exit(0)
