import json
import logging
import os
from datetime import datetime, timedelta

import boto3

from common import parameters
from common.data_layer import device as device_repository
from common.data_layer import account_repository

logger = logging.getLogger()
logger.setLevel(logging.INFO)

scheduler_client = boto3.client('scheduler')
DEVICES_ID_TO_SKIP = json.loads(parameters.get_param('device-deployment/devices_to_skip'))


def _get_deployment_time(should_deploy_now: bool):
    now = datetime.now()
    if should_deploy_now:
        return (now + timedelta(minutes=1)).isoformat(timespec='seconds')
    else:
        return(now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0).isoformat(timespec='seconds')


def upgrade_account_devices(account_id: str, version_tag: str, should_deploy_now: bool):
    for user_device in device_repository.get_account_user_devices(account_id):
        try:
            if user_device["deviceId"] in DEVICES_ID_TO_SKIP:
                logger.info(f"Skipping device {user_device['deviceId']}")
                continue
            timezone = user_device["timezone"] if user_device["timezone"] else "UTC"
            scheduler_client.create_schedule(
                ClientToken=f'{user_device["deviceId"]}_{version_tag}',
                FlexibleTimeWindow={
                    "Mode": "OFF"},
                Name=f'{os.environ["ENV"]}-device_{user_device["deviceId"]}-version_upgrade_{int(datetime.now().timestamp())}',
                ScheduleExpression=f'at({_get_deployment_time(should_deploy_now)})',
                ScheduleExpressionTimezone=timezone,
                Target={
                    "Arn": parameters.get_queue_arn("device-deployment-tracker"),
                    "Input": json.dumps({"device_id": user_device["deviceId"], "version_tag": version_tag}),
                    "RoleArn": os.environ["scheduler_role"]
                }
            )
        except Exception as e:
            logger.exception(f"Failed to schedule upgrade for device {user_device['deviceId']}. Error: {e}")


def lambda_handler(event, _):
    logger.info(f"Received event: {event}")
    version_tag = event["version_tag"]
    should_deploy_now = event.get("should_deploy_now", False)
    for account in account_repository.get_all():
        upgrade_account_devices(account.id, version_tag, should_deploy_now)
    logger.info("Finished requesting upgrade of all devices for all accounts")
